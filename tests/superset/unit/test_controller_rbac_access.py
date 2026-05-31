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
"""Regression tests for per-object RBAC checks that the Flask→Litestar port had
dropped on several READ endpoints (live-probed as a Gamma user).

Each endpoint here must enforce datasource/dashboard access BEFORE returning
data — 1:1 with upstream, which calls ``datasource.raise_for_access()`` /
``dashboard.raise_for_access()`` (and the ``dashboard_access_filters`` base
filter). Without these, a low-privilege user could read column values, chart
definitions, or generated SQL of objects they cannot access.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from superset.controllers.chart import ChartController
from superset.controllers.dashboard import DashboardController
from superset.controllers.datasource import DatasourceController
from superset.errors import ErrorLevel, SupersetError, SupersetErrorType
from superset.exceptions import ObjectNotFoundError, SupersetSecurityException


def _fn(cls: type, name: str):
    handler = getattr(cls, name)
    return handler.fn if hasattr(handler, "fn") else handler


def _denied() -> SupersetSecurityException:
    return SupersetSecurityException(
        SupersetError(
            error_type=SupersetErrorType.DATASOURCE_SECURITY_ACCESS_ERROR,
            message="Access denied",
            level=ErrorLevel.ERROR,
        )
    )


# ---------------------------------------------------------------------------
# datasource column values — must raise_for_access(datasource=...) → 403
# ---------------------------------------------------------------------------

_get_column_values = _fn(DatasourceController, "get_column_values")


async def test_column_values_denied_returns_403():
    controller = DatasourceController(owner=MagicMock())
    ds_dao = MagicMock()
    ds_dao.get_datasource = AsyncMock(return_value=MagicMock())
    sm = MagicMock()
    sm.raise_for_access = AsyncMock(side_effect=_denied())
    result = await _get_column_values(
        controller,
        datasource_type="table",
        datasource_id=1,
        column_name="source",
        ds_dao=ds_dao,
        security_manager=sm,
        current_user=MagicMock(),
    )
    assert result.status_code == 403
    sm.raise_for_access.assert_awaited_once()


async def test_column_values_access_check_runs_before_reading_values():
    """The access check must precede any value fetch (no data leak)."""
    controller = DatasourceController(owner=MagicMock())
    datasource = MagicMock()
    datasource.async_values_for_column = AsyncMock(return_value=["x"])
    ds_dao = MagicMock()
    ds_dao.get_datasource = AsyncMock(return_value=datasource)
    sm = MagicMock()
    sm.raise_for_access = AsyncMock(side_effect=_denied())
    result = await _get_column_values(
        controller,
        datasource_type="table",
        datasource_id=1,
        column_name="source",
        ds_dao=ds_dao,
        security_manager=sm,
        current_user=MagicMock(),
    )
    assert result.status_code == 403
    # Values must NOT have been read after a denial.
    datasource.async_values_for_column.assert_not_awaited()


# ---------------------------------------------------------------------------
# dashboard /charts /datasets /tabs — must access-filter (→404) + raise_for_access
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["get_charts", "get_datasets", "get_tabs"])
async def test_dashboard_subendpoint_access_filtered_404(method):
    """An inaccessible dashboard (filtered out by dashboard_access_filters) must
    404, not leak its charts/datasets/tab structure."""
    controller = DashboardController(owner=MagicMock())
    dao = MagicMock()
    # The access base-filter excludes the dashboard → loader returns None.
    dao.get_full_by_id_or_slug = AsyncMock(return_value=None)
    sm = MagicMock()
    sm.raise_for_access = AsyncMock()
    with patch(
        "superset.db.filters.dashboard_access_filters",
        new=AsyncMock(return_value=[MagicMock()]),
    ):
        with pytest.raises(ObjectNotFoundError):
            await _fn(DashboardController, method)(
                controller,
                id_or_slug="1",
                dao=dao,
                security_manager=sm,
                current_user=MagicMock(),
            )


@pytest.mark.parametrize("method", ["get_charts", "get_datasets", "get_tabs"])
async def test_dashboard_subendpoint_raise_for_access_denies(method):
    """Even a dashboard that passes the base filter is gated by the secondary
    raise_for_access (403)."""
    controller = DashboardController(owner=MagicMock())
    dashboard = MagicMock()
    dashboard.position_json = None
    dao = MagicMock()
    dao.get_full_by_id_or_slug = AsyncMock(return_value=dashboard)
    sm = MagicMock()
    sm.raise_for_access = AsyncMock(side_effect=_denied())
    with patch(
        "superset.db.filters.dashboard_access_filters",
        new=AsyncMock(return_value=[]),
    ):
        with pytest.raises(SupersetSecurityException):
            await _fn(DashboardController, method)(
                controller,
                id_or_slug="1",
                dao=dao,
                security_manager=sm,
                current_user=MagicMock(),
            )
    sm.raise_for_access.assert_awaited_once()


# ---------------------------------------------------------------------------
# GET /chart/{id}/data/ — access-scoped lookup → 404 (not 403 leaking ds name)
# ---------------------------------------------------------------------------

_get_chart_data = _fn(ChartController, "get_chart_data")


async def test_chart_data_get_access_filtered_404():
    """A chart the user can't access must 404 (access base-filter excludes it),
    not 403 leaking the backing datasource name. Mirrors upstream
    ``datamodel.get(pk, base_filters)``."""
    controller = ChartController(owner=MagicMock())
    dao = MagicMock()
    dao.find_all = AsyncMock(return_value=[])  # access filter excludes the chart
    sm = MagicMock()
    state = MagicMock()
    state.settings = MagicMock(global_async_queries=False)
    with patch(
        "superset.db.filters.chart_access_filters",
        new=AsyncMock(return_value=[MagicMock()]),
    ):
        with pytest.raises(ObjectNotFoundError):
            await _get_chart_data(
                controller,
                request=MagicMock(),
                pk=3,
                dao=dao,
                ds_dao=MagicMock(),
                security_manager=sm,
                current_user=MagicMock(),
                state=state,
            )
    # The lookup must be the access-scoped find_all, not an unfiltered find_by_id.
    dao.find_all.assert_awaited_once()
