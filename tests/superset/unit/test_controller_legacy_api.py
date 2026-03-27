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
"""Tests for LegacyApiController."""

from __future__ import annotations

from litestar import Litestar
from litestar.testing import AsyncTestClient

import pytest

from superset.controllers.legacy_api import LegacyApiController


@pytest.fixture
def app() -> Litestar:
    return Litestar(route_handlers=[LegacyApiController])


async def test_deprecated_query(app: Litestar) -> None:
    """GET /api/v1/query/ returns deprecation warning."""
    async with AsyncTestClient(app=app) as client:
        response = await client.get("/api/v1/query/")
        assert response.status_code == 200
        data = response.json()
        assert "Deprecated" in data["message"]
        assert "sqllab" in data["message"]
        assert response.headers.get("Deprecation") == "true"
        assert response.headers.get("X-Deprecated-Endpoint") == "/api/v1/query/"


async def test_deprecated_form_data(app: Litestar) -> None:
    """GET /api/v1/form_data/ returns deprecation warning."""
    async with AsyncTestClient(app=app) as client:
        response = await client.get("/api/v1/form_data/")
        assert response.status_code == 200
        data = response.json()
        assert "Deprecated" in data["message"]
        assert "form_data" in data["message"]
        assert response.headers.get("Deprecation") == "true"
        assert (
            response.headers.get("X-Deprecated-Endpoint") == "/api/v1/form_data/"
        )


async def test_deprecated_time_range(app: Litestar) -> None:
    """GET /api/v1/time_range/ returns deprecation warning."""
    async with AsyncTestClient(app=app) as client:
        response = await client.get("/api/v1/time_range/")
        assert response.status_code == 200
        data = response.json()
        assert "Deprecated" in data["message"]
        assert response.headers.get("Deprecation") == "true"
        assert (
            response.headers.get("X-Deprecated-Endpoint")
            == "/api/v1/time_range/"
        )
