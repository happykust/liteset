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
"""Tests for AsyncDatasetDAO using simplified test models."""

from __future__ import annotations

import pytest
from sqlalchemy import Column, ForeignKey, Integer, select, String
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase, relationship

from superset.db.base_dao import BaseAsyncDAO


class Base(DeclarativeBase):
    pass


class FakeDatabase(Base):
    __tablename__ = "test_databases"
    id = Column(Integer, primary_key=True, autoincrement=True)
    database_name = Column(String(250), nullable=False)


class FakeDataset(Base):
    __tablename__ = "test_datasets"
    id = Column(Integer, primary_key=True, autoincrement=True)
    table_name = Column(String(250), nullable=False)
    schema = Column(String(255), nullable=True)
    catalog = Column(String(256), nullable=True)
    database_id = Column(Integer, ForeignKey("test_databases.id"), nullable=False)
    columns = relationship(
        "FakeColumn", back_populates="dataset", cascade="all, delete-orphan"
    )
    metrics = relationship(
        "FakeMetric", back_populates="dataset", cascade="all, delete-orphan"
    )


class FakeColumn(Base):
    __tablename__ = "test_columns"
    id = Column(Integer, primary_key=True, autoincrement=True)
    column_name = Column(String(255), nullable=False)
    table_id = Column(Integer, ForeignKey("test_datasets.id"), nullable=False)
    dataset = relationship("FakeDataset", back_populates="columns")


class FakeMetric(Base):
    __tablename__ = "test_metrics"
    id = Column(Integer, primary_key=True, autoincrement=True)
    metric_name = Column(String(255), nullable=False)
    expression = Column(String(1024), nullable=True)
    table_id = Column(Integer, ForeignKey("test_datasets.id"), nullable=False)
    dataset = relationship("FakeDataset", back_populates="metrics")


class FakeDatasetDAO(BaseAsyncDAO[FakeDataset]):
    model_cls = FakeDataset

    async def get_database_by_id(self, database_id: int) -> FakeDatabase | None:
        return await self.session.get(FakeDatabase, database_id)

    async def validate_uniqueness(
        self,
        database_id: int,
        table_name: str,
        schema: str | None = None,
        dataset_id: int | None = None,
    ) -> bool:
        stmt = select(FakeDataset).where(
            FakeDataset.table_name == table_name,
            FakeDataset.database_id == database_id,
        )
        if schema is not None:
            stmt = stmt.where(FakeDataset.schema == schema)
        if dataset_id is not None:
            stmt = stmt.where(FakeDataset.id != dataset_id)
        result = await self.session.execute(stmt)
        return result.scalars().one_or_none() is None

    async def validate_columns_exist(
        self, dataset_id: int, column_ids: list[int]
    ) -> bool:
        if not column_ids:
            return True
        stmt = select(FakeColumn.id).where(
            FakeColumn.table_id == dataset_id,
            FakeColumn.id.in_(column_ids),
        )
        result = await self.session.execute(stmt)
        found = set(result.scalars().all())
        return found >= set(column_ids)

    async def validate_metrics_exist(
        self, dataset_id: int, metric_ids: list[int]
    ) -> bool:
        if not metric_ids:
            return True
        stmt = select(FakeMetric.id).where(
            FakeMetric.table_id == dataset_id,
            FakeMetric.id.in_(metric_ids),
        )
        result = await self.session.execute(stmt)
        found = set(result.scalars().all())
        return found >= set(metric_ids)

    async def update_columns(
        self, model: FakeDataset, property_columns: list[dict]
    ) -> None:
        stmt = select(FakeColumn).where(FakeColumn.table_id == model.id)
        result = await self.session.execute(stmt)
        existing = {col.id: col for col in result.scalars().all()}
        incoming_ids = set()
        for col_data in property_columns:
            col_id = col_data.get("id")
            if col_id and col_id in existing:
                col = existing[col_id]
                for k, v in col_data.items():
                    if k != "id":
                        setattr(col, k, v)
                incoming_ids.add(col_id)
            else:
                col_data_copy = {k: v for k, v in col_data.items() if k != "id"}
                col_data_copy["table_id"] = model.id
                self.session.add(FakeColumn(**col_data_copy))
        for col_id, col in existing.items():
            if col_id not in incoming_ids:
                await self.session.delete(col)

    async def update_metrics(
        self, model: FakeDataset, property_metrics: list[dict]
    ) -> None:
        stmt = select(FakeMetric).where(FakeMetric.table_id == model.id)
        result = await self.session.execute(stmt)
        existing = {m.id: m for m in result.scalars().all()}
        incoming_ids = set()
        for m_data in property_metrics:
            m_id = m_data.get("id")
            if m_id and m_id in existing:
                metric = existing[m_id]
                for k, v in m_data.items():
                    if k != "id":
                        setattr(metric, k, v)
                incoming_ids.add(m_id)
            else:
                m_data_copy = {k: v for k, v in m_data.items() if k != "id"}
                m_data_copy["table_id"] = model.id
                self.session.add(FakeMetric(**m_data_copy))
        for m_id, metric in existing.items():
            if m_id not in incoming_ids:
                await self.session.delete(metric)


@pytest.fixture
async def async_session():
    from tests.superset.conftest import create_test_session

    async with create_test_session(Base) as session:
        yield session


@pytest.fixture
async def test_db(async_session: AsyncSession) -> FakeDatabase:
    db = FakeDatabase(database_name="test_pg")
    async_session.add(db)
    await async_session.flush()
    return db


