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
"""Unit tests for the async-events polling endpoint.

The endpoint resolves the channel id from the request's ``async-token`` JWT
cookie — 1:1 with the original Flask
``AsyncEventsRestApi.events`` which called
``async_query_manager.parse_channel_id_from_request(request)`` and returned
``self.response_401()`` on an ``AsyncQueryTokenException`` (missing / invalid
token).  These tests mint a real ``async-token`` cookie (the exact shape
:class:`superset.middleware.async_token.AsyncTokenMiddleware` mints) and drive
the handler directly (its Litestar ``.fn``) with mocked DI dependencies,
mirroring ``test_async_chart_data_channel.py``.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import jwt as pyjwt
import pytest
from litestar.exceptions import NotAuthorizedException

from superset.controllers.async_event import AsyncEventController

GAQ_SECRET = "test-gaq-secret-at-least-16-chars"
COOKIE_NAME = "async-token"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_raw_method(controller_cls: type, method_name: str) -> Any:
    """Return the underlying async function from a Litestar-decorated handler."""
    handler = getattr(controller_cls, method_name)
    return handler.fn if hasattr(handler, "fn") else handler


_get_events = _get_raw_method(AsyncEventController, "get_events")


def _mint_async_token(channel: str, secret: str = GAQ_SECRET) -> str:
    """Mint an ``async-token`` JWT exactly as AsyncTokenMiddleware does."""
    token = pyjwt.encode(
        {"channel": channel, "sub": "42"},
        secret,
        algorithm="HS256",
    )
    return token.decode("ascii") if isinstance(token, bytes) else token


def _cookie_headers(channel: str, secret: str = GAQ_SECRET) -> list[tuple]:
    """Build an ASGI ``headers`` list carrying the async-token cookie."""
    raw = f"{COOKIE_NAME}={_mint_async_token(channel, secret)}"
    return [(b"cookie", raw.encode("utf-8"))]


def _make_request(headers: list[tuple]) -> MagicMock:
    """Mock a Litestar request exposing ``scope['headers']`` and app settings.

    The controller reads the GAQ secret / cookie name off
    ``request.app.state.settings`` — a plain string secret (not a SecretStr)
    matches the ``_resolve_secret_key`` happy path.
    """
    request = MagicMock()
    request.scope = {"headers": headers}
    settings = MagicMock()
    settings.global_async_queries_jwt_secret = GAQ_SECRET
    settings.global_async_queries_jwt_cookie_name = COOKIE_NAME
    request.app.state.settings = settings
    return request


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def controller() -> AsyncEventController:
    return AsyncEventController(owner=MagicMock())


@pytest.fixture
def mock_user() -> MagicMock:
    user = MagicMock()
    user.id = 42
    user.is_authenticated = True
    return user


@pytest.fixture
def mock_event_manager() -> AsyncMock:
    manager = AsyncMock()
    manager.read_events = AsyncMock(
        return_value=[
            {
                "id": "1607477697866-0",
                "channel_id": "ch-1",
                "job_id": "job-1",
                "user_id": 42,
                "status": "done",
                "errors": [],
                "result_url": "/api/v1/chart/data/cache-key-123",
            }
        ]
    )
    return manager


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_get_events_no_last_id(
    controller: AsyncEventController,
    mock_event_manager: AsyncMock,
    mock_user: MagicMock,
):
    """A valid async-token resolves the channel and returns its events."""
    channel = str(uuid.uuid4())
    request = _make_request(_cookie_headers(channel))

    result = await _get_events(
        controller,
        request=request,
        event_manager=mock_event_manager,
        current_user=mock_user,
    )

    assert result["result"][0]["status"] == "done"
    mock_event_manager.read_events.assert_called_once()
    _, kwargs = mock_event_manager.read_events.call_args
    # Channel comes from the cookie's ``channel`` claim, last_id defaults to None.
    assert kwargs["channel_id"] == channel
    assert kwargs["last_id"] is None


async def test_get_events_with_last_id(
    controller: AsyncEventController,
    mock_event_manager: AsyncMock,
    mock_user: MagicMock,
):
    """``last_id`` is forwarded to ``read_events``."""
    channel = str(uuid.uuid4())
    request = _make_request(_cookie_headers(channel))

    await _get_events(
        controller,
        request=request,
        event_manager=mock_event_manager,
        current_user=mock_user,
        last_id="1607477697866-0",
    )

    mock_event_manager.read_events.assert_called_once()
    _, kwargs = mock_event_manager.read_events.call_args
    assert kwargs["last_id"] == "1607477697866-0"
    assert kwargs["channel_id"] == channel


async def test_get_events_empty(
    controller: AsyncEventController,
    mock_event_manager: AsyncMock,
    mock_user: MagicMock,
):
    """An empty stream yields ``{"result": []}``."""
    mock_event_manager.read_events.return_value = []
    request = _make_request(_cookie_headers(str(uuid.uuid4())))

    result = await _get_events(
        controller,
        request=request,
        event_manager=mock_event_manager,
        current_user=mock_user,
    )

    assert result["result"] == []


async def test_get_events_missing_token_401(
    controller: AsyncEventController,
    mock_event_manager: AsyncMock,
    mock_user: MagicMock,
):
    """A missing async-token cookie yields HTTP 401.

    1:1 with the original ``AsyncEventsRestApi.events``: an
    ``AsyncQueryTokenException`` (no / unparseable token) maps to
    ``response_401()``.  No Redis read is attempted.
    """
    request = _make_request([])  # no cookie header

    with pytest.raises(NotAuthorizedException):
        await _get_events(
            controller,
            request=request,
            event_manager=mock_event_manager,
            current_user=mock_user,
        )
    mock_event_manager.read_events.assert_not_called()


async def test_get_events_invalid_token_401(
    controller: AsyncEventController,
    mock_event_manager: AsyncMock,
    mock_user: MagicMock,
):
    """A cookie signed with the wrong secret yields HTTP 401."""
    request = _make_request(
        _cookie_headers(str(uuid.uuid4()), secret="some-other-wrong-secret-32-bytes")
    )

    with pytest.raises(NotAuthorizedException):
        await _get_events(
            controller,
            request=request,
            event_manager=mock_event_manager,
            current_user=mock_user,
        )
    mock_event_manager.read_events.assert_not_called()
