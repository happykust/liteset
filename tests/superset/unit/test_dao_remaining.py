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
"""Tests for remaining async DAOs using simplified test models."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import Boolean, Column, DateTime, delete, Integer, LargeBinary, String
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase

from superset.db.base_dao import BaseAsyncDAO


class Base(DeclarativeBase):
    pass


# --- CssTemplate ---
class FakeCssTemplate(Base):
    __tablename__ = "fake_css_templates"
    id = Column(Integer, primary_key=True, autoincrement=True)
    template_name = Column(String(250), nullable=False)
    css = Column(String(2000), nullable=True)


class FakeCssTemplateDAO(BaseAsyncDAO[FakeCssTemplate]):
    model_cls = FakeCssTemplate


# --- Datasource ---
class FakeDatasource(Base):
    __tablename__ = "fake_datasources"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(250), nullable=False)
    ds_type = Column(String(50), nullable=False)


class FakeDatasourceDAO:
    _type_map = {"table": FakeDatasource}

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_datasource(self, ds_type: str, ds_id: int) -> FakeDatasource | None:
        model_cls = self._type_map.get(ds_type)
        if model_cls is None:
            raise ValueError(f"Unknown type: {ds_type}")
        return await self.session.get(model_cls, ds_id)


# --- KeyValue ---
class FakeKeyValue(Base):
    __tablename__ = "fake_key_values"
    id = Column(Integer, primary_key=True, autoincrement=True)
    resource = Column(String(250), nullable=False)
    value = Column(LargeBinary, nullable=True)
    expires_on = Column(DateTime, nullable=True)


class FakeKeyValueDAO(BaseAsyncDAO[FakeKeyValue]):
    model_cls = FakeKeyValue

    async def get_entry(self, resource: str, entry_id: int) -> FakeKeyValue | None:
        from sqlalchemy import or_, select

        stmt = select(FakeKeyValue).where(
            FakeKeyValue.resource == resource,
            FakeKeyValue.id == entry_id,
            or_(
                FakeKeyValue.expires_on.is_(None),
                FakeKeyValue.expires_on > datetime.now(tz=timezone.utc),
            ),
        )
        result = await self.session.execute(stmt)
        return result.scalars().one_or_none()

    async def create_entry(
        self, resource: str, value: bytes, expires_on: datetime | None = None
    ) -> FakeKeyValue:
        return await self.create(
            {
                "resource": resource,
                "value": value,
                "expires_on": expires_on,
            }
        )

    async def upsert_entry(
        self,
        resource: str,
        entry_id: int,
        value: bytes,
        expires_on: datetime | None = None,
    ) -> FakeKeyValue:
        existing = await self.get_entry(resource, entry_id)
        if existing:
            return await self.update(
                existing, {"value": value, "expires_on": expires_on}
            )
        return await self.create_entry(resource, value, expires_on)

    async def delete_expired_entries(self, resource: str) -> None:
        from datetime import timezone

        stmt = delete(FakeKeyValue).where(
            FakeKeyValue.resource == resource,
            FakeKeyValue.expires_on <= datetime.now(tz=timezone.utc),
        )
        await self.session.execute(stmt)

    async def delete_entry(self, resource: str, entry_id: int) -> bool:
        entry = await self.get_entry(resource, entry_id)
        if entry:
            await self.delete([entry])
            return True
        return False


# --- Log ---
class FakeLog(Base):
    __tablename__ = "fake_logs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False)
    action = Column(String(250), nullable=False)
    dttm = Column(DateTime, default=datetime.now)


class FakeLogDAO(BaseAsyncDAO[FakeLog]):
    model_cls = FakeLog

    async def get_recent_activity(
        self, user_id: int, actions: list[str], page: int = 0, page_size: int = 25
    ) -> list[FakeLog]:
        from sqlalchemy import select

        stmt = (
            select(FakeLog)
            .where(FakeLog.user_id == user_id, FakeLog.action.in_(actions))
            .order_by(FakeLog.dttm.desc())
            .offset(page * page_size)
            .limit(page_size)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())


# --- Theme ---
class FakeTheme(Base):
    __tablename__ = "fake_themes"
    id = Column(Integer, primary_key=True, autoincrement=True)
    uuid = Column(String(36), unique=True, nullable=True)
    theme_name = Column(String(250), nullable=False)
    is_system_default = Column(Boolean, default=False)
    is_system = Column(Boolean, default=False)


class FakeThemeDAO(BaseAsyncDAO[FakeTheme]):
    model_cls = FakeTheme

    async def find_by_uuid(self, uuid_str: str) -> FakeTheme | None:
        try:
            uuid.UUID(uuid_str)
        except ValueError:
            return None
        return await self.find_one_or_none(uuid=uuid_str)

    async def find_system_default(self) -> FakeTheme | None:
        from sqlalchemy import select

        stmt = select(FakeTheme).where(FakeTheme.is_system_default.is_(True))
        result = await self.session.execute(stmt)
        theme = result.scalars().one_or_none()
        if theme:
            return theme
        stmt = select(FakeTheme).where(
            FakeTheme.is_system.is_(True),
            FakeTheme.theme_name == "THEME_DEFAULT",
        )
        result = await self.session.execute(stmt)
        return result.scalars().one_or_none()


# --- User ---
class FakeUser(Base):
    __tablename__ = "fake_users"
    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(250), nullable=False)
    avatar_url = Column(String(500), nullable=True)


class FakeUserDAO:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, user_id: int) -> FakeUser | None:
        return await self.session.get(FakeUser, user_id)

    async def set_avatar_url(self, user: FakeUser, url: str) -> None:
        user.avatar_url = url


# --- Security (RLS) ---
class FakeRLS(Base):
    __tablename__ = "fake_rls"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(250), nullable=False)
    clause = Column(String(1000), nullable=True)


class FakeSecurityDAO(BaseAsyncDAO[FakeRLS]):
    model_cls = FakeRLS


# ============ Fixtures ============


@pytest.fixture
async def async_session():
    from tests.superset.conftest import create_test_session

    async with create_test_session(Base) as session:
        yield session


# ============ Tests ============


# --- CssTemplate ---
async def test_css_template_crud(async_session: AsyncSession) -> None:
    dao = FakeCssTemplateDAO(async_session)
    item = await dao.create({"template_name": "dark", "css": "body { color: white }"})
    await async_session.flush()
    assert item.id is not None

    found = await dao.find_by_id(item.id)
    assert found is not None
    assert found.template_name == "dark"


# --- Datasource ---
async def test_datasource_get(async_session: AsyncSession) -> None:
    ds = FakeDatasource(name="events", ds_type="table")
    async_session.add(ds)
    await async_session.flush()

    dao = FakeDatasourceDAO(async_session)
    found = await dao.get_datasource("table", ds.id)
    assert found is not None
    assert found.name == "events"


async def test_datasource_get_not_found(async_session: AsyncSession) -> None:
    dao = FakeDatasourceDAO(async_session)
    found = await dao.get_datasource("table", 9999)
    assert found is None


async def test_datasource_unknown_type(async_session: AsyncSession) -> None:
    dao = FakeDatasourceDAO(async_session)
    with pytest.raises(ValueError, match="Unknown type"):
        await dao.get_datasource("unknown", 1)


# --- KeyValue ---
async def test_kv_create_and_get(async_session: AsyncSession) -> None:
    dao = FakeKeyValueDAO(async_session)
    entry = await dao.create_entry("filter_state", b"data123")
    await async_session.flush()

    found = await dao.get_entry("filter_state", entry.id)
    assert found is not None
    assert found.value == b"data123"


async def test_kv_upsert_creates(async_session: AsyncSession) -> None:
    dao = FakeKeyValueDAO(async_session)
    entry = await dao.create_entry("cache", b"v1")
    await async_session.flush()
    assert entry.value == b"v1"


async def test_kv_upsert_updates(async_session: AsyncSession) -> None:
    dao = FakeKeyValueDAO(async_session)
    entry = await dao.create_entry("cache", b"v1")
    await async_session.flush()

    updated = await dao.upsert_entry("cache", entry.id, b"v2")
    await async_session.flush()
    assert updated.value == b"v2"


async def test_kv_delete(async_session: AsyncSession) -> None:
    dao = FakeKeyValueDAO(async_session)
    entry = await dao.create_entry("cache", b"x")
    await async_session.flush()

    assert await dao.delete_entry("cache", entry.id) is True
    await async_session.flush()
    assert await dao.get_entry("cache", entry.id) is None

    assert await dao.delete_entry("cache", 99999) is False


async def test_kv_delete_expired_entries(async_session: AsyncSession) -> None:
    from datetime import timezone

    dao = FakeKeyValueDAO(async_session)
    now = datetime.now(tz=timezone.utc)
    # Expired entry
    await dao.create_entry("cache", b"old", expires_on=now - timedelta(hours=1))
    # Valid entry
    valid = await dao.create_entry(
        "cache", b"fresh", expires_on=now + timedelta(hours=1)
    )
    # No expiry entry
    no_exp = await dao.create_entry("cache", b"forever", expires_on=None)
    await async_session.flush()

    await dao.delete_expired_entries("cache")
    await async_session.flush()

    # Expired entry should be gone, valid and no-expiry should remain
    from sqlalchemy import select

    stmt = select(FakeKeyValue).where(FakeKeyValue.resource == "cache")
    result = await async_session.execute(stmt)
    remaining = list(result.scalars().all())
    remaining_ids = {r.id for r in remaining}
    assert valid.id in remaining_ids
    assert no_exp.id in remaining_ids
    assert len(remaining) == 2


# --- Log ---
async def test_log_recent_activity(async_session: AsyncSession) -> None:
    dao = FakeLogDAO(async_session)
    await dao.create(
        {
            "user_id": 1,
            "action": "dashboard_view",
            "dttm": datetime.now(tz=timezone.utc),
        }
    )
    await dao.create(
        {"user_id": 1, "action": "chart_view", "dttm": datetime.now(tz=timezone.utc)}
    )
    await dao.create(
        {
            "user_id": 2,
            "action": "dashboard_view",
            "dttm": datetime.now(tz=timezone.utc),
        }
    )
    await async_session.flush()

    results = await dao.get_recent_activity(
        user_id=1, actions=["dashboard_view", "chart_view"]
    )
    assert len(results) == 2

    results = await dao.get_recent_activity(user_id=1, actions=["dashboard_view"])
    assert len(results) == 1


async def test_log_pagination(async_session: AsyncSession) -> None:
    dao = FakeLogDAO(async_session)
    for i in range(10):
        await dao.create(
            {
                "user_id": 1,
                "action": "view",
                "dttm": datetime.now(tz=timezone.utc) - timedelta(minutes=i * 1),
            }
        )
    await async_session.flush()

    page0 = await dao.get_recent_activity(
        user_id=1, actions=["view"], page=0, page_size=3
    )
    page1 = await dao.get_recent_activity(
        user_id=1, actions=["view"], page=1, page_size=3
    )
    assert len(page0) == 3
    assert len(page1) == 3


# --- Theme ---
async def test_theme_find_by_uuid(async_session: AsyncSession) -> None:
    dao = FakeThemeDAO(async_session)
    theme_uuid = str(uuid.uuid4())
    await dao.create({"theme_name": "Custom", "uuid": theme_uuid})
    await async_session.flush()

    found = await dao.find_by_uuid(theme_uuid)
    assert found is not None
    assert found.theme_name == "Custom"

    not_found = await dao.find_by_uuid("not-a-uuid")
    assert not_found is None


async def test_theme_find_system_default(async_session: AsyncSession) -> None:
    dao = FakeThemeDAO(async_session)
    await dao.create({"theme_name": "Default", "is_system_default": True})
    await async_session.flush()

    found = await dao.find_system_default()
    assert found is not None
    assert found.theme_name == "Default"


async def test_theme_find_system_default_fallback(async_session: AsyncSession) -> None:
    dao = FakeThemeDAO(async_session)
    await dao.create(
        {
            "theme_name": "THEME_DEFAULT",
            "is_system": True,
            "is_system_default": False,
        }
    )
    await async_session.flush()

    found = await dao.find_system_default()
    assert found is not None
    assert found.theme_name == "THEME_DEFAULT"


# --- User ---
async def test_user_get_by_id(async_session: AsyncSession) -> None:
    user = FakeUser(username="alice")
    async_session.add(user)
    await async_session.flush()

    dao = FakeUserDAO(async_session)
    found = await dao.get_by_id(user.id)
    assert found is not None
    assert found.username == "alice"


async def test_user_set_avatar(async_session: AsyncSession) -> None:
    user = FakeUser(username="bob")
    async_session.add(user)
    await async_session.flush()

    dao = FakeUserDAO(async_session)
    await dao.set_avatar_url(user, "https://example.com/avatar.png")
    assert user.avatar_url == "https://example.com/avatar.png"


# --- Security (RLS) ---
async def test_kv_get_entry_expired_returns_none(async_session: AsyncSession) -> None:
    dao = FakeKeyValueDAO(async_session)
    now = datetime.now(tz=timezone.utc)
    entry = await dao.create_entry(
        "cache", b"expired", expires_on=now - timedelta(hours=1)
    )
    await async_session.flush()

    found = await dao.get_entry("cache", entry.id)
    assert found is None


async def test_kv_get_entry_not_expired(async_session: AsyncSession) -> None:
    dao = FakeKeyValueDAO(async_session)
    now = datetime.now(tz=timezone.utc)
    entry = await dao.create_entry(
        "cache", b"valid", expires_on=now + timedelta(hours=1)
    )
    await async_session.flush()

    found = await dao.get_entry("cache", entry.id)
    assert found is not None
    assert found.value == b"valid"


async def test_kv_get_entry_no_expiry(async_session: AsyncSession) -> None:
    dao = FakeKeyValueDAO(async_session)
    entry = await dao.create_entry("cache", b"forever", expires_on=None)
    await async_session.flush()

    found = await dao.get_entry("cache", entry.id)
    assert found is not None


# --- Embedded Dashboard ---
class FakeEmbeddedDashboard(Base):
    __tablename__ = "fake_embedded_dashboards"
    id = Column(Integer, primary_key=True, autoincrement=True)
    uuid = Column(String(36), unique=True, nullable=True)
    dashboard_id = Column(Integer, nullable=False)


class FakeEmbeddedDashboardDAO(BaseAsyncDAO[FakeEmbeddedDashboard]):
    model_cls = FakeEmbeddedDashboard

    async def find_by_dashboard_id(
        self, dashboard_id: int
    ) -> FakeEmbeddedDashboard | None:
        return await self.find_one_or_none(dashboard_id=dashboard_id)

    async def find_by_uuid(self, uuid_str: str) -> FakeEmbeddedDashboard | None:
        try:
            uuid.UUID(uuid_str)
        except ValueError:
            return None
        return await self.find_one_or_none(uuid=uuid_str)


async def test_embedded_dashboard_find_by_dashboard_id(
    async_session: AsyncSession,
) -> None:
    dao = FakeEmbeddedDashboardDAO(async_session)
    ed = FakeEmbeddedDashboard(dashboard_id=42)
    async_session.add(ed)
    await async_session.flush()

    found = await dao.find_by_dashboard_id(42)
    assert found is not None

    not_found = await dao.find_by_dashboard_id(999)
    assert not_found is None


async def test_embedded_dashboard_find_by_uuid(async_session: AsyncSession) -> None:
    dao = FakeEmbeddedDashboardDAO(async_session)
    ed_uuid = str(uuid.uuid4())
    ed = FakeEmbeddedDashboard(dashboard_id=42, uuid=ed_uuid)
    async_session.add(ed)
    await async_session.flush()

    found = await dao.find_by_uuid(ed_uuid)
    assert found is not None

    assert await dao.find_by_uuid("not-a-uuid") is None


# --- SSH Tunnel ---
class FakeSSHTunnel(Base):
    __tablename__ = "fake_ssh_tunnels"
    id = Column(Integer, primary_key=True, autoincrement=True)
    database_id = Column(Integer, nullable=False)


class FakeSSHTunnelDAO:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_database_id(self, database_id: int) -> FakeSSHTunnel | None:
        from sqlalchemy import select

        stmt = select(FakeSSHTunnel).where(FakeSSHTunnel.database_id == database_id)
        result = await self.session.execute(stmt)
        return result.scalars().one_or_none()


async def test_ssh_tunnel_get_by_database_id(async_session: AsyncSession) -> None:
    dao = FakeSSHTunnelDAO(async_session)
    tunnel = FakeSSHTunnel(database_id=100)
    async_session.add(tunnel)
    await async_session.flush()

    found = await dao.get_by_database_id(100)
    assert found is not None

    not_found = await dao.get_by_database_id(999)
    assert not_found is None


async def test_rls_crud(async_session: AsyncSession) -> None:
    dao = FakeSecurityDAO(async_session)
    rls = await dao.create({"name": "dept_filter", "clause": "dept_id = 1"})
    await async_session.flush()

    found = await dao.find_by_id(rls.id)
    assert found is not None
    assert found.clause == "dept_id = 1"
