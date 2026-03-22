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
"""Integration tests for SavedQueryController.

Tests the full HTTP pipeline with mocked DAO dependencies.
"""

from __future__ import annotations

import pytest
from litestar.testing import AsyncTestClient

from liteset.controllers.saved_query import SavedQueryController
from tests.liteset.integration.conftest import create_test_app, create_test_app_no_auth


@pytest.fixture
def app():
    return create_test_app(SavedQueryController)


async def test_get_saved_query_list(app):
    """GET /api/v1/saved_query/ returns empty list with count."""
    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/api/v1/saved_query/")
        assert resp.status_code == 200
        data = resp.json()
        assert "result" in data
        assert "count" in data
        assert data["result"] == []
        assert data["count"] == 0


async def test_get_saved_query_info(app):
    """GET /api/v1/saved_query/_info returns permissions metadata."""
    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/api/v1/saved_query/_info")
        assert resp.status_code == 200
        data = resp.json()
        assert "permissions" in data
        assert "can_read" in data["permissions"]


async def test_get_saved_query_by_id_not_found(app):
    """GET /api/v1/saved_query/{pk} returns 404 when not found."""
    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/api/v1/saved_query/999")
        assert resp.status_code == 404


async def test_delete_saved_query_bulk_no_ids(app):
    """DELETE /api/v1/saved_query/ (bulk) with no ids returns 422 validation error."""
    async with AsyncTestClient(app=app) as client:
        resp = await client.delete("/api/v1/saved_query/")
        # rison_params mock returns None → ids=[] → CommandInvalidError
        assert resp.status_code == 422


async def test_update_saved_query_not_found(app):
    """PUT /api/v1/saved_query/{pk} returns 404 when query is not found.

    MockDAO.find_by_id returns None, so UpdateSavedQueryCommand raises
    ObjectNotFoundError -> 404.
    """
    async with AsyncTestClient(app=app) as client:
        resp = await client.put(
            "/api/v1/saved_query/999",
            json={"label": "Updated Query", "sql": "SELECT 2"},
        )
        assert resp.status_code == 404


async def test_delete_saved_query_single_not_found(app):
    """DELETE /api/v1/saved_query/{pk} returns 404 when query not found."""
    async with AsyncTestClient(app=app) as client:
        resp = await client.delete("/api/v1/saved_query/999")
        assert resp.status_code == 404


async def test_export_saved_query_no_ids(app):
    """GET /api/v1/saved_query/export/ with no ids returns 422."""
    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/api/v1/saved_query/export/")
        # rison_params mock returns None → ids=[] → CommandInvalidError
        assert resp.status_code == 422


async def test_unauthenticated_returns_401():
    """GET /api/v1/saved_query/ without credentials returns 401."""
    no_auth_app = create_test_app_no_auth(SavedQueryController)
    async with AsyncTestClient(app=no_auth_app) as client:
        resp = await client.get("/api/v1/saved_query/")
        assert resp.status_code == 401
