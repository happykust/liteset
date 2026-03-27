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
"""Integration tests for DatabaseController.

Tests the full HTTP pipeline with mocked DAO dependencies.
"""

from __future__ import annotations

import pytest
from litestar.testing import AsyncTestClient

from superset.controllers.database import DatabaseController
from tests.superset.integration.conftest import create_test_app, create_test_app_no_auth


@pytest.fixture
def app():
    return create_test_app(DatabaseController)


async def test_get_database_list(app):
    """GET /api/v1/database/ returns empty list with count."""
    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/api/v1/database/")
        assert resp.status_code == 200
        data = resp.json()
        assert "result" in data
        assert "count" in data
        assert data["result"] == []
        assert data["count"] == 0


async def test_get_database_info(app):
    """GET /api/v1/database/_info returns permissions metadata."""
    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/api/v1/database/_info")
        assert resp.status_code == 200
        data = resp.json()
        assert "permissions" in data
        assert "can_read" in data["permissions"]


async def test_get_database_available(app):
    """GET /api/v1/database/available/ returns available engines."""
    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/api/v1/database/available/")
        assert resp.status_code == 200
        data = resp.json()
        assert "databases" in data
        assert isinstance(data["databases"], list)


async def test_get_database_by_id_not_found(app):
    """GET /api/v1/database/{pk} returns 404 when not found."""
    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/api/v1/database/999")
        assert resp.status_code == 404


async def test_get_database_oauth2(app):
    """GET /api/v1/database/oauth2/ returns stub response."""
    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/api/v1/database/oauth2/")
        assert resp.status_code == 200
        data = resp.json()
        assert "message" in data


async def test_unauthenticated_returns_401():
    """GET /api/v1/database/ without credentials returns 401."""
    no_auth_app = create_test_app_no_auth(DatabaseController)
    async with AsyncTestClient(app=no_auth_app) as client:
        resp = await client.get("/api/v1/database/")
        assert resp.status_code == 401
