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
"""Integration tests for chart data, filter_state, and permalink endpoints."""

from __future__ import annotations

import pytest
from litestar.testing import AsyncTestClient

from superset.controllers.chart import ChartController
from tests.superset.integration.conftest import create_test_app


@pytest.fixture
def app():
    return create_test_app(ChartController)


async def test_post_chart_data_datasource_not_found(app):
    """POST /api/v1/chart/data returns 404 when datasource doesn't exist."""
    async with AsyncTestClient(app=app) as client:
        payload = {
            "datasource": {"id": 1, "type": "table"},
            "queries": [
                {
                    "columns": ["col1"],
                    "metrics": ["count"],
                    "filters": [],
                }
            ],
        }
        resp = await client.post("/api/v1/chart/data", json=payload)
        # MockDAO.get_datasource returns None → ObjectNotFoundError → 404
        assert resp.status_code == 404


async def test_get_chart_data_not_found(app):
    """GET /api/v1/chart/999/data/ returns 404 for non-existent chart."""
    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/api/v1/chart/999/data/")
        assert resp.status_code == 404


async def test_get_cached_chart_data_miss(app):
    """GET /api/v1/chart/data/nonexistent returns cache miss."""
    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/api/v1/chart/data/nonexistent")
        assert resp.status_code == 200
        data = resp.json()
        assert data["result"] == []
        assert data["message"] == "Cache miss"
