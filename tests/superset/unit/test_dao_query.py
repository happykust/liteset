# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
"""Tests for AsyncQueryDAO using simplified test models."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy import Column, DateTime, Float, Integer, String
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase

from superset.db.base_dao import BaseAsyncDAO
from superset.db.daos.query import AsyncQueryDAO


class Base(DeclarativeBase):
    pass


class FakeQuery(Base):
    __tablename__ = "fake_queries"
    id = Column(Integer, primary_key=True, autoincrement=True)
    client_id = Column(String(255), unique=True, nullable=True)
    user_id = Column(Integer, nullable=False)
    status = Column(String(50), nullable=False, default="running")
    end_time = Column(Float, nullable=True)
    changed_on = Column(DateTime, default=datetime.now)
    extra_json = Column(String(2000), default="{}")

    def set_extra_json_key(self, key: str, value: object) -> None:
        import json as _json

        data = _json.loads(self.extra_json or "{}")
        data[key] = value
        self.extra_json = _json.dumps(data)


class FakeSavedQuery(Base):
    __tablename__ = "fake_saved_queries"
    id = Column(Integer, primary_key=True, autoincrement=True)
    label = Column(String(250), nullable=False)


class FakeQueryDAO(BaseAsyncDAO[FakeQuery]):
    model_cls = FakeQuery

    async def save_metadata(self, query: FakeQuery, payload: dict) -> None:
        # default {}, unconditional overwrite, keep name key
        columns = payload.get("columns", {})
        for col in columns:
            if "name" in col:
                col["column_name"] = col.get("name")
        self.session.add(query)
        query.set_extra_json_key("columns", columns)

    async def get_queries_changed_after(
        self, user_id: int, last_updated_ms: float
    ) -> list[FakeQuery]:
        from sqlalchemy import select

        last_updated_dt = datetime.fromtimestamp(
            last_updated_ms / 1000, tz=timezone.utc
        )
        stmt = select(FakeQuery).where(
            FakeQuery.user_id == user_id,
            FakeQuery.changed_on >= last_updated_dt,
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def stop_query(self, client_id: str) -> FakeQuery | None:
        query = await self.find_one_or_none(client_id=client_id)
        if not query:
            return None
        if query.status in ("failed", "success", "timed_out", "stopped"):
            return query
        query.status = "stopped"
        query.end_time = datetime.now(tz=timezone.utc).timestamp()
        return query


class FakeSavedQueryDAO(BaseAsyncDAO[FakeSavedQuery]):
    model_cls = FakeSavedQuery


@pytest.fixture
async def async_session():
    from tests.superset.conftest import create_test_session

    async with create_test_session(Base) as session:
        yield session


async def test_create_query(async_session: AsyncSession) -> None:
    dao = FakeQueryDAO(async_session)
    q = await dao.create(
        {
            "client_id": "abc-123",
            "user_id": 1,
            "status": "running",
            "changed_on": datetime.now(tz=timezone.utc),
        }
    )
    await async_session.flush()
    assert q.id is not None


async def test_stop_query(async_session: AsyncSession) -> None:
    dao = FakeQueryDAO(async_session)
    await dao.create(
        {
            "client_id": "stop-me",
            "user_id": 1,
            "status": "running",
            "changed_on": datetime.now(tz=timezone.utc),
        }
    )
    await async_session.flush()

    stopped = await dao.stop_query("stop-me")
    assert stopped is not None
    assert stopped.status == "stopped"
    assert stopped.end_time is not None


async def test_stop_query_not_found(async_session: AsyncSession) -> None:
    dao = FakeQueryDAO(async_session)
    result = await dao.stop_query("nonexistent")
    assert result is None


async def test_stop_query_already_stopped(async_session: AsyncSession) -> None:
    dao = FakeQueryDAO(async_session)
    await dao.create(
        {
            "client_id": "done",
            "user_id": 1,
            "status": "success",
            "changed_on": datetime.now(tz=timezone.utc),
        }
    )
    await async_session.flush()

    result = await dao.stop_query("done")
    assert result is not None
    assert result.status == "success"  # unchanged


async def test_get_queries_changed_after(async_session: AsyncSession) -> None:
    dao = FakeQueryDAO(async_session)
    old_time = datetime(2020, 1, 1)
    new_time = datetime(2025, 6, 1)

    await dao.create(
        {
            "client_id": "old",
            "user_id": 1,
            "status": "success",
            "changed_on": old_time,
        }
    )
    await dao.create(
        {
            "client_id": "new",
            "user_id": 1,
            "status": "running",
            "changed_on": new_time,
        }
    )
    await async_session.flush()

    # Query for changes after 2024-01-01
    cutoff_ms = datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp() * 1000
    results = await dao.get_queries_changed_after(user_id=1, last_updated_ms=cutoff_ms)
    assert len(results) == 1
    assert results[0].client_id == "new"


async def test_saved_query_crud(async_session: AsyncSession) -> None:
    dao = FakeSavedQueryDAO(async_session)
    sq = await dao.create({"label": "My Query"})
    await async_session.flush()
    assert sq.id is not None

    found = await dao.find_by_id(sq.id)
    assert found is not None
    assert found.label == "My Query"


async def test_save_metadata(async_session: AsyncSession) -> None:
    dao = FakeQueryDAO(async_session)
    q = await dao.create(
        {
            "client_id": "meta-1",
            "user_id": 1,
            "status": "success",
            "changed_on": datetime.now(tz=timezone.utc),
        }
    )
    await async_session.flush()

    await dao.save_metadata(
        q,
        {
            "columns": [
                {"name": "id", "type": "INTEGER"},
                {"column_name": "already_named", "type": "TEXT"},
            ],
        },
    )
    await async_session.flush()

    data = json.loads(q.extra_json)
    assert len(data["columns"]) == 2
    # column_name is set from name
    assert data["columns"][0]["column_name"] == "id"
    # 'name' key is kept, not removed
    assert data["columns"][0]["name"] == "id"
    assert data["columns"][1]["column_name"] == "already_named"


# ---------------------------------------------------------------------------
# Direct tests for AsyncQueryDAO.save_metadata (via FakeQuery + mock session)
# ---------------------------------------------------------------------------


async def test_async_dao_save_metadata_name_preserved() -> None:
    """Col with only 'name' stores BOTH 'name' AND 'column_name'."""
    mock_session = MagicMock()
    dao = AsyncQueryDAO(mock_session)
    q = FakeQuery()
    q.extra_json = "{}"

    await dao.save_metadata(q, {"columns": [{"name": "my_col", "type": "TEXT"}]})

    data = json.loads(q.extra_json)
    assert data["columns"][0]["column_name"] == "my_col"
    # original keeps 'name' key — must not be popped
    assert data["columns"][0]["name"] == "my_col"


async def test_async_dao_save_metadata_overwrites_column_name() -> None:
    """'column_name' is overwritten when 'name' is present.

    The assignment is unconditional: col["column_name"] = col.get("name")
    """
    mock_session = MagicMock()
    dao = AsyncQueryDAO(mock_session)
    q = FakeQuery()
    q.extra_json = "{}"

    await dao.save_metadata(
        q,
        {
            "columns": [
                {"name": "new_name", "column_name": "old_name", "type": "TEXT"},
            ]
        },
    )

    data = json.loads(q.extra_json)
    # column_name unconditionally overwritten with name
    assert data["columns"][0]["column_name"] == "new_name"
    assert data["columns"][0]["name"] == "new_name"


async def test_async_dao_save_metadata_no_columns_default_dict() -> None:
    """Absent 'columns' key stores {} not []."""
    mock_session = MagicMock()
    dao = AsyncQueryDAO(mock_session)
    q = FakeQuery()
    q.extra_json = "{}"

    await dao.save_metadata(q, {})  # no 'columns' key in payload

    data = json.loads(q.extra_json)
    # original defaults to {} (empty dict), not []
    assert data["columns"] == {}
