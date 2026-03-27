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
"""Integration tests for WebSocket event streaming.

Tests cover:
- Unauthorized / forbidden origin rejection
- Per-user connection limit enforcement
- WebSocket auth module (unit-level, tested via controller path)

The "happy path" (connect -> receive event) is hard to test with Litestar's
synchronous WebSocketTestSession because the handler runs infinite TaskGroup
coroutines. Auth rejection tests work because the handler closes the socket
before entering the TaskGroup.

Redis is mocked to avoid external dependency in CI.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import jwt as pyjwt
import pytest
from litestar import Litestar
from litestar.datastructures import State
from litestar.testing import AsyncTestClient

from superset.websocket.events import AsyncQueryWebSocket


JWT_SECRET = "test-secret-key-that-is-32-bytes!"


def _create_token(user_id: int = 42, channel: str = "ch-1") -> str:
    return pyjwt.encode({"channel": channel, "sub": str(user_id)}, JWT_SECRET, algorithm="HS256")


def _create_settings(**overrides: object) -> MagicMock:
    settings = MagicMock()
    settings.secret_key = JWT_SECRET
    settings.cors_allow_origins = overrides.get("cors_allow_origins", [])
    settings.max_ws_per_user = overrides.get("max_ws_per_user", 10)
    return settings


@pytest.fixture
def mock_redis() -> AsyncMock:
    """Mock redis.asyncio client with pub/sub support."""
    redis = AsyncMock()
    pubsub = AsyncMock()
    pubsub.subscribe = AsyncMock()
    pubsub.unsubscribe = AsyncMock()
    pubsub.close = AsyncMock()
    redis.pubsub.return_value = pubsub
    return redis


def _make_app(**settings_overrides: object) -> tuple[Litestar, dict[object, int]]:
    """Create a Litestar app with AsyncQueryWebSocket and given settings."""
    settings = _create_settings(**settings_overrides)
    active_ws: dict[object, int] = settings_overrides.get("_active_ws", {})  # type: ignore[assignment]
    mock_redis = AsyncMock()
    app = Litestar(
        route_handlers=[AsyncQueryWebSocket],
        state=State({
            "settings": settings,
            "redis": mock_redis,
            "active_websockets": active_ws,
        }),
    )
    return app, active_ws


async def test_websocket_unauthorized_no_token():
    """Test WebSocket rejection when no token is provided."""
    app, _ = _make_app()
    async with AsyncTestClient(app) as client:
        with pytest.raises(Exception):
            ws = await client.websocket_connect("/ws/events")
            with ws:
                ws.receive_json()


async def test_websocket_unauthorized_invalid_token():
    """Test WebSocket rejection with invalid JWT."""
    app, _ = _make_app()
    async with AsyncTestClient(app) as client:
        with pytest.raises(Exception):
            ws = await client.websocket_connect("/ws/events?token=invalid")
            with ws:
                ws.receive_json()


async def test_websocket_forbidden_origin():
    """Test WebSocket rejection when origin is not allowed."""
    app, _ = _make_app(cors_allow_origins=["https://allowed.com"])
    token = _create_token()
    async with AsyncTestClient(app) as client:
        with pytest.raises(Exception):
            ws = await client.websocket_connect(
                f"/ws/events?token={token}",
                headers={"origin": "https://evil.com"},
            )
            with ws:
                ws.receive_json()


async def test_websocket_per_user_limit():
    """Test per-user connection limit enforcement."""
    active_ws: dict[object, int] = {}
    # Pre-fill with one connection for user 42
    fake_socket = MagicMock()
    active_ws[fake_socket] = 42

    settings = _create_settings(max_ws_per_user=1)
    mock_redis = AsyncMock()
    app = Litestar(
        route_handlers=[AsyncQueryWebSocket],
        state=State({
            "settings": settings,
            "redis": mock_redis,
            "active_websockets": active_ws,
        }),
    )
    token = _create_token(user_id=42)
    async with AsyncTestClient(app) as client:
        with pytest.raises(Exception):
            ws = await client.websocket_connect(f"/ws/events?token={token}")
            with ws:
                ws.receive_json()
