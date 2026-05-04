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
"""RBAC negative-path tests -- verify 403 for insufficient permissions and 401 for
unauthenticated.
"""

from __future__ import annotations

import pytest
from litestar.testing import AsyncTestClient

from superset.controllers.chart import ChartController
from superset.controllers.dashboard import DashboardController
from superset.controllers.dashboard_filter_state import DashboardFilterStateController
from superset.controllers.database import DatabaseController
from superset.controllers.dataset import DatasetController
from superset.controllers.query import QueryController
from superset.controllers.saved_query import SavedQueryController
from superset.controllers.sqllab import SqlLabController
from tests.superset.integration.conftest import (
    create_test_app_limited,
    create_test_app_no_auth,
)


@pytest.fixture
def chart_app_limited():
    return create_test_app_limited(ChartController)


@pytest.fixture
def dashboard_app_limited():
    return create_test_app_limited(DashboardController)


async def test_chart_write_returns_403(chart_app_limited):
    """User with only can_read_Chart gets 403 on POST."""
    async with AsyncTestClient(app=chart_app_limited) as client:
        resp = await client.post(
            "/api/v1/chart/", json={"slice_name": "test", "viz_type": "table"}
        )
        assert resp.status_code == 403


async def test_chart_delete_returns_403(chart_app_limited):
    """User with only can_read_Chart gets 403 on DELETE."""
    async with AsyncTestClient(app=chart_app_limited) as client:
        resp = await client.delete("/api/v1/chart/1")
        assert resp.status_code == 403


async def test_chart_read_allowed(chart_app_limited):
    """User with can_read_Chart CAN read charts (200 or at least not 403)."""
    async with AsyncTestClient(app=chart_app_limited) as client:
        resp = await client.get("/api/v1/chart/")
        assert resp.status_code != 403


async def test_dashboard_read_returns_403(dashboard_app_limited):
    """User with only can_read_Chart gets 403 on Dashboard read (no
    can_read_Dashboard).
    """
    async with AsyncTestClient(app=dashboard_app_limited) as client:
        resp = await client.get("/api/v1/dashboard/")
        assert resp.status_code == 403


async def test_unauthenticated_returns_401():
    """Unauthenticated user gets 401."""
    app = create_test_app_no_auth(ChartController)
    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/api/v1/chart/")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Parametrized 403 coverage for remaining core controllers
# ---------------------------------------------------------------------------
# Each tuple: (controller_class, http_method, path, description, body)
# MockLimitedUser has only "can_read_Chart", so every entry below should
# yield 403 — either because the operation requires a write permission the
# user lacks, or because the user has no permission on that resource at all.
#
# POST/PUT cases carry a minimal valid body so that msgspec schema validation
# passes and the RBAC guard (not a 422) is what produces the 403.
_RBAC_403_CASES: list[tuple[type, str, str, str, dict | None]] = [
    # DatabaseController — write operations
    # DatabasePostSchema requires: database_name (str)
    (
        DatabaseController,
        "POST",
        "/api/v1/database/",
        "database_create_forbidden",
        {"database_name": "test_db"},
    ),
    (
        DatabaseController,
        "DELETE",
        "/api/v1/database/1",
        "database_delete_forbidden",
        None,
    ),
    # DatasetController — write operations
    # DatasetPostSchema requires: table_name (str), database (int)
    (
        DatasetController,
        "POST",
        "/api/v1/dataset/",
        "dataset_create_forbidden",
        {"table_name": "test_table", "database": 1},
    ),
    (
        DatasetController,
        "DELETE",
        "/api/v1/dataset/1",
        "dataset_delete_forbidden",
        None,
    ),
    # SavedQueryController — write operations
    # SavedQueryPostSchema requires: label (str), sql (str), db_id (int)
    (
        SavedQueryController,
        "POST",
        "/api/v1/saved_query/",
        "saved_query_create_forbidden",
        {"label": "my query", "sql": "SELECT 1", "db_id": 1},
    ),
    (
        SavedQueryController,
        "DELETE",
        "/api/v1/saved_query/1",
        "saved_query_delete_forbidden",
        None,
    ),
    # DashboardFilterStateController — write on resource user cannot access
    # FilterStateSchema requires: value (str)
    (
        DashboardFilterStateController,
        "POST",
        "/api/v1/dashboard/1/filter_state/",
        "dashboard_filter_state_create_forbidden",
        {"value": "{}"},
    ),
    # SqlLabController — execute requires can_write_SqlLab
    # ExecutePayloadSchema requires: database_id (int), sql (str)
    (
        SqlLabController,
        "POST",
        "/api/v1/sqllab/execute/",
        "sqllab_execute_forbidden",
        {"database_id": 1, "sql": "SELECT 1"},
    ),
]


@pytest.mark.parametrize(
    "controller_cls, method, path, description, body",
    [
        pytest.param(ctrl, method, path, desc, body, id=desc)
        for ctrl, method, path, desc, body in _RBAC_403_CASES
    ],
)
async def test_limited_user_gets_403(
    controller_cls: type,
    method: str,
    path: str,
    description: str,
    body: dict | None,
) -> None:
    """MockLimitedUser (can_read_Chart only) receives 403 for every listed operation."""
    app = create_test_app_limited(controller_cls)
    async with AsyncTestClient(app=app) as client:
        request_method = getattr(client, method.lower())
        kwargs = {"json": body} if body is not None else {}
        resp = await request_method(path, **kwargs)
        assert resp.status_code == 403, (
            f"{description}: expected 403 but got {resp.status_code} "
            f"for {method} {path}"
        )


# ---------------------------------------------------------------------------
# 401 coverage — unauthenticated requests to controllers beyond ChartController
# ---------------------------------------------------------------------------
# Each entry: (controller_class, path, description)
# create_test_app_no_auth raises NotAuthorizedException before any handler
# runs, so every GET below must return 401 regardless of the resource type.
_UNAUTHENTICATED_401_CASES: list[tuple[type, str, str]] = [
    (DatabaseController, "/api/v1/database/", "database_list_unauthenticated"),
    (DatasetController, "/api/v1/dataset/", "dataset_list_unauthenticated"),
    (QueryController, "/api/v1/query/", "query_list_unauthenticated"),
    (SavedQueryController, "/api/v1/saved_query/", "saved_query_list_unauthenticated"),
    (SqlLabController, "/api/v1/sqllab/", "sqllab_bootstrap_unauthenticated"),
]


@pytest.mark.parametrize(
    "controller_cls, path, description",
    [
        pytest.param(ctrl, path, desc, id=desc)
        for ctrl, path, desc in _UNAUTHENTICATED_401_CASES
    ],
)
async def test_unauthenticated_gets_401(
    controller_cls: type,
    path: str,
    description: str,
) -> None:
    """Unauthenticated requests receive 401 for every listed controller."""
    app = create_test_app_no_auth(controller_cls)
    async with AsyncTestClient(app=app) as client:
        resp = await client.get(path)
        assert resp.status_code == 401, (
            f"{description}: expected 401 but got {resp.status_code} for GET {path}"
        )
