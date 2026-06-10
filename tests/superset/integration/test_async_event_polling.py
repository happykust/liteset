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
"""Integration tests for async event polling REST API."""

from __future__ import annotations

import json
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock

import jwt as pyjwt
import pytest
from litestar import Litestar

# Skip msgspec validation for DI-injected mock params
from litestar._signature.model import (
    _normalize_annotation as _norm_fn,
)
from litestar.connection import ASGIConnection
from litestar.datastructures import State
from litestar.di import Provide
from litestar.middleware.authentication import (
    AbstractAuthenticationMiddleware,
    AuthenticationResult,
)
from litestar.testing import AsyncTestClient

from superset.async_events.manager import AsyncEventManager
from superset.controllers.async_event import AsyncEventController

_SKIP_VALIDATION_NAMES: set[str] = _norm_fn.__globals__["SKIP_VALIDATION_NAMES"]
_DI_PARAMS = frozenset({"event_manager", "current_user"})

# Shared signing key + channel used to mint a valid ``async-token`` cookie so
# the polling endpoint resolves a channel id and returns 200 (instead of the
# 401 it raises 1:1-with-upstream when the cookie is missing/invalid).
_JWT_SECRET = "test-secret-key-at-least-32-bytes-long-for-gaq"
_CHANNEL_ID = "test-channel-1"


def _mint_async_token() -> str:
    """Encode an ``async-token`` JWT the same way ``AsyncTokenMiddleware`` does."""
    return pyjwt.encode(
        {"channel": _CHANNEL_ID, "sub": "42"},
        _JWT_SECRET,
        algorithm="HS256",
    )


class _MockAuthMiddleware(AbstractAuthenticationMiddleware):
    """Minimal auth middleware that sets a mock user on every request."""

    async def authenticate_request(
        self, connection: ASGIConnection
    ) -> AuthenticationResult:
        user = MagicMock()
        user.id = 42
        user.is_authenticated = True
        # Grant the ``can_list`` permission on AsyncEventsRestApi so the
        # ``require_permission`` guard on the polling endpoint passes.
        user.permissions = {("can_list", "AsyncEventsRestApi")}
        user.roles = []
        return AuthenticationResult(user=user, auth=None)


@contextmanager
def _skip_di_validation():
    _SKIP_VALIDATION_NAMES.update(_DI_PARAMS)
    try:
        yield
    finally:
        _SKIP_VALIDATION_NAMES.difference_update(_DI_PARAMS)


@pytest.fixture
def mock_redis() -> AsyncMock:
    redis = AsyncMock()
    redis.xrange = AsyncMock(
        return_value=[
            (
                "1607477697866-0",
                {
                    "data": json.dumps(
                        {
                            "channel_id": "ch-1",
                            "job_id": "job-1",
                            "user_id": 42,
                            "status": "done",
                            "errors": [],
                            "result_url": "/api/v1/chart/data/cache-key-123",
                        }
                    )
                },
            ),
        ]
    )
    return redis


@pytest.fixture
def event_manager(mock_redis: AsyncMock) -> AsyncEventManager:
    return AsyncEventManager(redis=mock_redis)


@pytest.fixture
async def client(event_manager: AsyncEventManager):
    async def provide_event_manager() -> AsyncEventManager:
        return event_manager

    async def provide_current_user() -> MagicMock:
        user = MagicMock()
        user.id = 42
        return user

    # The polling endpoint reads the JWT signing key off ``app.state.settings``;
    # provide one matching the cookie we mint below.
    settings = MagicMock()
    settings.secret_key = _JWT_SECRET
    settings.global_async_queries_jwt_secret = None
    settings.global_async_queries_jwt_cookie_name = "async-token"

    with _skip_di_validation():
        app = Litestar(
            route_handlers=[AsyncEventController],
            dependencies={
                "event_manager": Provide(provide_event_manager),
                "current_user": Provide(provide_current_user),
            },
            middleware=[_MockAuthMiddleware],
            state=State({"settings": settings}),
        )
    async with AsyncTestClient(app) as tc:
        # Present a valid async-token cookie so channel resolution succeeds.
        tc.cookies.set("async-token", _mint_async_token())
        yield tc


async def test_polling_returns_events(client: AsyncTestClient):
    resp = await client.get("/api/v1/async_event/")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["result"]) == 1
    assert data["result"][0]["status"] == "done"
    assert data["result"][0]["result_url"] == "/api/v1/chart/data/cache-key-123"


async def test_polling_with_last_id(client: AsyncTestClient, mock_redis: AsyncMock):
    resp = await client.get("/api/v1/async_event/?last_id=1607477697866-0")
    assert resp.status_code == 200
    # Verify xrange was called with incremented ID
    call_args = mock_redis.xrange.call_args
    assert "1607477697866-1" in str(call_args)


async def test_polling_empty_response(client: AsyncTestClient, mock_redis: AsyncMock):
    mock_redis.xrange.return_value = []
    resp = await client.get("/api/v1/async_event/")
    assert resp.status_code == 200
    assert resp.json()["result"] == []
