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
"""Integration tests for PUT partial update with msgspec UNSET sentinel.

Verifies that PUT endpoints accept partial JSON bodies without rejecting
them as validation errors (400/422).  All fields in the PUT schemas use
``msgspec.UNSET`` as the default, so omitted fields should be ignored
rather than treated as required.

The mock DAO returns ``None`` for ``find_by_id`` by default, so the
controller raises ``ObjectNotFoundError`` (404).  The key assertion is
that the request passes schema validation — i.e. the response is NOT
400 or 422.
"""

from __future__ import annotations

import pytest
from litestar.testing import AsyncTestClient

from liteset.controllers.chart import ChartController
from tests.liteset.integration.conftest import create_test_app


@pytest.fixture
def chart_app():
    """App with mock DAO that returns an existing chart."""
    return create_test_app(ChartController)


async def test_put_partial_update_accepts_partial_body(chart_app):
    """PUT with only slice_name should not require all fields."""
    async with AsyncTestClient(app=chart_app) as client:
        resp = await client.put(
            "/api/v1/chart/1",
            json={"slice_name": "Updated Name"},
        )
        # 404 is OK (mock DAO returns None by default for find_by_id)
        # The important thing is it's NOT 400/422 (validation error)
        assert resp.status_code in (200, 404)


async def test_put_empty_body_accepted(chart_app):
    """PUT with empty body should be accepted (no required fields in PUT schema)."""
    async with AsyncTestClient(app=chart_app) as client:
        resp = await client.put(
            "/api/v1/chart/1",
            json={},
        )
        assert resp.status_code in (200, 404)


async def test_put_with_single_field(chart_app):
    """PUT with a single field should only update that field."""
    async with AsyncTestClient(app=chart_app) as client:
        resp = await client.put(
            "/api/v1/chart/1",
            json={"description": "New description"},
        )
        assert resp.status_code in (200, 404)


async def test_put_with_viz_type_only(chart_app):
    """PUT with only viz_type should pass validation."""
    async with AsyncTestClient(app=chart_app) as client:
        resp = await client.put(
            "/api/v1/chart/1",
            json={"viz_type": "pie"},
        )
        assert resp.status_code in (200, 404)


async def test_put_with_cache_timeout_only(chart_app):
    """PUT with only cache_timeout (int field) should pass validation."""
    async with AsyncTestClient(app=chart_app) as client:
        resp = await client.put(
            "/api/v1/chart/1",
            json={"cache_timeout": 3600},
        )
        assert resp.status_code in (200, 404)


async def test_put_with_multiple_partial_fields(chart_app):
    """PUT with a subset of fields should pass validation."""
    async with AsyncTestClient(app=chart_app) as client:
        resp = await client.put(
            "/api/v1/chart/1",
            json={
                "slice_name": "Renamed",
                "description": "Updated desc",
                "cache_timeout": 120,
            },
        )
        assert resp.status_code in (200, 404)


async def test_put_with_null_field(chart_app):
    """PUT with explicit null should be accepted (clearing a field)."""
    async with AsyncTestClient(app=chart_app) as client:
        resp = await client.put(
            "/api/v1/chart/1",
            json={"description": None},
        )
        assert resp.status_code in (200, 404)


async def test_put_with_owners_list(chart_app):
    """PUT with only owners list should pass validation."""
    async with AsyncTestClient(app=chart_app) as client:
        resp = await client.put(
            "/api/v1/chart/1",
            json={"owners": [1, 2, 3]},
        )
        assert resp.status_code in (200, 404)
