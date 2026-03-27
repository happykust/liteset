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
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import jwt as pyjwt
import pytest

from superset.websocket.auth import (
    authenticate_websocket,
    validate_origin,
    WebSocketAuthResult,
)


def _make_socket(
    query_params: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> MagicMock:
    """Create a mock WebSocket with query_params and headers."""
    socket = MagicMock()
    socket.query_params = query_params or {}
    socket.headers = headers or {}
    return socket


def test_validate_origin_allowed():
    assert validate_origin("https://superset.example.com", ["https://superset.example.com"]) is True


def test_validate_origin_disallowed():
    assert validate_origin("https://evil.com", ["https://superset.example.com"]) is False


def test_validate_origin_no_restrictions():
    assert validate_origin("https://anything.com", []) is True


def test_validate_origin_wildcard():
    assert validate_origin("https://anything.com", ["*"]) is True


async def test_authenticate_websocket_jwt_query_param():
    secret = "test-secret-key-that-is-32-bytes!"
    token = pyjwt.encode({"channel": "ch-1", "sub": "42"}, secret, algorithm="HS256")
    socket = _make_socket(query_params={"token": token})

    result = await authenticate_websocket(socket, jwt_secret=secret)
    assert result is not None
    assert result.user_id == 42
    assert result.channel == "ch-1"


async def test_authenticate_websocket_invalid_jwt():
    socket = _make_socket(query_params={"token": "invalid-token"})
    result = await authenticate_websocket(socket, jwt_secret="some-secret-key-32-bytes-long!!")
    assert result is None


async def test_authenticate_websocket_no_token():
    socket = _make_socket()
    result = await authenticate_websocket(socket, jwt_secret="some-secret-key-32-bytes-long!!")
    assert result is None


async def test_authenticate_websocket_expired_jwt():
    import time

    secret = "test-secret-key-that-is-32-bytes!"
    token = pyjwt.encode(
        {"channel": "ch-1", "sub": "42", "exp": int(time.time()) - 100},
        secret,
        algorithm="HS256",
    )
    socket = _make_socket(query_params={"token": token})
    result = await authenticate_websocket(socket, jwt_secret=secret)
    assert result is None


async def test_authenticate_websocket_cookie_fallback():
    secret = "test-secret-key-that-is-32-bytes!"
    token = pyjwt.encode({"channel": "ch-1", "sub": "42"}, secret, algorithm="HS256")
    socket = _make_socket(headers={"cookie": f"async-token={token}"})
    result = await authenticate_websocket(socket, jwt_secret=secret, cookie_name="async-token")
    assert result is not None
    assert result.user_id == 42
