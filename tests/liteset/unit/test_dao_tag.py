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
"""Tests for AsyncTagDAO using simplified test models."""
from __future__ import annotations

import pytest
from sqlalchemy import Column, ForeignKey, Integer, String, Table, delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase

from liteset.db.base_dao import BaseAsyncDAO


class Base(DeclarativeBase):
    pass


class FakeTag(Base):
    __tablename__ = "fake_tags"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(250), unique=True, nullable=False)
    type = Column(String(50), nullable=True)


class FakeTaggedObject(Base):
    __tablename__ = "fake_tagged_objects"
    id = Column(Integer, primary_key=True, autoincrement=True)
    tag_id = Column(Integer, ForeignKey("fake_tags.id"), nullable=False)
    object_id = Column(Integer, nullable=False)
    object_type = Column(String(50), nullable=False)


fake_user_favorite_tag_table = Table(
    "fake_user_favorite_tags",
    Base.metadata,
    Column("tag_id", Integer, ForeignKey("fake_tags.id"), primary_key=True),
    Column("user_id", Integer, primary_key=True),
)


class FakeTagDAO(BaseAsyncDAO[FakeTag]):
    model_cls = FakeTag

    async def find_by_name(self, name: str) -> FakeTag | None:
        return await self.find_one_or_none(name=name)

    async def find_by_names(self, names: list[str]) -> list[FakeTag]:
        if not names:
            return []
        stmt = select(FakeTag).where(FakeTag.name.in_(names))
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_name(self, name: str, type_name: str = "custom") -> FakeTag:
        tag = await self.find_one_or_none(name=name, type=type_name)
        if tag:
            return tag
        tag = FakeTag(name=name, type=type_name)
        self.session.add(tag)
        await self.session.flush()
        return tag

    async def _find_tagged_object(
        self, object_type: str, object_id: int, tag_id: int,
    ) -> FakeTaggedObject | None:
        stmt = select(FakeTaggedObject).where(
            FakeTaggedObject.object_type == object_type,
            FakeTaggedObject.object_id == object_id,
            FakeTaggedObject.tag_id == tag_id,
        )
        result = await self.session.execute(stmt)
        return result.scalars().one_or_none()

    async def find_tagged_object(
        self, object_type: str, object_id: int, tag_id: int,
    ) -> FakeTaggedObject | None:
        return await self._find_tagged_object(object_type, object_id, tag_id)

    async def create_custom_tagged_objects(
        self, object_type: str, object_id: int, tag_names: list[str],
    ) -> None:
        for name in tag_names:
            tag = await self.get_by_name(name, "custom")
            existing = await self._find_tagged_object(object_type, object_id, tag.id)
            if not existing:
                tagged = FakeTaggedObject(
                    tag_id=tag.id, object_id=object_id, object_type=object_type,
                )
                self.session.add(tagged)

    async def delete_tagged_object(
        self, object_type: str, object_id: int, tag_name: str,
    ) -> None:
        tag = await self.find_by_name(tag_name)
        if not tag:
            return
        stmt = delete(FakeTaggedObject).where(
            FakeTaggedObject.tag_id == tag.id,
            FakeTaggedObject.object_type == object_type,
            FakeTaggedObject.object_id == object_id,
        )
        await self.session.execute(stmt)

    async def delete_tags(self, tag_names: list[str]) -> None:
        if not tag_names:
            return
        tags = await self.find_by_names(tag_names)
        tag_ids = [t.id for t in tags]
        if tag_ids:
            await self.session.execute(
                delete(FakeTaggedObject).where(FakeTaggedObject.tag_id.in_(tag_ids))
            )
            await self.session.execute(
                delete(FakeTag).where(FakeTag.id.in_(tag_ids))
            )

    async def get_tagged_objects_by_tag_names(
        self, tag_names: list[str], obj_types: list[str] | None = None,
    ) -> list[FakeTaggedObject]:
        tags = await self.find_by_names(tag_names)
        tag_ids = [t.id for t in tags]
        return await self.get_tagged_objects_by_tag_ids(tag_ids, obj_types)

    async def create_tag_relationship(
        self, objects_to_tag: list[tuple[str, int]], tag: FakeTag,
    ) -> None:
        for obj_type, obj_id in objects_to_tag:
            existing = await self._find_tagged_object(obj_type, obj_id, tag.id)
            if not existing:
                tagged = FakeTaggedObject(
                    tag_id=tag.id, object_id=obj_id, object_type=obj_type,
                )
                self.session.add(tagged)

    async def get_tagged_objects_by_tag_ids(
        self,
        tag_ids: list[int],
        obj_types: list[str] | None = None,
    ) -> list[FakeTaggedObject]:
        if not tag_ids:
            return []
        stmt = select(FakeTaggedObject).where(FakeTaggedObject.tag_id.in_(tag_ids))
        if obj_types:
            stmt = stmt.where(FakeTaggedObject.object_type.in_(obj_types))
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def favorite_tag_by_id_for_current_user(
        self, tag_id: int, user_id: int
    ) -> bool:
        tag = await self.find_by_id(tag_id)
        if not tag:
            return False
        existing = await self.favorited_ids([tag_id], user_id)
        if existing:
            return True
        stmt = fake_user_favorite_tag_table.insert().values(
            tag_id=tag_id, user_id=user_id
        )
        await self.session.execute(stmt)
        return True

    async def remove_user_favorite_tag(self, tag_id: int, user_id: int) -> bool:
        tag = await self.find_by_id(tag_id)
        if not tag:
            return False
        stmt = delete(fake_user_favorite_tag_table).where(
            fake_user_favorite_tag_table.c.tag_id == tag_id,
            fake_user_favorite_tag_table.c.user_id == user_id,
        )
        await self.session.execute(stmt)
        return True

    async def favorited_ids(self, tag_ids: list[int], user_id: int) -> list[int]:
        if not tag_ids:
            return []
        stmt = select(fake_user_favorite_tag_table.c.tag_id).where(
            fake_user_favorite_tag_table.c.tag_id.in_(tag_ids),
            fake_user_favorite_tag_table.c.user_id == user_id,
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())


