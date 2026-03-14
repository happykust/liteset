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
from __future__ import annotations

import pytest
from sqlalchemy import Column, Integer, String
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from liteset.db.base_dao import BaseAsyncDAO


class Base(DeclarativeBase):
    pass


class SampleModel(Base):
    __tablename__ = "sample"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(50), nullable=False)


class SampleDAO(BaseAsyncDAO[SampleModel]):
    model_cls = SampleModel


@pytest.fixture
async def async_session():
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with AsyncSession(engine, expire_on_commit=False) as session:
        yield session

    await engine.dispose()


async def test_create(async_session: AsyncSession) -> None:
    dao = SampleDAO(async_session)
    item = await dao.create({"name": "alice"})
    await async_session.flush()
    assert item.id is not None
    assert item.name == "alice"


async def test_find_by_id(async_session: AsyncSession) -> None:
    dao = SampleDAO(async_session)
    item = await dao.create({"name": "bob"})
    await async_session.flush()

    found = await dao.find_by_id(item.id)
    assert found is not None
    assert found.name == "bob"


async def test_find_by_id_not_found(async_session: AsyncSession) -> None:
    dao = SampleDAO(async_session)
    found = await dao.find_by_id(9999)
    assert found is None


async def test_find_all(async_session: AsyncSession) -> None:
    dao = SampleDAO(async_session)
    await dao.create({"name": "one"})
    await dao.create({"name": "two"})
    await async_session.flush()

    items = await dao.find_all()
    assert len(items) == 2


async def test_find_all_with_filters(async_session: AsyncSession) -> None:
    dao = SampleDAO(async_session)
    await dao.create({"name": "alpha"})
    await dao.create({"name": "beta"})
    await async_session.flush()

    items = await dao.find_all(filters=[SampleModel.name == "alpha"])
    assert len(items) == 1
    assert items[0].name == "alpha"


async def test_update(async_session: AsyncSession) -> None:
    dao = SampleDAO(async_session)
    item = await dao.create({"name": "original"})
    await async_session.flush()

    updated = await dao.update(item, {"name": "changed"})
    assert updated.name == "changed"


async def test_delete(async_session: AsyncSession) -> None:
    dao = SampleDAO(async_session)
    item = await dao.create({"name": "to_delete"})
    await async_session.flush()

    await dao.delete([item])
    await async_session.flush()

    found = await dao.find_by_id(item.id)
    assert found is None


async def test_find_one_or_none(async_session: AsyncSession) -> None:
    dao = SampleDAO(async_session)
    await dao.create({"name": "unique"})
    await async_session.flush()

    found = await dao.find_one_or_none(name="unique")
    assert found is not None
    assert found.name == "unique"

    not_found = await dao.find_one_or_none(name="nonexistent")
    assert not_found is None


async def test_bulk_delete(async_session: AsyncSession) -> None:
    dao = SampleDAO(async_session)
    a = await dao.create({"name": "a"})
    b = await dao.create({"name": "b"})
    await dao.create({"name": "c"})
    await async_session.flush()
    deleted = await dao.bulk_delete([a.id, b.id])
    await async_session.flush()
    assert deleted == 2
    remaining = await dao.find_all()
    assert len(remaining) == 1
    assert remaining[0].name == "c"


async def test_bulk_delete_empty_list(async_session: AsyncSession) -> None:
    dao = SampleDAO(async_session)
    deleted = await dao.bulk_delete([])
    assert deleted == 0


async def test_find_by_ids(async_session: AsyncSession) -> None:
    dao = SampleDAO(async_session)
    a = await dao.create({"name": "a"})
    b = await dao.create({"name": "b"})
    await dao.create({"name": "c"})
    await async_session.flush()

    items = await dao.find_by_ids([a.id, b.id])
    assert len(items) == 2
    names = {item.name for item in items}
    assert names == {"a", "b"}
