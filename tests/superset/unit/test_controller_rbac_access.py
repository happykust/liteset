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
data, calling ``datasource.raise_for_access()`` /
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
    datasource.async_values_for_column.assert_not_awaited()


@pytest.mark.parametrize("method", ["get_charts", "get_datasets", "get_tabs"])
async def test_dashboard_subendpoint_access_filtered_404(method):
    """An inaccessible dashboard (filtered out by dashboard_access_filters) must
    404, not leak its charts/datasets/tab structure."""
    controller = DashboardController(owner=MagicMock())
    dao = MagicMock()
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


_get_chart_data = _fn(ChartController, "get_chart_data")


async def test_chart_data_get_access_filtered_404():
    """A chart the user can't access must 404 (access base-filter excludes it),
    not 403 leaking the backing datasource name."""
    controller = ChartController(owner=MagicMock())
    dao = MagicMock()
    dao.find_all = AsyncMock(return_value=[])
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
    dao.find_all.assert_awaited_once()


async def test_dashboard_copy_access_filtered_404():
    """Gamma must NOT be able to copy (exfiltrate) a dashboard it can't access."""
    from superset.schemas.dashboard import DashboardCopySchema

    controller = DashboardController(owner=MagicMock())
    dao = MagicMock()
    dao.get_full_by_id_or_slug = AsyncMock(return_value=None)
    sm = MagicMock()
    sm.raise_for_access = AsyncMock()
    with patch(
        "superset.db.filters.dashboard_access_filters",
        new=AsyncMock(return_value=[MagicMock()]),
    ):
        with pytest.raises(ObjectNotFoundError):
            await _fn(DashboardController, "copy_dashboard")(
                controller,
                id_or_slug="1",
                data=DashboardCopySchema(
                    dashboard_title="x", json_metadata="{}", duplicate_slices=False
                ),
                dao=dao,
                security_manager=sm,
                current_user=MagicMock(),
            )


async def test_dashboard_get_embedded_access_filtered_404():
    """Gamma must NOT read the embedded config of an inaccessible dashboard."""
    controller = DashboardController(owner=MagicMock())
    dao = MagicMock()
    dao.get_full_by_id_or_slug = AsyncMock(return_value=None)
    sm = MagicMock()
    sm.raise_for_access = AsyncMock()
    with patch(
        "superset.db.filters.dashboard_access_filters",
        new=AsyncMock(return_value=[MagicMock()]),
    ):
        with pytest.raises(ObjectNotFoundError):
            await _fn(DashboardController, "get_embedded")(
                controller,
                id_or_slug="1",
                dao=dao,
                embedded_dao=MagicMock(),
                security_manager=sm,
                current_user=MagicMock(),
            )


_get_datasource = _fn(DatasourceController, "get_datasource")


async def test_get_datasource_denied_returns_403():
    controller = DatasourceController(owner=MagicMock())
    ds_dao = MagicMock()
    ds_dao.get_datasource = AsyncMock(return_value=MagicMock())
    sm = MagicMock()
    sm.raise_for_access = AsyncMock(side_effect=_denied())
    result = await _get_datasource(
        controller,
        datasource_type="table",
        datasource_id=1,
        ds_dao=ds_dao,
        security_manager=sm,
        current_user=MagicMock(),
    )
    assert result.status_code == 403
    sm.raise_for_access.assert_awaited_once()


async def test_dashboard_create_permalink_access_filtered_404():
    """Gamma must NOT create a permalink for a dashboard it can't access
    (upstream CreateDashboardPermalinkCommand gates via get_by_id_or_slug)."""
    from superset.schemas.dashboard import DashboardPermalinkSchema

    controller = DashboardController(owner=MagicMock())
    dao = MagicMock()
    dao.get_full_by_id_or_slug = AsyncMock(return_value=None)
    sm = MagicMock()
    sm.raise_for_access = AsyncMock()
    user = MagicMock()
    user.id = 3
    with patch(
        "superset.db.filters.dashboard_access_filters",
        new=AsyncMock(return_value=[MagicMock()]),
    ):
        with pytest.raises(ObjectNotFoundError):
            await _fn(DashboardController, "create_permalink")(
                controller,
                pk=1,
                data=DashboardPermalinkSchema(),
                dao=dao,
                kv_dao=MagicMock(),
                security_manager=sm,
                current_user=user,
            )