@pytest.fixture
async def async_session():
    from tests.liteset.conftest import create_test_session

    async with create_test_session(Base) as session:
        yield session


async def test_create_tag(async_session: AsyncSession) -> None:
    dao = FakeTagDAO(async_session)
    tag = await dao.create({"name": "important", "type": "custom"})
    await async_session.flush()
    assert tag.id is not None
    assert tag.name == "important"


async def test_get_tagged_objects_by_tag_ids(async_session: AsyncSession) -> None:
    dao = FakeTagDAO(async_session)
    tag = await dao.create({"name": "test-tag"})
    await async_session.flush()

    to1 = FakeTaggedObject(tag_id=tag.id, object_id=1, object_type="dashboard")
    to2 = FakeTaggedObject(tag_id=tag.id, object_id=2, object_type="chart")
    to3 = FakeTaggedObject(tag_id=tag.id, object_id=3, object_type="chart")
    async_session.add_all([to1, to2, to3])
    await async_session.flush()

    results = await dao.get_tagged_objects_by_tag_ids([tag.id])
    assert len(results) == 3


async def test_get_tagged_objects_filtered_by_type(async_session: AsyncSession) -> None:
    dao = FakeTagDAO(async_session)
    tag = await dao.create({"name": "filter-tag"})
    await async_session.flush()

    to1 = FakeTaggedObject(tag_id=tag.id, object_id=1, object_type="dashboard")
    to2 = FakeTaggedObject(tag_id=tag.id, object_id=2, object_type="chart")
    async_session.add_all([to1, to2])
    await async_session.flush()

    results = await dao.get_tagged_objects_by_tag_ids(
        [tag.id], obj_types=["chart"]
    )
    assert len(results) == 1
    assert results[0].object_type == "chart"


async def test_get_tagged_objects_empty_tag_ids(async_session: AsyncSession) -> None:
    dao = FakeTagDAO(async_session)
    results = await dao.get_tagged_objects_by_tag_ids([])
    assert results == []


async def test_tag_crud(async_session: AsyncSession) -> None:
    dao = FakeTagDAO(async_session)
    tag = await dao.create({"name": "to-delete"})
    await async_session.flush()

    await dao.delete([tag])
    await async_session.flush()

    found = await dao.find_by_id(tag.id)
    assert found is None


async def test_favorite_tag(async_session: AsyncSession) -> None:
    dao = FakeTagDAO(async_session)
    tag = await dao.create({"name": "fav-tag", "type": "custom"})
    await async_session.flush()

    result = await dao.favorite_tag_by_id_for_current_user(tag.id, user_id=1)
    assert result is True

    # Verify idempotency
    result2 = await dao.favorite_tag_by_id_for_current_user(tag.id, user_id=1)
    assert result2 is True


async def test_favorite_tag_not_found(async_session: AsyncSession) -> None:
    dao = FakeTagDAO(async_session)
    result = await dao.favorite_tag_by_id_for_current_user(tag_id=9999, user_id=1)
    assert result is False


async def test_remove_favorite_tag(async_session: AsyncSession) -> None:
    dao = FakeTagDAO(async_session)
    tag = await dao.create({"name": "rm-tag", "type": "custom"})
    await async_session.flush()

    await dao.favorite_tag_by_id_for_current_user(tag.id, user_id=1)
    await async_session.flush()
    result = await dao.remove_user_favorite_tag(tag.id, user_id=1)
    assert result is True

    favs = await dao.favorited_ids([tag.id], user_id=1)
    assert favs == []


async def test_find_by_name(async_session: AsyncSession) -> None:
    dao = FakeTagDAO(async_session)
    await dao.create({"name": "alpha", "type": "custom"})
    await async_session.flush()

    found = await dao.find_by_name("alpha")
    assert found is not None
    assert found.name == "alpha"

    not_found = await dao.find_by_name("nonexistent")
    assert not_found is None


