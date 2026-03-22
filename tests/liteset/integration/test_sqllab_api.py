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
"""Integration tests for SqlLabController.

Tests the full HTTP pipeline with mocked DAO dependencies.
"""

from __future__ import annotations

import pytest
from litestar.testing import AsyncTestClient

from liteset.controllers.sqllab import SqlLabController
from tests.liteset.integration.conftest import create_test_app, create_test_app_no_auth


@pytest.fixture
def app():
    return create_test_app(SqlLabController)


async def test_get_sqllab_bootstrap(app):
    """GET /api/v1/sqllab/ returns bootstrap data with user info."""
    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/api/v1/sqllab/")
        assert resp.status_code == 200
        data = resp.json()
        assert "user" in data
        assert data["user"]["id"] == 1


async def test_post_format_sql(app):
    """POST /api/v1/sqllab/format_sql/ formats SQL."""
    async with AsyncTestClient(app=app) as client:
        resp = await client.post(
            "/api/v1/sqllab/format_sql/",
            json={"sql": "SELECT * FROM t"},
        )
        # Litestar returns 201 for POST by default
        assert resp.status_code == 201
        data = resp.json()
        assert "result" in data


async def test_get_sqllab_results_no_key(app):
    """GET /api/v1/sqllab/results/ with no key returns 422 validation error."""
    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/api/v1/sqllab/results/")
        # rison_params mock returns None → key="" → CommandInvalidError
        assert resp.status_code == 422


async def test_execute_sql_returns_result(app):
    """POST /api/v1/sqllab/execute/ returns query result dict.

    ExecuteSQLCommand.run() returns a stub dict with ``status``, ``data``,
    and ``columns`` keys — verifying the route is wired and the command
    response passes through the controller unchanged.
    """
    async with AsyncTestClient(app=app) as client:
        resp = await client.post(
            "/api/v1/sqllab/execute/",
            json={
                "database_id": 1,
                "sql": "SELECT 1",
                "schema": None,
                "runAsync": False,
            },
        )
        # POST returns 201 by default in Litestar
        assert resp.status_code == 201
        data = resp.json()
        assert "status" in data


async def test_get_sqllab_results_route_exists(app):
    """GET /api/v1/sqllab/results/ is a registered route."""
    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/api/v1/sqllab/results/")
        # Without rison params, key is empty -> 422 validation error
        assert resp.status_code == 422


async def test_get_sqllab_results_with_rison_key(app):
    """GET /api/v1/sqllab/results/ with rison key.

    MockDAO rison_params returns None -> empty key -> 422.
    """
    async with AsyncTestClient(app=app) as client:
        resp = await client.get(
            "/api/v1/sqllab/results/",
            params={"q": '(key:"abc123")'},
        )
        assert resp.status_code == 422


async def test_unauthenticated_returns_401():
    """GET /api/v1/sqllab/ without credentials returns 401."""
    no_auth_app = create_test_app_no_auth(SqlLabController)
    async with AsyncTestClient(app=no_auth_app) as client:
        resp = await client.get("/api/v1/sqllab/")
        assert resp.status_code == 401
