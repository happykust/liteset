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
"""Integration tests for DashboardController.

Tests the full HTTP pipeline with mocked DAO dependencies.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from litestar.di import Provide
from litestar.testing import AsyncTestClient

from superset.controllers.dashboard import DashboardController
from superset.controllers.dashboard_filter_state import DashboardFilterStateController
from tests.superset.integration.conftest import (
    create_test_app,
    create_test_app_no_auth,
    make_mock_dao,
)


@pytest.fixture
def app():
    return create_test_app(DashboardController)


async def test_get_dashboard_list(app):
    """GET /api/v1/dashboard/ returns empty list with count."""
    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/api/v1/dashboard/")
        assert resp.status_code == 200
        data = resp.json()
        assert "result" in data
        assert "count" in data
        assert data["result"] == []
        assert data["count"] == 0


async def test_get_dashboard_info(app):
    """GET /api/v1/dashboard/_info returns permissions metadata."""
    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/api/v1/dashboard/_info")
        assert resp.status_code == 200
        data = resp.json()
        assert "permissions" in data
        assert "can_read" in data["permissions"]


async def test_get_dashboard_by_slug_not_found(app):
    """GET /api/v1/dashboard/{id_or_slug} returns 404 when not found."""
    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/api/v1/dashboard/my-dashboard")
        assert resp.status_code == 404


async def test_get_dashboard_favorite_status_empty(app):
    """GET /api/v1/dashboard/favorite_status/ returns empty without ids."""
    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/api/v1/dashboard/favorite_status/")
        assert resp.status_code == 200
        data = resp.json()
        assert data["result"] == []


async def test_delete_dashboard_bulk_no_ids(app):
    """DELETE /api/v1/dashboard/ (bulk) with no ids returns 422 validation error."""
    async with AsyncTestClient(app=app) as client:
        resp = await client.delete("/api/v1/dashboard/")
        # rison_params mock returns None → ids=[] → CommandInvalidError
        assert resp.status_code == 422


async def test_create_permalink(app):
    """POST /api/v1/dashboard/1/permalink returns 404 when dashboard not found."""
    async with AsyncTestClient(app=app) as client:
        payload = {
            "dataMask": {},
            "activeTabs": [],
            "anchor": None,
            "urlParams": [],
        }
        # MockDAO.find_by_id returns None, so dashboard lookup raises 404
        resp = await client.post("/api/v1/dashboard/1/permalink", json=payload)
        assert resp.status_code == 404


async def test_create_filter_state():
    """POST /api/v1/dashboard/1/filter_state/ returns 201 with key."""
    # The create command runs ``check_access`` which loads the dashboard via
    # ``dashboard_dao.get_full_by_id_or_slug``; supply a dao that finds one so
    # the access gate passes and the create reaches the 201 path.
    found_dashboard_dao = make_mock_dao()
    found_dashboard_dao.get_full_by_id_or_slug = AsyncMock(return_value=MagicMock())
    filter_app = create_test_app(
        DashboardFilterStateController,
        dependency_overrides={
            "dashboard_dao": Provide(
                lambda: found_dashboard_dao, sync_to_thread=False
            ),
        },
    )
    async with AsyncTestClient(app=filter_app) as client:
        payload = {"value": '{"test": true}'}
        resp = await client.post("/api/v1/dashboard/1/filter_state/", json=payload)
        assert resp.status_code == 201
        data = resp.json()
        assert "key" in data


async def test_update_dashboard_not_found(app):
    """PUT /api/v1/dashboard/{pk} returns 404 when dashboard not found.

    MockDAO.find_by_id returns None by default, so UpdateDashboardCommand
    raises ObjectNotFoundError -> 404.
    """
    async with AsyncTestClient(app=app) as client:
        resp = await client.put(
            "/api/v1/dashboard/99",
            json={"dashboard_title": "Updated Title"},
        )
        assert resp.status_code == 404


async def test_delete_dashboard_single_not_found(app):
    """DELETE /api/v1/dashboard/{pk} returns 404 when dashboard not found."""
    async with AsyncTestClient(app=app) as client:
        resp = await client.delete("/api/v1/dashboard/99")
        assert resp.status_code == 404


async def test_copy_dashboard_not_found(app):
    """POST /api/v1/dashboard/{id_or_slug}/copy/ returns 404 when not found.

    MockDAO.get_by_id_or_slug returns None by default.
    """
    async with AsyncTestClient(app=app) as client:
        resp = await client.post(
            "/api/v1/dashboard/nonexistent/copy/",
            json={"dashboard_title": "Copy of Dashboard", "json_metadata": "{}"},
        )
        assert resp.status_code == 404


async def test_get_embedded_not_found(app):
    """GET /api/v1/dashboard/{id_or_slug}/embedded returns 404 for missing dashboard."""
    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/api/v1/dashboard/nonexistent/embedded")
        assert resp.status_code == 404


async def test_get_dashboard_tabs_not_found(app):
    """GET /api/v1/dashboard/{id_or_slug}/tabs returns 404 when dashboard missing."""
    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/api/v1/dashboard/nonexistent/tabs")
        assert resp.status_code == 404


async def test_get_dashboard_charts_not_found(app):
    """GET /api/v1/dashboard/{id_or_slug}/charts returns 404 when dashboard missing."""
    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/api/v1/dashboard/nonexistent/charts")
        assert resp.status_code == 404


async def test_get_dashboard_datasets_not_found(app):
    """GET /api/v1/dashboard/{id_or_slug}/datasets returns 404 when dashboard
    missing.
    """
    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/api/v1/dashboard/nonexistent/datasets")
        assert resp.status_code == 404


async def test_unauthenticated_returns_401():
    """GET /api/v1/dashboard/ without credentials returns 401."""
    no_auth_app = create_test_app_no_auth(DashboardController)
    async with AsyncTestClient(app=no_auth_app) as client:
        resp = await client.get("/api/v1/dashboard/")
        assert resp.status_code == 401