async def test_find_by_names(async_session: AsyncSession) -> None:
    dao = FakeTagDAO(async_session)
    await dao.create({"name": "a"})
    await dao.create({"name": "b"})
    await dao.create({"name": "c"})
    await async_session.flush()

    found = await dao.find_by_names(["a", "c"])
    assert len(found) == 2
    names = {t.name for t in found}
    assert names == {"a", "c"}


async def test_find_by_names_empty(async_session: AsyncSession) -> None:
    dao = FakeTagDAO(async_session)
    found = await dao.find_by_names([])
    assert found == []


async def test_get_by_name_creates_if_missing(async_session: AsyncSession) -> None:
    dao = FakeTagDAO(async_session)
    tag = await dao.get_by_name("new-tag", "custom")
    assert tag.id is not None
    assert tag.name == "new-tag"

    # Second call returns same tag
    tag2 = await dao.get_by_name("new-tag", "custom")
    assert tag2.id == tag.id


async def test_create_custom_tagged_objects(async_session: AsyncSession) -> None:
    dao = FakeTagDAO(async_session)
    await dao.create_custom_tagged_objects("dashboard", 42, ["tag1", "tag2"])
    await async_session.flush()

    tag1 = await dao.find_by_name("tag1")
    tag2 = await dao.find_by_name("tag2")
    assert tag1 is not None
    assert tag2 is not None

    objs = await dao.get_tagged_objects_by_tag_ids([tag1.id, tag2.id])
    assert len(objs) == 2

    # Idempotent - calling again should not duplicate
    await dao.create_custom_tagged_objects("dashboard", 42, ["tag1"])
    await async_session.flush()
    objs2 = await dao.get_tagged_objects_by_tag_ids([tag1.id])
    assert len(objs2) == 1


async def test_find_tagged_object(async_session: AsyncSession) -> None:
    dao = FakeTagDAO(async_session)
    tag = await dao.create({"name": "ft-tag"})
    await async_session.flush()

    to = FakeTaggedObject(tag_id=tag.id, object_id=1, object_type="chart")
    async_session.add(to)
    await async_session.flush()

    found = await dao.find_tagged_object("chart", 1, tag.id)
    assert found is not None

    not_found = await dao.find_tagged_object("chart", 999, tag.id)
    assert not_found is None


async def test_delete_tagged_object(async_session: AsyncSession) -> None:
    dao = FakeTagDAO(async_session)
    tag = await dao.create({"name": "del-tag"})
    await async_session.flush()

    to = FakeTaggedObject(tag_id=tag.id, object_id=5, object_type="dashboard")
    async_session.add(to)
    await async_session.flush()

    await dao.delete_tagged_object("dashboard", 5, "del-tag")
    await async_session.flush()

    found = await dao.find_tagged_object("dashboard", 5, tag.id)
    assert found is None


async def test_delete_tagged_object_tag_not_found(async_session: AsyncSession) -> None:
    dao = FakeTagDAO(async_session)
    # Should not raise
    await dao.delete_tagged_object("dashboard", 5, "nonexistent")


async def test_delete_tags(async_session: AsyncSession) -> None:
    dao = FakeTagDAO(async_session)
    t1 = await dao.create({"name": "del1"})
    t2 = await dao.create({"name": "del2"})
    await async_session.flush()

    to = FakeTaggedObject(tag_id=t1.id, object_id=1, object_type="chart")
    async_session.add(to)
    await async_session.flush()

    await dao.delete_tags(["del1", "del2"])
    await async_session.flush()

    assert await dao.find_by_name("del1") is None
    assert await dao.find_by_name("del2") is None
    objs = await dao.get_tagged_objects_by_tag_ids([t1.id])
    assert objs == []


async def test_get_tagged_objects_by_tag_names(async_session: AsyncSession) -> None:
    dao = FakeTagDAO(async_session)
    t1 = await dao.create({"name": "name-tag"})
    await async_session.flush()

    to = FakeTaggedObject(tag_id=t1.id, object_id=10, object_type="chart")
    async_session.add(to)
    await async_session.flush()

    results = await dao.get_tagged_objects_by_tag_names(["name-tag"])
    assert len(results) == 1

    results2 = await dao.get_tagged_objects_by_tag_names(["name-tag"], obj_types=["dashboard"])
    assert len(results2) == 0


async def test_create_tag_relationship(async_session: AsyncSession) -> None:
    dao = FakeTagDAO(async_session)
    tag = await dao.create({"name": "rel-tag"})
    await async_session.flush()

    await dao.create_tag_relationship(
        [("chart", 1), ("dashboard", 2)], tag,
    )
    await async_session.flush()

    objs = await dao.get_tagged_objects_by_tag_ids([tag.id])
    assert len(objs) == 2

    # Idempotent
    await dao.create_tag_relationship([("chart", 1)], tag)
    await async_session.flush()
    objs2 = await dao.get_tagged_objects_by_tag_ids([tag.id])
    assert len(objs2) == 2
