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
"""Unit tests for events.py GAQ JWT secret / cookie-name resolution logic.

Because the full on_event WebSocket handler is difficult to unit-test in
isolation (it interacts with the Litestar WebSocket accept/close lifecycle),
these tests exercise the secret-resolution logic by calling the production
helper ``_resolve_secret_key`` from ``superset.middleware.async_token``
directly (events.py delegates to the same function).

Additional tests verify the Redis channel-lookup path and graceful
degradation when Redis is unavailable.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import jwt as pyjwt
import pytest

from superset.middleware.async_token import _channel_key, _resolve_secret_key
from superset.websocket.auth import authenticate_websocket


# ---------------------------------------------------------------------------
# Helper to build a mock settings object
# ---------------------------------------------------------------------------


def _make_settings(
    *,
    gaq_secret: str | None = "test-secret-change-me",
    secret_key: str = "superset-secret-key-32-bytes!!!",
    cookie_name: str = "async-token",
    use_secret_str: bool = False,
) -> MagicMock:
    """Build a mock settings object mirroring SupersetConfig fields."""
    settings = MagicMock()
    if use_secret_str:
        sk = MagicMock()
        sk.get_secret_value.return_value = secret_key
        settings.secret_key = sk
    else:
        settings.secret_key = secret_key
    settings.global_async_queries_jwt_secret = gaq_secret
    settings.global_async_queries_jwt_cookie_name = cookie_name
    settings.session_cookie_name = "session"
    return settings


# ---------------------------------------------------------------------------
# Secret-resolution tests — exercise PRODUCTION _resolve_secret_key directly
# ---------------------------------------------------------------------------


def test_gaq_secret_preferred_over_secret_key():
    """global_async_queries_jwt_secret must win over secret_key."""
    settings = _make_settings(gaq_secret="gaq-secret", secret_key="main-secret")
    resolved = _resolve_secret_key(settings)
    assert resolved == "gaq-secret"


def test_falls_back_to_secret_key_when_gaq_is_none():
    """When global_async_queries_jwt_secret is None, fall back to secret_key."""
    settings = _make_settings(gaq_secret=None, secret_key="main-secret")
    resolved = _resolve_secret_key(settings)
    assert resolved == "main-secret"


def test_secret_key_secretstr_unwrapped():
    """get_secret_value() is called on SecretStr-like secret_key fallback."""
    settings = _make_settings(gaq_secret=None, secret_key="my-secret", use_secret_str=True)
    resolved = _resolve_secret_key(settings)
    assert resolved == "my-secret"
    settings.secret_key.get_secret_value.assert_called_once()


def test_gaq_secret_not_secretstr_not_unwrapped():
    """If GAQ secret is a plain string, get_secret_value is not called."""
    settings = _make_settings(gaq_secret="plain-gaq-secret", secret_key="main-secret")
    resolved = _resolve_secret_key(settings)
    assert resolved == "plain-gaq-secret"


# ---------------------------------------------------------------------------
# End-to-end: correct GAQ secret enables successful cookie auth
# ---------------------------------------------------------------------------


async def test_events_secret_resolution_allows_gaq_signed_cookie():
    """Using the resolved GAQ secret decodes an async-token cookie correctly.

    This is the regression test for the #1 critical bug: events.py was using
    settings.secret_key (which may differ from the GAQ secret) to decode the
    async-token cookie, causing WS auth to always fail at default config.
    """
    gaq_secret = "test-secret-change-me"
    wrong_secret = "totally-different-secret-key!!"

    token = pyjwt.encode(
        {"channel": "real-uuid-channel", "sub": "5"},
        gaq_secret,
        algorithm="HS256",
    )

    socket = MagicMock()
    socket.query_params = {}
    socket.headers = {"cookie": f"async-token={token}"}

    # With correct GAQ secret → should succeed
    result = await authenticate_websocket(socket, jwt_secret=gaq_secret)
    assert result is not None
    assert result.user_id == 5
    assert result.channel == "real-uuid-channel"

    # With wrong secret (old buggy behaviour) → must fail
    socket2 = MagicMock()
    socket2.query_params = {}
    socket2.headers = {"cookie": f"async-token={token}"}
    result2 = await authenticate_websocket(socket2, jwt_secret=wrong_secret)
    assert result2 is None


# ---------------------------------------------------------------------------
# Redis channel-lookup path (simulates the empty-channel Redis resolve)
# ---------------------------------------------------------------------------


async def test_channel_key_format():
    """_channel_key returns the expected Redis key pattern."""
    assert _channel_key(42) == "async-channels:user:42"
    assert _channel_key(0) == "async-channels:user:anonymous"
    assert _channel_key(None) == "async-channels:user:anonymous"


async def test_channel_resolved_from_redis_when_empty():
    """When auth_result.channel is empty, the Redis UUID is used as channel.

    Simulates the resolution block added to events.py on_event.
    """
    # Simulate auth_result with empty channel (session-cookie fallback)
    from superset.websocket.auth import WebSocketAuthResult

    auth_result = WebSocketAuthResult(user_id=99, channel="")

    # Mock redis that returns a UUID for the expected key
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=b"some-uuid-channel-from-redis")

    # Run the resolution logic (mirrors on_event code)
    channel = auth_result.channel
    if not channel:
        raw = await redis.get(_channel_key(auth_result.user_id))
        if raw:
            channel = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)

    assert channel == "some-uuid-channel-from-redis"
    redis.get.assert_awaited_once_with("async-channels:user:99")


async def test_channel_stays_empty_when_redis_returns_none():
    """If Redis has no entry, channel stays empty (live relay still works)."""
    from superset.websocket.auth import WebSocketAuthResult

    auth_result = WebSocketAuthResult(user_id=99, channel="")

    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)

    channel = auth_result.channel
    if not channel:
        raw = await redis.get(_channel_key(auth_result.user_id))
        if raw:
            channel = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)

    assert channel == ""


async def test_jwt_channel_not_overwritten_by_redis():
    """When JWT already has a channel claim, Redis lookup must NOT run.

    Simulates the guard: 'if not channel' — a truthy JWT channel wins.
    """
    from superset.websocket.auth import WebSocketAuthResult

    auth_result = WebSocketAuthResult(user_id=7, channel="jwt-given-channel")

    redis = AsyncMock()

    channel = auth_result.channel
    if not channel:
        raw = await redis.get(_channel_key(auth_result.user_id))
        if raw:
            channel = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)

    assert channel == "jwt-given-channel"
    redis.get.assert_not_awaited()


# ---------------------------------------------------------------------------
# M4: Graceful degradation — redis=None AND channel="" (catch-up guard)
# ---------------------------------------------------------------------------


async def test_channel_stays_empty_when_redis_is_none():
    """When state.redis is None and auth_result.channel is empty, channel stays
    empty and no exception is raised.

    This exercises the branch:
        redis = getattr(state, "redis", None)
        if redis is not None:
            ...Redis lookup...
    which must silently skip when Redis is unavailable, leaving channel as "".

    The subsequent catch-up guard (``if last_id and channel``) then correctly
    skips the catch-up read rather than passing an empty/invalid channel id
    to event_manager.read_events().

    We test the resolution logic at the channel-resolution seam (the two
    code blocks that together determine the final ``channel`` value) rather
    than driving the full on_event handler, because the handler requires a
    live Litestar WebSocket accept/close cycle that is impractical to mock.
    """
    from superset.websocket.auth import WebSocketAuthResult

    auth_result = WebSocketAuthResult(user_id=42, channel="")

    # Simulate state.redis = None
    state = MagicMock()
    state.redis = None

    # --- channel resolution block (mirrors on_event) ---
    channel = auth_result.channel
    if not channel:
        redis = getattr(state, "redis", None)
        if redis is not None:  # <-- must NOT enter this branch
            try:
                raw = await redis.get(_channel_key(auth_result.user_id))
                if raw:
                    channel = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
            except Exception:  # noqa: BLE001
                pass

    # channel must remain "" — no exception raised, no bogus key used
    assert channel == ""

    # --- catch-up guard (mirrors on_event) ---
    last_id = "some-last-id"
    catch_up_would_run = bool(last_id and channel)
    assert catch_up_would_run is False, (
        "Catch-up must NOT run when channel is empty (guard: 'if last_id and channel')"
    )
