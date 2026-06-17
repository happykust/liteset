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
"""Integration tests for QueryController.

Tests the full HTTP pipeline with mocked DAO dependencies.
"""

from __future__ import annotations

import pytest
from litestar.testing import AsyncTestClient

from superset.controllers.query import QueryController
from tests.superset.integration.conftest import create_test_app, create_test_app_no_auth


@pytest.fixture
def app():
    return create_test_app(QueryController)


async def test_get_query_list(app):
    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/api/v1/query/")
        assert resp.status_code == 200
        data = resp.json()
        assert "result" in data
        assert "count" in data
        assert data["result"] == []
        assert data["count"] == 0


async def test_get_query_info(app):
    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/api/v1/query/_info")
        assert resp.status_code == 200
        data = resp.json()
        assert "permissions" in data
        assert "can_read" in data["permissions"]


async def test_get_query_info_keys_filter():
    """_info handler forwards rison_params to get_info_payload so the keys filter works.

    When the client sends ``keys`` in the rison payload the response is filtered to
    include only the requested top-level keys.  Without the fix the full payload was
    returned regardless of the ``keys`` parameter (rison_params was never forwarded).
    """
    from unittest.mock import AsyncMock, MagicMock

    from superset.controllers.query import QueryController

    # Invoke the underlying handler function directly (bypassing HTTP/DI layer)
    # so that we can pass rison_params explicitly and verify the filter is applied.
    info_fn = QueryController.info.fn

    mock_dao = MagicMock()
    mock_sm = MagicMock()
    mock_sm.can_access = AsyncMock(return_value=True)
    mock_user = MagicMock()
    mock_user.id = 1

    result = await info_fn(
        self=None,
        dao=mock_dao,
        security_manager=mock_sm,
        current_user=mock_user,
        rison_params={"keys": ["permissions"]},
    )

    # Only the requested key must be present (keys filter activated)
    assert list(result.keys()) == ["permissions"], (
        f"Expected only 'permissions' key in result, got: {list(result.keys())}"
    )
    assert "can_read" in result["permissions"]


async def test_get_query_updated_since(app):
    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/api/v1/query/updated_since")
        assert resp.status_code == 200
        data = resp.json()
        assert "result" in data
        assert isinstance(data["result"], list)


async def test_unauthenticated_returns_401():
    no_auth_app = create_test_app_no_auth(QueryController)
    async with AsyncTestClient(app=no_auth_app) as client:
        resp = await client.get("/api/v1/query/")
        assert resp.status_code == 401
