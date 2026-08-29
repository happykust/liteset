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
"""Regression: a report/alert schedule must not bind to a chart, dashboard,
or database the caller cannot see.

``_validate_chart_dashboard``/``_find_accessible_database``
(superset/commands/report.py) used to resolve the referenced chart /
dashboard / database via ``find_by_id`` with NO access filter. Neither the
alert-execution path nor the dashboard-render path re-checks access at
run time, so an id the caller cannot see must resolve to "not found" here
— exactly like the object's own GET/PUT endpoints.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.fixture
async def report_env():
    """In-memory SQLite session with one Database, one Dashboard, and one
    Chart row."""
    import superset.models  # noqa: F401  (register models)
    from superset.models.core import Database
    from superset.models.dashboard import Dashboard
    from superset.models.helpers import Base
    from superset.models.slice import Slice

    sync_engine = create_engine("sqlite://")
    Base.metadata.create_all(sync_engine)
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        creator=lambda: sync_engine.raw_connection(),
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        database = Database(database_name="secret_db", sqlalchemy_uri="sqlite://")
        dashboard = Dashboard(dashboard_title="Secret Dashboard", published=True)
        chart = Slice(slice_name="Secret Chart", viz_type="table")
        session.add_all([database, dashboard, chart])
        await session.commit()
        yield session, database, dashboard, chart
    await engine.dispose()


async def test_dashboard_lookup_refused_when_not_accessible(report_env) -> None:
    from superset.commands.report import _validate_chart_dashboard
    from superset.commands.report_exceptions import DashboardNotFoundValidationError
    from superset.models.reports import ReportCreationMethod

    session, _database, dashboard, _chart = report_env
    dao = SimpleNamespace(session=session)
    sm = AsyncMock()
    user = SimpleNamespace(id=1)
    data = {
        "dashboard": dashboard.id,
        "creation_method": ReportCreationMethod.DASHBOARDS.value,
    }
    exceptions: list = []

    with patch(
        "superset.db.filters.dashboard_access_filters",
        new=AsyncMock(return_value=[sa.text("0=1")]),  # deny everything
    ):
        await _validate_chart_dashboard(dao, data, exceptions, sm, user)

    assert data["dashboard"] is None
    assert any(isinstance(e, DashboardNotFoundValidationError) for e in exceptions)


async def test_dashboard_lookup_succeeds_when_accessible(report_env) -> None:
    from superset.commands.report import _validate_chart_dashboard
    from superset.models.reports import ReportCreationMethod

    session, _database, dashboard, _chart = report_env
    dao = SimpleNamespace(session=session)
    sm = AsyncMock()
    user = SimpleNamespace(id=1)
    data = {
        "dashboard": dashboard.id,
        "creation_method": ReportCreationMethod.DASHBOARDS.value,
    }
    exceptions: list = []

    with patch(
        "superset.db.filters.dashboard_access_filters",
        new=AsyncMock(return_value=[]),  # admin / full access -> no restriction
    ):
        await _validate_chart_dashboard(dao, data, exceptions, sm, user)

    assert exceptions == []
    assert data["dashboard"] is not None
    assert data["dashboard"].id == dashboard.id


async def test_chart_lookup_refused_when_not_accessible(report_env) -> None:
    from superset.commands.report import _validate_chart_dashboard
    from superset.commands.report_exceptions import ChartNotFoundValidationError
    from superset.models.reports import ReportCreationMethod

    session, _database, _dashboard, chart = report_env
    dao = SimpleNamespace(session=session)
    sm = AsyncMock()
    user = SimpleNamespace(id=1)
    data = {
        "chart": chart.id,
        "creation_method": ReportCreationMethod.CHARTS.value,
    }
    exceptions: list = []

    with patch(
        "superset.db.filters.chart_access_filters",
        new=AsyncMock(return_value=[sa.text("0=1")]),  # deny everything
    ):
        await _validate_chart_dashboard(dao, data, exceptions, sm, user)

    assert data["chart"] is None
    assert any(isinstance(e, ChartNotFoundValidationError) for e in exceptions)


async def test_chart_lookup_succeeds_when_accessible(report_env) -> None:
    from superset.commands.report import _validate_chart_dashboard
    from superset.models.reports import ReportCreationMethod

    session, _database, _dashboard, chart = report_env
    dao = SimpleNamespace(session=session)
    sm = AsyncMock()
    user = SimpleNamespace(id=1)
    data = {
        "chart": chart.id,
        "creation_method": ReportCreationMethod.CHARTS.value,
    }
    exceptions: list = []

    with patch(
        "superset.db.filters.chart_access_filters",
        new=AsyncMock(return_value=[]),  # admin / full access -> no restriction
    ):
        await _validate_chart_dashboard(dao, data, exceptions, sm, user)

    assert exceptions == []
    assert data["chart"] is not None
    assert data["chart"].id == chart.id


async def test_alert_database_lookup_refused_when_not_accessible(report_env) -> None:
    from superset.commands.report import _find_accessible_database

    session, database, _dashboard, _chart = report_env
    sm = AsyncMock()
    user = SimpleNamespace(id=1)

    with patch(
        "superset.db.filters.database_access_filters",
        new=AsyncMock(return_value=[sa.text("0=1")]),  # deny everything
    ):
        found = await _find_accessible_database(session, database.id, sm, user)

    assert found is None


async def test_alert_database_lookup_succeeds_when_accessible(report_env) -> None:
    from superset.commands.report import _find_accessible_database

    session, database, _dashboard, _chart = report_env
    sm = AsyncMock()
    user = SimpleNamespace(id=1)

    with patch(
        "superset.db.filters.database_access_filters",
        new=AsyncMock(return_value=[]),
    ):
        found = await _find_accessible_database(session, database.id, sm, user)

    assert found is not None
    assert found.id == database.id
