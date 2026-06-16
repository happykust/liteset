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
"""Liteset port of ``tests/unit_tests/commands/importers/v1/assets_test.py``.

Verifies that ``ImportAssetsCommand._import`` (async) correctly wires up the
``dashboard_slices`` M2M when importing the all-in-one asset bundle:

* a clean import creates the expected number of dashboard<->chart links;
* re-importing a bundle that adds a chart updates the links;
* re-importing a bundle that drops a chart removes the link.

Adapted to the async port: ``_import`` is an instance coroutine taking
``(configs, sparse)``; permission grants (``add_permissions``, network-bound)
are stubbed out — matching the original's ``security_manager.can_access``
patch. A real in-memory SQLite engine backs the session.
"""

from __future__ import annotations

import copy

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from tests.superset.unit.fixtures.assets_configs import (
    charts_config_1,
    charts_config_2,
    dashboards_config_1,
    dashboards_config_2,
    databases_config,
    datasets_config,
)


@pytest.fixture
async def import_env(monkeypatch):
    import superset.commands.chart.importers.v1.utils as chart_utils
    import superset.models  # noqa: F401  (register all models)
    from superset.models.helpers import Base

    # The bundle importer grants catalog/schema permissions via
    # ``add_permissions`` (network/security-bound) — stub it out the same way
    # the original test mocked ``security_manager.can_access`` to True.

    async def _noop_add_permissions(session, database, ssh_tunnel=None):
        return None

    monkeypatch.setattr(chart_utils, "add_permissions", _noop_add_permissions)

    # The fixture databases use a sqlite ``sqlalchemy_uri``; the importer's
    # ``check_sqlalchemy_uri`` rejects those when PREVENT_UNSAFE_DB_CONNECTIONS
    # is on. Disable it for the import (the original ran with it off).
    monkeypatch.setenv("LITESET_PREVENT_UNSAFE_DB_CONNECTIONS", "false")

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


def _make_command(session):
    from superset.commands.importers.v1.assets import ImportAssetsCommand

    cmd = ImportAssetsCommand.__new__(ImportAssetsCommand)
    cmd.session = session
    cmd.security_manager = None
    cmd.current_user = None
    return cmd


async def _dashboard_and_chart_ids(session):
    from superset.models.dashboard import dashboard_slices

    dashboard_ids = (
        (await session.execute(select(dashboard_slices.c.dashboard_id).distinct()))
        .scalars()
        .all()
    )
    chart_ids = (
        (await session.execute(select(dashboard_slices.c.slice_id))).scalars().all()
    )
    return dashboard_ids, chart_ids


async def test_import_new_assets(import_env) -> None:
    """All new assets are imported correctly."""
    session = import_env
    cmd = _make_command(session)

    configs = {
        **copy.deepcopy(databases_config),
        **copy.deepcopy(datasets_config),
        **copy.deepcopy(charts_config_1),
        **copy.deepcopy(dashboards_config_1),
    }
    expected_number_of_dashboards = len(dashboards_config_1)
    expected_number_of_charts = len(charts_config_1)

    await cmd._import(configs, sparse=False)
    await session.flush()

    dashboard_ids, chart_ids = await _dashboard_and_chart_ids(session)
    assert len(chart_ids) == expected_number_of_charts
    assert len(dashboard_ids) == expected_number_of_dashboards


async def test_import_adds_dashboard_charts(import_env) -> None:
    """Existing dashboards are updated with new charts."""
    session = import_env
    cmd = _make_command(session)

    base_configs = {
        **copy.deepcopy(databases_config),
        **copy.deepcopy(datasets_config),
        **copy.deepcopy(charts_config_2),
        **copy.deepcopy(dashboards_config_2),
    }
    new_configs = {
        **copy.deepcopy(databases_config),
        **copy.deepcopy(datasets_config),
        **copy.deepcopy(charts_config_1),
        **copy.deepcopy(dashboards_config_1),
    }
    expected_number_of_dashboards = len(dashboards_config_1)
    expected_number_of_charts = len(charts_config_1)

    await cmd._import(base_configs, sparse=False)
    await cmd._import(new_configs, sparse=False)
    await session.flush()

    dashboard_ids, chart_ids = await _dashboard_and_chart_ids(session)
    assert len(chart_ids) == expected_number_of_charts
    assert len(dashboard_ids) == expected_number_of_dashboards


async def test_import_removes_dashboard_charts(import_env) -> None:
    """Existing dashboards are updated without old charts."""
    session = import_env
    cmd = _make_command(session)

    base_configs = {
        **copy.deepcopy(databases_config),
        **copy.deepcopy(datasets_config),
        **copy.deepcopy(charts_config_1),
        **copy.deepcopy(dashboards_config_1),
    }
    new_configs = {
        **copy.deepcopy(databases_config),
        **copy.deepcopy(datasets_config),
        **copy.deepcopy(charts_config_2),
        **copy.deepcopy(dashboards_config_2),
    }
    expected_number_of_dashboards = len(dashboards_config_2)
    expected_number_of_charts = len(charts_config_2)

    await cmd._import(base_configs, sparse=False)
    await cmd._import(new_configs, sparse=False)
    await session.flush()

    dashboard_ids, chart_ids = await _dashboard_and_chart_ids(session)
    assert len(chart_ids) == expected_number_of_charts
    assert len(dashboard_ids) == expected_number_of_dashboards
