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
"""Tests for AsyncDashboardDAO using simplified test models."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase

from superset.db.base_dao import BaseAsyncDAO


class Base(DeclarativeBase):
    pass


class FakeDashboard(Base):
    __tablename__ = "test_dashboards"
    id = Column(Integer, primary_key=True, autoincrement=True)
    uuid = Column(String(36), unique=True, nullable=True)
    dashboard_title = Column(String(500), nullable=False)
    slug = Column(String(255), unique=True, nullable=True)
    position_json = Column(Text, nullable=True)
    json_metadata = Column(Text, nullable=True)
    css = Column(Text, nullable=True)
    published = Column(Boolean, default=False)
    changed_on = Column(DateTime, default=datetime.now)


class FakeFavStar(Base):
    __tablename__ = "test_favstar"
    id = Column(Integer, primary_key=True, autoincrement=True)
    class_name = Column(String(50), nullable=False)
    obj_id = Column(Integer, nullable=False)
    user_id = Column(Integer, nullable=False)
    dttm = Column(DateTime, nullable=True)


class FakeEmbeddedDashboard(Base):
    __tablename__ = "test_embedded_dashboards"
    id = Column(Integer, primary_key=True, autoincrement=True)
    uuid = Column(String(36), unique=True, nullable=True)
    dashboard_id = Column(Integer, nullable=False)
    allowed_domains = Column(Text, nullable=True)


class FakeEmbeddedDashboardDAO(BaseAsyncDAO[FakeEmbeddedDashboard]):
    model_cls = FakeEmbeddedDashboard

    async def find_by_dashboard_id(
        self, dashboard_id: int
    ) -> FakeEmbeddedDashboard | None:
        return await self.find_one_or_none(dashboard_id=dashboard_id)

    async def upsert(
        self,
        dashboard_id: int,
        allowed_domains: str,
    ) -> FakeEmbeddedDashboard:
        existing = await self.find_by_dashboard_id(dashboard_id)
        if existing:
            existing.allowed_domains = allowed_domains
            return existing
        embedded = FakeEmbeddedDashboard(
            dashboard_id=dashboard_id,
            allowed_domains=allowed_domains,
        )
        self.session.add(embedded)
        return embedded


class FakeDashboardDAO(BaseAsyncDAO[FakeDashboard]):
    model_cls = FakeDashboard

    async def copy_dashboard(
        self, original: FakeDashboard, data: dict
    ) -> FakeDashboard:
        dash = FakeDashboard()
        dash.dashboard_title = data.get("dashboard_title", original.dashboard_title)
        dash.slug = data.get("slug", original.slug)
        dash.position_json = original.position_json
        dash.json_metadata = original.json_metadata
        dash.css = original.css
        dash.published = original.published
        dash.changed_on = datetime.now()
        self.session.add(dash)
        return dash

    async def get_by_id_or_slug(self, id_or_slug: int | str) -> FakeDashboard | None:
        try:
            dash_id = int(id_or_slug)
            return await self.find_by_id(dash_id)
        except (ValueError, TypeError):
            pass
        try:
            uuid.UUID(str(id_or_slug))
            result = await self.find_one_or_none(uuid=str(id_or_slug))
            if result:
                return result
        except ValueError:
            pass
        return await self.find_one_or_none(slug=str(id_or_slug))

    async def validate_slug_uniqueness(self, slug: str) -> bool:
        if not slug:
            return True
        existing = await self.find_one_or_none(slug=slug)
        return existing is None

    async def validate_update_slug_uniqueness(
        self, dashboard_id: int, slug: str | None
    ) -> bool:
        if slug is None:
            return True
        from sqlalchemy import select

        stmt = select(FakeDashboard).where(
            FakeDashboard.slug == slug,
            FakeDashboard.id != dashboard_id,
        )
        result = await self.session.execute(stmt)
        return result.scalars().one_or_none() is None

    async def set_dash_metadata(self, dashboard: FakeDashboard, data: dict) -> None:
        md = {}
        if dashboard.json_metadata:
            try:
                md = json.loads(dashboard.json_metadata)
            except (json.JSONDecodeError, TypeError):
                pass
        for key in ("color_scheme", "refresh_frequency", "native_filter_configuration"):
            if key in data:
                md[key] = data[key]
        dashboard.json_metadata = json.dumps(md)

    async def favorited_ids(self, dashboard_ids: list[int], user_id: int) -> list[int]:
        if not dashboard_ids:
            return []
        from sqlalchemy import select

        stmt = select(FakeFavStar.obj_id).where(
            FakeFavStar.class_name == "dashboard",
            FakeFavStar.obj_id.in_(dashboard_ids),
            FakeFavStar.user_id == user_id,
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def add_favorite(self, dashboard_id: int, user_id: int) -> None:
        existing = await self.favorited_ids([dashboard_id], user_id)
        if existing:
            return
        fav = FakeFavStar(
            class_name="dashboard",
            obj_id=dashboard_id,
            user_id=user_id,
            dttm=datetime.now(),
        )
        self.session.add(fav)

    async def remove_favorite(self, dashboard_id: int, user_id: int) -> None:
        from sqlalchemy import delete

        stmt = delete(FakeFavStar).where(
            FakeFavStar.class_name == "dashboard",
            FakeFavStar.obj_id == dashboard_id,
            FakeFavStar.user_id == user_id,
        )
        await self.session.execute(stmt)

    async def get_dashboard_changed_on(self, dashboard: FakeDashboard) -> datetime:
        changed_on = dashboard.changed_on
        if changed_on is None:
            return datetime.now(tz=timezone.utc).replace(microsecond=0)
        if changed_on.tzinfo is None:
            changed_on = changed_on.replace(tzinfo=timezone.utc)
        return changed_on.replace(microsecond=0)

    async def update_native_filters_config(
        self,
        dashboard: FakeDashboard,
        native_filter_configuration: list[dict],
    ) -> None:
        md = {}
        if dashboard.json_metadata:
            try:
                md = json.loads(dashboard.json_metadata)
            except (json.JSONDecodeError, TypeError):
                pass
        md["native_filter_configuration"] = native_filter_configuration
        dashboard.json_metadata = json.dumps(md)

    async def update_colors_config(
        self,
        dashboard: FakeDashboard,
        data: dict,
    ) -> None:
        md = {}
        if dashboard.json_metadata:
            try:
                md = json.loads(dashboard.json_metadata)
            except (json.JSONDecodeError, TypeError):
                pass
        for key in (
            "color_namespace",
            "color_scheme",
            "label_colors",
            "shared_label_colors",
            "color_scheme_domain",
        ):
            if key in data:
                md[key] = data[key]
        dashboard.json_metadata = json.dumps(md)


@pytest.fixture
async def async_session():
    from tests.superset.conftest import create_test_session

    async with create_test_session(Base) as session:
        yield session


async def test_create_dashboard(async_session: AsyncSession) -> None:
    dao = FakeDashboardDAO(async_session)
    dash = await dao.create(
        {"dashboard_title": "Test Dash", "changed_on": datetime.now()}
    )
    await async_session.flush()
    assert dash.id is not None


async def test_get_by_id_or_slug_by_id(async_session: AsyncSession) -> None:
    dao = FakeDashboardDAO(async_session)
    dash = await dao.create({"dashboard_title": "D1", "changed_on": datetime.now()})
    await async_session.flush()
    found = await dao.get_by_id_or_slug(dash.id)
    assert found is not None
    assert found.dashboard_title == "D1"


async def test_get_by_id_or_slug_by_slug(async_session: AsyncSession) -> None:
    dao = FakeDashboardDAO(async_session)
    await dao.create(
        {
            "dashboard_title": "Slugged",
            "slug": "my-dash",
            "changed_on": datetime.now(),
        }
    )
    await async_session.flush()
    found = await dao.get_by_id_or_slug("my-dash")
    assert found is not None
    assert found.dashboard_title == "Slugged"


async def test_get_by_id_or_slug_by_uuid(async_session: AsyncSession) -> None:
    dao = FakeDashboardDAO(async_session)
    dash_uuid = str(uuid.uuid4())
    await dao.create(
        {
            "dashboard_title": "UUID Dash",
            "uuid": dash_uuid,
            "changed_on": datetime.now(),
        }
    )
    await async_session.flush()
    found = await dao.get_by_id_or_slug(dash_uuid)
    assert found is not None
    assert found.dashboard_title == "UUID Dash"


async def test_validate_slug_uniqueness(async_session: AsyncSession) -> None:
    dao = FakeDashboardDAO(async_session)
    assert await dao.validate_slug_uniqueness("new-slug") is True
    await dao.create(
        {
            "dashboard_title": "D",
            "slug": "taken-slug",
            "changed_on": datetime.now(),
        }
    )
    await async_session.flush()
    assert await dao.validate_slug_uniqueness("taken-slug") is False


async def test_validate_slug_uniqueness_empty(async_session: AsyncSession) -> None:
    dao = FakeDashboardDAO(async_session)
    assert await dao.validate_slug_uniqueness("") is True


async def test_validate_update_slug_uniqueness(async_session: AsyncSession) -> None:
    dao = FakeDashboardDAO(async_session)
    d1 = await dao.create(
        {
            "dashboard_title": "D1",
            "slug": "slug-a",
            "changed_on": datetime.now(),
        }
    )
    await dao.create(
        {
            "dashboard_title": "D2",
            "slug": "slug-b",
            "changed_on": datetime.now(),
        }
    )
    await async_session.flush()

    # d1 can keep its own slug
    assert await dao.validate_update_slug_uniqueness(d1.id, "slug-a") is True
    # d1 cannot take d2's slug
    assert await dao.validate_update_slug_uniqueness(d1.id, "slug-b") is False
    # None slug always valid
    assert await dao.validate_update_slug_uniqueness(d1.id, None) is True


async def test_set_dash_metadata(async_session: AsyncSession) -> None:
    dao = FakeDashboardDAO(async_session)
    dash = await dao.create(
        {
            "dashboard_title": "Meta",
            "changed_on": datetime.now(),
        }
    )
    await async_session.flush()

    await dao.set_dash_metadata(
        dash,
        {
            "color_scheme": "supersetColors",
            "refresh_frequency": 30,
        },
    )

    md = json.loads(dash.json_metadata)
    assert md["color_scheme"] == "supersetColors"
    assert md["refresh_frequency"] == 30


async def test_set_dash_metadata_merges(async_session: AsyncSession) -> None:
    dao = FakeDashboardDAO(async_session)
    dash = await dao.create(
        {
            "dashboard_title": "Merge",
            "json_metadata": json.dumps({"existing_key": "value"}),
            "changed_on": datetime.now(),
        }
    )
    await async_session.flush()

    await dao.set_dash_metadata(dash, {"color_scheme": "blue"})
    md = json.loads(dash.json_metadata)
    assert md["existing_key"] == "value"
    assert md["color_scheme"] == "blue"


async def test_favorited_ids(async_session: AsyncSession) -> None:
    dao = FakeDashboardDAO(async_session)
    d1 = await dao.create({"dashboard_title": "D1", "changed_on": datetime.now()})
    d2 = await dao.create({"dashboard_title": "D2", "changed_on": datetime.now()})
    await async_session.flush()

    await dao.add_favorite(d1.id, user_id=1)
    await async_session.flush()

    favs = await dao.favorited_ids([d1.id, d2.id], user_id=1)
    assert d1.id in favs
    assert d2.id not in favs


async def test_remove_favorite(async_session: AsyncSession) -> None:
    dao = FakeDashboardDAO(async_session)
    dash = await dao.create({"dashboard_title": "RF", "changed_on": datetime.now()})
    await async_session.flush()

    await dao.add_favorite(dash.id, user_id=1)
    await async_session.flush()
    await dao.remove_favorite(dash.id, user_id=1)
    await async_session.flush()

    favs = await dao.favorited_ids([dash.id], user_id=1)
    assert len(favs) == 0


async def test_get_dashboard_changed_on(async_session: AsyncSession) -> None:
    dao = FakeDashboardDAO(async_session)
    now = datetime.now()
    dash = await dao.create({"dashboard_title": "CO", "changed_on": now})
    await async_session.flush()

    changed = await dao.get_dashboard_changed_on(dash)
    assert changed.microsecond == 0


async def test_copy_dashboard(async_session: AsyncSession) -> None:
    dao = FakeDashboardDAO(async_session)
    original = await dao.create(
        {
            "dashboard_title": "Original",
            "slug": "orig",
            "published": True,
            "changed_on": datetime.now(),
        }
    )
    await async_session.flush()

    copy = await dao.copy_dashboard(
        original,
        {
            "dashboard_title": "Copy",
            "slug": "copy-slug",
        },
    )
    await async_session.flush()

    assert copy.dashboard_title == "Copy"
    assert copy.id != original.id
    assert copy.published == original.published


async def test_get_dashboard_changed_on_none(async_session: AsyncSession) -> None:
    dao = FakeDashboardDAO(async_session)
    dash = await dao.create({"dashboard_title": "No Time", "changed_on": None})
    await async_session.flush()

    changed = await dao.get_dashboard_changed_on(dash)
    assert changed.tzinfo is not None
    assert changed.microsecond == 0


async def test_get_dashboard_changed_on_tz_naive(async_session: AsyncSession) -> None:
    dao = FakeDashboardDAO(async_session)
    naive_dt = datetime(2025, 3, 15, 10, 30, 45, 123456)
    dash = await dao.create({"dashboard_title": "Naive", "changed_on": naive_dt})
    await async_session.flush()

    changed = await dao.get_dashboard_changed_on(dash)
    assert changed.tzinfo is not None
    assert changed.microsecond == 0
    assert changed.year == 2025


async def test_update_native_filters_config(async_session: AsyncSession) -> None:
    dao = FakeDashboardDAO(async_session)
    dash = await dao.create(
        {
            "dashboard_title": "Filters",
            "json_metadata": json.dumps({"existing": "value"}),
            "changed_on": datetime.now(tz=timezone.utc),
        }
    )
    await async_session.flush()

    await dao.update_native_filters_config(dash, [{"id": "f1", "targets": []}])
    md = json.loads(dash.json_metadata)
    assert md["existing"] == "value"
    assert md["native_filter_configuration"] == [{"id": "f1", "targets": []}]


async def test_update_colors_config(async_session: AsyncSession) -> None:
    dao = FakeDashboardDAO(async_session)
    dash = await dao.create(
        {
            "dashboard_title": "Colors",
            "changed_on": datetime.now(tz=timezone.utc),
        }
    )
    await async_session.flush()

    await dao.update_colors_config(
        dash,
        {
            "color_scheme": "supersetColors",
            "label_colors": {"label1": "#ff0000"},
            "irrelevant_key": "ignored",
        },
    )
    md = json.loads(dash.json_metadata)
    assert md["color_scheme"] == "supersetColors"
    assert md["label_colors"] == {"label1": "#ff0000"}
    assert "irrelevant_key" not in md


async def test_embedded_upsert_create(async_session: AsyncSession) -> None:
    dao = FakeEmbeddedDashboardDAO(async_session)
    embedded = await dao.upsert(dashboard_id=42, allowed_domains="example.com")
    await async_session.flush()
    assert embedded.id is not None
    assert embedded.dashboard_id == 42
    assert embedded.allowed_domains == "example.com"


async def test_embedded_upsert_update(async_session: AsyncSession) -> None:
    dao = FakeEmbeddedDashboardDAO(async_session)
    await dao.upsert(dashboard_id=42, allowed_domains="old.com")
    await async_session.flush()

    updated = await dao.upsert(dashboard_id=42, allowed_domains="new.com")
    await async_session.flush()
    assert updated.allowed_domains == "new.com"

    # Only one record should exist
    from sqlalchemy import select

    stmt = select(FakeEmbeddedDashboard).where(FakeEmbeddedDashboard.dashboard_id == 42)
    result = await async_session.execute(stmt)
    assert len(list(result.scalars().all())) == 1


async def test_get_datasets_for_dashboard_includes_query_datasources():
    """get_datasets_for_dashboard must return Query/SavedQuery datasources too,
    not only SqlaTable — 1:1 with upstream datasets_trimmed_for_slices which
    groups slices across all datasource types. A chart backed by a SQL Lab
    Query was previously dropped from GET /dashboard/{id}/datasets."""
    from unittest.mock import AsyncMock, MagicMock

    from superset.db.daos.dashboard import AsyncDashboardDAO

    table_slice = MagicMock(datasource_id=1, datasource_type="table")
    query_slice = MagicMock(datasource_id=7, datasource_type="query")
    dashboard = MagicMock()
    dashboard.slices = [table_slice, query_slice]

    table_ds = MagicMock()
    query_ds = MagicMock()
    res_table = MagicMock()
    res_table.scalars.return_value.all.return_value = [table_ds]
    res_query = MagicMock()
    res_query.scalars.return_value.all.return_value = [query_ds]

    session = AsyncMock(spec=AsyncSession)
    session.refresh = AsyncMock()
    session.execute = AsyncMock(side_effect=[res_table, res_query])

    dao = AsyncDashboardDAO(session)
    result = await dao.get_datasets_for_dashboard(dashboard)

    assert table_ds in result, "table datasource must be returned"
    assert query_ds in result, "query datasource must be returned (was dropped)"
    assert session.execute.await_count == 2
