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
"""Tests for AsyncAnnotationLayerDAO and AsyncAnnotationDAO."""

from __future__ import annotations

import pytest
from sqlalchemy import Column, ForeignKey, Integer, select, String
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase

from liteset.db.base_dao import BaseAsyncDAO


class Base(DeclarativeBase):
    pass


class FakeAnnotationLayer(Base):
    __tablename__ = "fake_annotation_layers"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(250), nullable=False)


class FakeAnnotation(Base):
    __tablename__ = "fake_annotations"
    id = Column(Integer, primary_key=True, autoincrement=True)
    short_descr = Column(String(500), nullable=False)
    layer_id = Column(Integer, ForeignKey("fake_annotation_layers.id"), nullable=False)


class FakeAnnotationLayerDAO(BaseAsyncDAO[FakeAnnotationLayer]):
    model_cls = FakeAnnotationLayer

    async def validate_update_uniqueness(
        self, name: str, layer_id: int | None = None
    ) -> bool:
        stmt = select(FakeAnnotationLayer).where(FakeAnnotationLayer.name == name)
        if layer_id is not None:
            stmt = stmt.where(FakeAnnotationLayer.id != layer_id)
        result = await self.session.execute(stmt)
        return result.scalars().one_or_none() is None

    async def has_annotations(self, model_id: int | list[int]) -> bool:
        if isinstance(model_id, list):
            stmt = (
                select(FakeAnnotation.id)
                .where(FakeAnnotation.layer_id.in_(model_id))
                .limit(1)
            )
        else:
            stmt = (
                select(FakeAnnotation.id)
                .where(FakeAnnotation.layer_id == model_id)
                .limit(1)
            )
        result = await self.session.execute(stmt)
        return result.scalars().first() is not None


class FakeAnnotationDAO(BaseAsyncDAO[FakeAnnotation]):
    model_cls = FakeAnnotation

    async def validate_update_uniqueness(
        self, layer_id: int, short_descr: str, annotation_id: int | None = None
    ) -> bool:
        stmt = select(FakeAnnotation).where(
            FakeAnnotation.short_descr == short_descr,
            FakeAnnotation.layer_id == layer_id,
        )
        if annotation_id is not None:
            stmt = stmt.where(FakeAnnotation.id != annotation_id)
        result = await self.session.execute(stmt)
        return result.scalars().one_or_none() is None


@pytest.fixture
async def async_session():
    from tests.liteset.conftest import create_test_session

    async with create_test_session(Base) as session:
        yield session


async def test_create_layer(async_session: AsyncSession) -> None:
    dao = FakeAnnotationLayerDAO(async_session)
    layer = await dao.create({"name": "Layer 1"})
    await async_session.flush()
    assert layer.id is not None


async def test_validate_layer_uniqueness(async_session: AsyncSession) -> None:
    dao = FakeAnnotationLayerDAO(async_session)
    layer = await dao.create({"name": "Unique Layer"})
    await async_session.flush()

    assert await dao.validate_update_uniqueness("Unique Layer") is False
    assert await dao.validate_update_uniqueness("Unique Layer", layer.id) is True
    assert await dao.validate_update_uniqueness("New Layer") is True


async def test_has_annotations_true(async_session: AsyncSession) -> None:
    layer_dao = FakeAnnotationLayerDAO(async_session)
    layer = await layer_dao.create({"name": "Has Annots"})
    await async_session.flush()

    ann = FakeAnnotation(short_descr="Test", layer_id=layer.id)
    async_session.add(ann)
    await async_session.flush()

    assert await layer_dao.has_annotations(layer.id) is True


async def test_has_annotations_false(async_session: AsyncSession) -> None:
    layer_dao = FakeAnnotationLayerDAO(async_session)
    layer = await layer_dao.create({"name": "Empty"})
    await async_session.flush()

    assert await layer_dao.has_annotations(layer.id) is False


async def test_has_annotations_list(async_session: AsyncSession) -> None:
    layer_dao = FakeAnnotationLayerDAO(async_session)
    l1 = await layer_dao.create({"name": "L1"})
    l2 = await layer_dao.create({"name": "L2"})
    await async_session.flush()

    ann = FakeAnnotation(short_descr="Test", layer_id=l1.id)
    async_session.add(ann)
    await async_session.flush()

    assert await layer_dao.has_annotations([l1.id, l2.id]) is True
    assert await layer_dao.has_annotations([l2.id]) is False


async def test_annotation_uniqueness(async_session: AsyncSession) -> None:
    layer_dao = FakeAnnotationLayerDAO(async_session)
    layer = await layer_dao.create({"name": "L"})
    await async_session.flush()

    ann_dao = FakeAnnotationDAO(async_session)
    ann = await ann_dao.create({"short_descr": "Existing", "layer_id": layer.id})
    await async_session.flush()

    assert await ann_dao.validate_update_uniqueness(layer.id, "Existing") is False
    assert (
        await ann_dao.validate_update_uniqueness(layer.id, "Existing", ann.id) is True
    )
    assert await ann_dao.validate_update_uniqueness(layer.id, "New") is True