async def test_explore_datasource_denied_returns_403():
    from superset.controllers.explore import ExploreController

    controller = ExploreController(owner=MagicMock())
    request = MagicMock()
    request.query_params = {"datasource_id": "1", "datasource_type": "table"}
    dataset = MagicMock()
    dataset.database = None
    dataset_dao = MagicMock()
    dataset_dao.find_all = AsyncMock(return_value=[dataset])
    sm = MagicMock()
    sm.raise_for_access = AsyncMock(side_effect=_denied())
    result = await _fn(ExploreController, "get_explore")(
        controller,
        request=request,
        chart_dao=MagicMock(),
        dataset_dao=dataset_dao,
        kv_dao=MagicMock(),
        query_dao=MagicMock(),
        security_manager=sm,
        current_user=MagicMock(),
        session=AsyncMock(),
    )
    assert result.status_code == 403


async def test_database_tables_filters_inaccessible():
    from superset.controllers.database import DatabaseController

    controller = DatabaseController(owner=MagicMock())
    dao = MagicMock()
    db = MagicMock()
    dao.find_by_id = AsyncMock(return_value=db)
    dao.get_table_extra_lookup = AsyncMock(return_value={})
    sm = MagicMock()
    sm.can_access_database = AsyncMock(return_value=False)
    sm.get_schemas_accessible_by_user = AsyncMock(return_value=[])

    class _Spec:
        async def get_table_names(self, conn, schema):
            return ["secret_table", "ab_user"]

        async def get_view_names(self, conn, schema):
            return ["secret_view"]

    import contextlib

    @contextlib.asynccontextmanager
    async def _fake_conn(_db):
        yield (MagicMock(), _Spec())

    with (
        patch("superset.controllers.database.get_async_connection", _fake_conn),
        # The database itself IS visible to the user (the R13-07 visibility
        # gate passes); this test exercises the table-level filtering below.
        patch(
            "superset.controllers.database._database_is_accessible",
            new=AsyncMock(return_value=True),
        ),
    ):
        result = await _fn(DatabaseController, "tables")(
            controller,
            pk=1,
            dao=dao,
            security_manager=sm,
            current_user=MagicMock(),
            rison_params={"schema_name": "public"},
        )
    assert result["count"] == 0
    assert result["result"] == []


async def test_database_ssh_tunnel_get_denied_404():
    from superset.controllers.database import DatabaseController

    controller = DatabaseController(owner=MagicMock())
    dao = MagicMock()
    dao.find_by_id = AsyncMock(return_value=MagicMock())
    sm = MagicMock()
    sm.can_access_all_databases = AsyncMock(return_value=False)
    sm.user_view_menu_names = AsyncMock(return_value=[])
    sm.is_admin = MagicMock(return_value=False)
    with pytest.raises(ObjectNotFoundError):
        await _fn(DatabaseController, "get_ssh_tunnel")(
            controller,
            pk=1,
            dao=dao,
            security_manager=sm,
            current_user=MagicMock(),
        )


def test_database_get_ssh_tunnel_requires_can_write() -> None:
    """GET /database/{pk}/ssh_tunnel/ must require can_write — upstream exposes
    tunnel metadata only via get_connection (can_write); a can_read gate would
    let Gamma enumerate SSH bastion host/username (R15-01)."""
    from superset.controllers.database import DatabaseController

    handler = DatabaseController.get_ssh_tunnel
    perms = [
        c
        for g in (handler.guards or [])
        for c in (cell.cell_contents for cell in (g.__closure__ or []))
        if isinstance(c, tuple) and len(c) == 2
    ]
    assert ("can_write", "Database") in perms, perms


def test_user_avatar_requires_authentication() -> None:
    """GET /user/{id}/avatar.png must require authentication.
    The port had used exclude_from_auth (R15-02) allowing anonymous access."""
    from superset.controllers.user import UserPublicController
    from superset.guards.rbac import require_authentication

    handler = UserPublicController.avatar
    assert require_authentication in (handler.guards or [])
    assert not (handler.opt or {}).get("exclude_from_auth"), handler.opt
