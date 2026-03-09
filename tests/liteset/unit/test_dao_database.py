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
"""Tests for AsyncDatabaseDAO using simplified test models."""
from __future__ import annotations

import pytest
from sqlalchemy import Column, ForeignKey, Integer, String, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase

from liteset.db.base_dao import BaseAsyncDAO


class Base(DeclarativeBase):
    pass


class FakeDatabase(Base):
    __tablename__ = "fake_databases"
    id = Column(Integer, primary_key=True, autoincrement=True)
    database_name = Column(String(250), unique=True, nullable=False)


class FakeSSHTunnel(Base):
    __tablename__ = "fake_ssh_tunnels"
    id = Column(Integer, primary_key=True, autoincrement=True)
    database_id = Column(Integer, ForeignKey("fake_databases.id"), nullable=False)
    server_address = Column(String(250), nullable=True)


class FakeDataset(Base):
    __tablename__ = "fake_datasets"
    id = Column(Integer, primary_key=True, autoincrement=True)
    table_name = Column(String(250), nullable=False)
    database_id = Column(Integer, ForeignKey("fake_databases.id"), nullable=False)


class FakeDatabaseDAO(BaseAsyncDAO[FakeDatabase]):
    model_cls = FakeDatabase

    async def validate_uniqueness(self, database_name: str) -> bool:
        existing = await self.find_one_or_none(database_name=database_name)
        return existing is None

    async def validate_update_uniqueness(
        self, database_id: int, database_name: str
    ) -> bool:
        stmt = select(FakeDatabase).where(
            FakeDatabase.database_name == database_name,
            FakeDatabase.id != database_id,
        )
        result = await self.session.execute(stmt)
        return result.scalars().one_or_none() is None

    async def get_database_by_name(self, database_name: str) -> FakeDatabase | None:
        return await self.find_one_or_none(database_name=database_name)

    async def get_ssh_tunnel(self, database_id: int) -> FakeSSHTunnel | None:
        stmt = select(FakeSSHTunnel).where(FakeSSHTunnel.database_id == database_id)
        result = await self.session.execute(stmt)
        return result.scalars().one_or_none()

    @staticmethod
    def build_db_for_connection_test(
        server_cert: str = "",
        extra: str = "",
        impersonate_user: bool = False,
        encrypted_extra: str = "",
    ) -> FakeDatabase:
        return FakeDatabase(
            database_name=f"_test_{server_cert}",
        )

    async def get_datasets(
        self, database_id: int, schema: str | None = None
    ) -> list[FakeDataset]:
        stmt = select(FakeDataset).where(FakeDataset.database_id == database_id)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())


@pytest.fixture
async def async_session():
    from tests.liteset.conftest import create_test_session

    async with create_test_session(Base) as session:
        yield session


async def test_create_database(async_session: AsyncSession) -> None:
    dao = FakeDatabaseDAO(async_session)
    db = await dao.create({"database_name": "test_pg"})
    await async_session.flush()
    assert db.id is not None


async def test_validate_uniqueness(async_session: AsyncSession) -> None:
    dao = FakeDatabaseDAO(async_session)
    assert await dao.validate_uniqueness("new_db") is True
    await dao.create({"database_name": "existing"})
    await async_session.flush()
    assert await dao.validate_uniqueness("existing") is False


async def test_validate_update_uniqueness(async_session: AsyncSession) -> None:
    dao = FakeDatabaseDAO(async_session)
    d1 = await dao.create({"database_name": "db1"})
    d2 = await dao.create({"database_name": "db2"})
    await async_session.flush()

    assert await dao.validate_update_uniqueness(d1.id, "db1") is True
    assert await dao.validate_update_uniqueness(d1.id, "db2") is False


async def test_get_database_by_name(async_session: AsyncSession) -> None:
    dao = FakeDatabaseDAO(async_session)
    await dao.create({"database_name": "findme"})
    await async_session.flush()

    found = await dao.get_database_by_name("findme")
    assert found is not None
    assert found.database_name == "findme"

    not_found = await dao.get_database_by_name("nope")
    assert not_found is None


async def test_get_datasets(async_session: AsyncSession) -> None:
    dao = FakeDatabaseDAO(async_session)
    db = await dao.create({"database_name": "withdata"})
    await async_session.flush()

    ds1 = FakeDataset(table_name="events", database_id=db.id)
    ds2 = FakeDataset(table_name="users", database_id=db.id)
    async_session.add_all([ds1, ds2])
    await async_session.flush()

    datasets = await dao.get_datasets(db.id)
    assert len(datasets) == 2


async def test_get_ssh_tunnel(async_session: AsyncSession) -> None:
    dao = FakeDatabaseDAO(async_session)
    db = await dao.create({"database_name": "with_tunnel"})
    await async_session.flush()

    tunnel = FakeSSHTunnel(database_id=db.id, server_address="bastion.example.com")
    async_session.add(tunnel)
    await async_session.flush()

    found = await dao.get_ssh_tunnel(db.id)
    assert found is not None
    assert found.server_address == "bastion.example.com"


async def test_get_ssh_tunnel_not_found(async_session: AsyncSession) -> None:
    dao = FakeDatabaseDAO(async_session)
    db = await dao.create({"database_name": "no_tunnel"})
    await async_session.flush()

    found = await dao.get_ssh_tunnel(db.id)
    assert found is None


async def test_build_db_for_connection_test() -> None:
    db = FakeDatabaseDAO.build_db_for_connection_test(server_cert="cert.pem")
    assert db is not None
    assert db.database_name == "_test_cert.pem"
