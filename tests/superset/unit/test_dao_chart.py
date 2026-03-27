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
"""Tests for AsyncChartDAO using simplified test models.

We use test-local models instead of real superset models because
superset requires Flask/werkzeug which is not available in superset's
test environment.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import pytest
from sqlalchemy import Column, DateTime, Integer, String
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase

from superset.db.base_dao import BaseAsyncDAO


class Base(DeclarativeBase):
    pass


class FakeChart(Base):
    __tablename__ = "test_charts"
    id = Column(Integer, primary_key=True, autoincrement=True)
    uuid = Column(String(36), unique=True, nullable=True)
    slice_name = Column(String(250), nullable=False)
    viz_type = Column(String(250), nullable=True)


class FakeFavStar(Base):
    __tablename__ = "test_favstar"
    id = Column(Integer, primary_key=True, autoincrement=True)
    class_name = Column(String(50), nullable=False)
    obj_id = Column(Integer, nullable=False)
    user_id = Column(Integer, nullable=False)
    dttm = Column(DateTime, nullable=True)


class FakeChartDAO(BaseAsyncDAO[FakeChart]):
    model_cls = FakeChart

    async def get_by_id_or_uuid(self, id_or_uuid: int | str) -> FakeChart | None:
        try:
            chart_id = int(id_or_uuid)
            return await self.find_by_id(chart_id)
        except (ValueError, TypeError):
            pass
        try:
            uuid.UUID(str(id_or_uuid))
        except ValueError:
            return None
        return await self.find_one_or_none(uuid=str(id_or_uuid))

    async def favorited_ids(self, chart_ids: list[int], user_id: int) -> list[int]:
        if not chart_ids:
            return []
        from sqlalchemy import select

        stmt = select(FakeFavStar.obj_id).where(
            FakeFavStar.class_name == "chart",
            FakeFavStar.obj_id.in_(chart_ids),
            FakeFavStar.user_id == user_id,
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def add_favorite(self, chart_id: int, user_id: int) -> None:
        existing = await self.favorited_ids([chart_id], user_id)
        if existing:
            return
        fav = FakeFavStar(
            class_name="chart",
            obj_id=chart_id,
            user_id=user_id,
            dttm=datetime.now(),
        )
        self.session.add(fav)

    async def remove_favorite(self, chart_id: int, user_id: int) -> None:
        from sqlalchemy import delete

        stmt = delete(FakeFavStar).where(
            FakeFavStar.class_name == "chart",
            FakeFavStar.obj_id == chart_id,
            FakeFavStar.user_id == user_id,
        )
        await self.session.execute(stmt)


@pytest.fixture
async def async_session():
    from tests.superset.conftest import create_test_session

    async with create_test_session(Base) as session:
        yield session


async def test_create_chart(async_session: AsyncSession) -> None:
    dao = FakeChartDAO(async_session)
    chart = await dao.create({"slice_name": "My Chart", "viz_type": "bar"})
    await async_session.flush()
    assert chart.id is not None
    assert chart.slice_name == "My Chart"


async def test_get_by_id_or_uuid_by_id(async_session: AsyncSession) -> None:
    dao = FakeChartDAO(async_session)
    chart = await dao.create({"slice_name": "Test"})
    await async_session.flush()

    found = await dao.get_by_id_or_uuid(chart.id)
    assert found is not None
    assert found.slice_name == "Test"


async def test_get_by_id_or_uuid_by_uuid(async_session: AsyncSession) -> None:
    dao = FakeChartDAO(async_session)
    chart_uuid = str(uuid.uuid4())
    chart = await dao.create({"slice_name": "UUID Chart", "uuid": chart_uuid})
    await async_session.flush()

    found = await dao.get_by_id_or_uuid(chart_uuid)
    assert found is not None
    assert found.slice_name == "UUID Chart"


async def test_get_by_id_or_uuid_not_found(async_session: AsyncSession) -> None:
    dao = FakeChartDAO(async_session)
    found = await dao.get_by_id_or_uuid("nonexistent-uuid")
    assert found is None


async def test_favorited_ids(async_session: AsyncSession) -> None:
    dao = FakeChartDAO(async_session)
    c1 = await dao.create({"slice_name": "C1"})
    c2 = await dao.create({"slice_name": "C2"})
    await async_session.flush()

    await dao.add_favorite(c1.id, user_id=1)
    await async_session.flush()

    favs = await dao.favorited_ids([c1.id, c2.id], user_id=1)
    assert c1.id in favs
    assert c2.id not in favs


async def test_favorited_ids_empty(async_session: AsyncSession) -> None:
    dao = FakeChartDAO(async_session)
    favs = await dao.favorited_ids([], user_id=1)
    assert favs == []


async def test_add_favorite_idempotent(async_session: AsyncSession) -> None:
    dao = FakeChartDAO(async_session)
    chart = await dao.create({"slice_name": "Fav"})
    await async_session.flush()

    await dao.add_favorite(chart.id, user_id=1)
    await async_session.flush()
    await dao.add_favorite(chart.id, user_id=1)  # should not duplicate
    await async_session.flush()

    favs = await dao.favorited_ids([chart.id], user_id=1)
    assert len(favs) == 1


async def test_remove_favorite(async_session: AsyncSession) -> None:
    dao = FakeChartDAO(async_session)
    chart = await dao.create({"slice_name": "Removable"})
    await async_session.flush()

    await dao.add_favorite(chart.id, user_id=1)
    await async_session.flush()

    await dao.remove_favorite(chart.id, user_id=1)
    await async_session.flush()

    favs = await dao.favorited_ids([chart.id], user_id=1)
    assert len(favs) == 0
