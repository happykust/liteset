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
"""Integration tests for ChartController.

Tests the full HTTP pipeline with mocked DAO dependencies.
"""

from __future__ import annotations

import pytest
from litestar.testing import AsyncTestClient

from superset.controllers.chart import ChartController
from tests.superset.integration.conftest import create_test_app, create_test_app_no_auth


@pytest.fixture
def app():
    return create_test_app(ChartController)


async def test_get_chart_list(app):
    """GET /api/v1/chart/ returns empty list with count."""
    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/api/v1/chart/")
        assert resp.status_code == 200
        data = resp.json()
        assert "result" in data
        assert "count" in data
        assert data["result"] == []
        assert data["count"] == 0


async def test_get_chart_info(app):
    """GET /api/v1/chart/_info returns permissions metadata."""
    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/api/v1/chart/_info")
        assert resp.status_code == 200
        data = resp.json()
        assert "permissions" in data
        assert "can_read" in data["permissions"]
        assert "can_write" in data["permissions"]


async def test_get_chart_by_id_not_found(app):
    """GET /api/v1/chart/{id} returns 404 when chart not found."""
    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/api/v1/chart/999")
        assert resp.status_code == 404


async def test_get_chart_favorite_status_empty(app):
    """GET /api/v1/chart/favorite_status/ returns 404 without ids.

    Matches upstream 1:1: ``favorite_status`` does ``if not charts:
    return self.response_404()`` when no chart ids resolve.
    """
    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/api/v1/chart/favorite_status/")
        assert resp.status_code == 404


async def test_get_chart_export_no_ids(app):
    """GET /api/v1/chart/export/ with no ids returns 422."""
    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/api/v1/chart/export/")
        # rison_params mock returns None → ids=[] → CommandInvalidError
        assert resp.status_code == 422


async def test_delete_chart_bulk_no_ids(app):
    """DELETE /api/v1/chart/ (bulk) with no ids returns 422 validation error."""
    async with AsyncTestClient(app=app) as client:
        resp = await client.delete("/api/v1/chart/")
        # rison_params mock returns None → ids=[] → CommandInvalidError
        assert resp.status_code == 422


@pytest.mark.xfail(
    reason="SQLAlchemy model init triggers env-specific import chain", strict=False
)
async def test_create_chart(app):
    """POST /api/v1/chart/ returns 201 on valid payload."""
    async with AsyncTestClient(app=app) as client:
        payload = {
            "slice_name": "Test Chart",
            "viz_type": "table",
            "datasource_id": 1,
            "datasource_type": "table",
        }
        resp = await client.post("/api/v1/chart/", json=payload)
        assert resp.status_code == 201


async def test_warm_up_cache(app):
    """PUT /api/v1/chart/warm_up_cache returns result."""
    async with AsyncTestClient(app=app) as client:
        payload = {"chart_id": 1}
        resp = await client.put("/api/v1/chart/warm_up_cache", json=payload)
        # The mock DAO find_by_id returns None, so command raises ObjectNotFoundError
        assert resp.status_code == 404


async def test_get_single_chart_404_response_body(app):
    """GET /api/v1/chart/<pk> with non-existing chart returns structured error."""
    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/api/v1/chart/1")
        assert resp.status_code == 404
        data = resp.json()
        assert "detail" in data or "message" in data


async def test_unauthenticated_returns_401():
    """GET /api/v1/chart/ without credentials returns 401."""
    no_auth_app = create_test_app_no_auth(ChartController)
    async with AsyncTestClient(app=no_auth_app) as client:
        resp = await client.get("/api/v1/chart/")
        assert resp.status_code == 401