async def test_create_dataset(
    async_session: AsyncSession, test_db: FakeDatabase
) -> None:
    dao = FakeDatasetDAO(async_session)
    ds = await dao.create(
        {
            "table_name": "events",
            "database_id": test_db.id,
        }
    )
    await async_session.flush()
    assert ds.id is not None
    assert ds.table_name == "events"


async def test_get_database_by_id(
    async_session: AsyncSession, test_db: FakeDatabase
) -> None:
    dao = FakeDatasetDAO(async_session)
    found = await dao.get_database_by_id(test_db.id)
    assert found is not None
    assert found.database_name == "test_pg"


async def test_get_database_by_id_not_found(async_session: AsyncSession) -> None:
    dao = FakeDatasetDAO(async_session)
    found = await dao.get_database_by_id(9999)
    assert found is None


async def test_validate_uniqueness_unique(
    async_session: AsyncSession, test_db: FakeDatabase
) -> None:
    dao = FakeDatasetDAO(async_session)
    assert await dao.validate_uniqueness(test_db.id, "new_table") is True


async def test_validate_uniqueness_duplicate(
    async_session: AsyncSession, test_db: FakeDatabase
) -> None:
    dao = FakeDatasetDAO(async_session)
    await dao.create({"table_name": "events", "database_id": test_db.id})
    await async_session.flush()
    assert await dao.validate_uniqueness(test_db.id, "events") is False


async def test_validate_uniqueness_exclude_id(
    async_session: AsyncSession, test_db: FakeDatabase
) -> None:
    dao = FakeDatasetDAO(async_session)
    ds = await dao.create({"table_name": "events", "database_id": test_db.id})
    await async_session.flush()
    # Excluding self should pass
    assert await dao.validate_uniqueness(test_db.id, "events", dataset_id=ds.id) is True


async def test_validate_columns_exist(
    async_session: AsyncSession, test_db: FakeDatabase
) -> None:
    dao = FakeDatasetDAO(async_session)
    ds = await dao.create({"table_name": "t", "database_id": test_db.id})
    await async_session.flush()

    col = FakeColumn(column_name="col1", table_id=ds.id)
    async_session.add(col)
    await async_session.flush()

    assert await dao.validate_columns_exist(ds.id, [col.id]) is True
    assert await dao.validate_columns_exist(ds.id, [col.id, 9999]) is False
    assert await dao.validate_columns_exist(ds.id, []) is True


async def test_validate_columns_exist_with_duplicates(
    async_session: AsyncSession, test_db: FakeDatabase
) -> None:
    dao = FakeDatasetDAO(async_session)
    ds = await dao.create({"table_name": "t", "database_id": test_db.id})
    await async_session.flush()

    col = FakeColumn(column_name="col1", table_id=ds.id)
    async_session.add(col)
    await async_session.flush()

    # Duplicate IDs in input should still return True if the column exists
    assert await dao.validate_columns_exist(ds.id, [col.id, col.id]) is True


async def test_validate_metrics_exist(
    async_session: AsyncSession, test_db: FakeDatabase
) -> None:
    dao = FakeDatasetDAO(async_session)
    ds = await dao.create({"table_name": "t", "database_id": test_db.id})
    await async_session.flush()

    m = FakeMetric(metric_name="count_star", expression="COUNT(*)", table_id=ds.id)
    async_session.add(m)
    await async_session.flush()

    assert await dao.validate_metrics_exist(ds.id, [m.id]) is True
    assert await dao.validate_metrics_exist(ds.id, [9999]) is False


async def test_update_columns_add_and_remove(
    async_session: AsyncSession, test_db: FakeDatabase
) -> None:
    dao = FakeDatasetDAO(async_session)
    ds = await dao.create({"table_name": "t", "database_id": test_db.id})
    await async_session.flush()

    # Add initial columns
    c1 = FakeColumn(column_name="col1", table_id=ds.id)
    c2 = FakeColumn(column_name="col2", table_id=ds.id)
    async_session.add_all([c1, c2])
    await async_session.flush()

    # Update: keep c1 (renamed), remove c2, add c3
    await dao.update_columns(
        ds,
        [
            {"id": c1.id, "column_name": "col1_renamed"},
            {"column_name": "col3"},
        ],
    )
    await async_session.flush()

    stmt = select(FakeColumn).where(FakeColumn.table_id == ds.id)
    result = await async_session.execute(stmt)
    cols = list(result.scalars().all())
    names = {c.column_name for c in cols}
    assert "col1_renamed" in names
    assert "col3" in names
    assert "col2" not in names
    assert len(cols) == 2


async def test_update_metrics_add_and_remove(
    async_session: AsyncSession, test_db: FakeDatabase
) -> None:
    dao = FakeDatasetDAO(async_session)
    ds = await dao.create({"table_name": "t", "database_id": test_db.id})
    await async_session.flush()

    m1 = FakeMetric(metric_name="m1", expression="SUM(x)", table_id=ds.id)
    async_session.add(m1)
    await async_session.flush()

    # Replace m1 with m2
    await dao.update_metrics(ds, [{"metric_name": "m2", "expression": "AVG(x)"}])
    await async_session.flush()

    stmt = select(FakeMetric).where(FakeMetric.table_id == ds.id)
    result = await async_session.execute(stmt)
    metrics = list(result.scalars().all())
    assert len(metrics) == 1
    assert metrics[0].metric_name == "m2"
