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
"""Regression tests for AsyncQueryWebSocket's security hardening.

``/ws/events`` used to be reachable in every
deployment with the shipped default secret.  Confirmed by execution: a
token forged with the default secret, with no cookies at all, was accepted
onto the global firehose channel (``channel: "full"``).  These tests cover
the two independent defects fixed in ``superset/websocket/events.py``:

1. the channel-token verifier failing closed on the shipped default JWT
   secret rather than verifying against a public string, and
2. a channel claim of ``"full"`` (colliding with the reserved
   global-firehose stream key) being rejected instead of accepted.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import jwt as pyjwt
import pytest
from litestar import Litestar
from litestar.datastructures import State
from litestar.exceptions import WebSocketException
from litestar.testing import AsyncTestClient

from superset.websocket.events import AsyncQueryWebSocket

DEFAULT_GAQ_SECRET = "test-secret-change-me"  # noqa: S105 — the shipped default
REAL_SECRET = "a-real-32-byte-or-longer-secret!"  # noqa: S105


def _make_settings(**overrides: object) -> MagicMock:
    settings = MagicMock()
    settings.secret_key = REAL_SECRET
    settings.global_async_queries_jwt_secret = DEFAULT_GAQ_SECRET
    settings.global_async_queries_jwt_cookie_name = "async-token"
    settings.session_cookie_name = "session"
    settings.cors_allow_origins = []
    settings.cors_options = {}
    settings.max_ws_per_user = 10
    # The frontend never sends ``?token=`` in production (see
    # global_async_queries_ws_allow_query_token's docstring in config.py),
    # but these tests drive the handshake via the query param — the
    # simplest way to exercise the full on_event() code path through
    # AsyncTestClient — so opt in explicitly.
    settings.global_async_queries_ws_allow_query_token = True
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


def _make_app(**settings_overrides: object) -> Litestar:
    settings = _make_settings(**settings_overrides)
    mock_redis = AsyncMock()
    return Litestar(
        route_handlers=[AsyncQueryWebSocket],
        state=State(
            {
                "settings": settings,
                "redis": mock_redis,
                "active_websockets": {},
            }
        ),
    )


async def _connect_and_receive(client: AsyncTestClient, url: str) -> None:
    ws = await client.websocket_connect(url, headers={})
    with ws:
        ws.receive_json()


def _close_code(exc: WebSocketException) -> int | None:
    """Extract the WebSocket close code — mirrors the helper in
    ``tests/superset/integration/test_websocket.py``."""
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return code
    detail = getattr(exc, "detail", None)
    if isinstance(detail, int):
        return detail
    if isinstance(detail, str) and detail.isdigit():
        return int(detail)
    return None


async def test_default_secret_token_rejected():
    """A token forged with the shipped default GAQ secret must be rejected.

    The verifier must fail closed instead of decoding against a public
    string — even with a syntactically valid token and no cookies at all.
    """
    app = _make_app(global_async_queries_jwt_secret=DEFAULT_GAQ_SECRET)
    token = pyjwt.encode(
        {"channel": "attacker-controlled-channel", "sub": None},
        DEFAULT_GAQ_SECRET,
        algorithm="HS256",
    )
    async with AsyncTestClient(app) as client:
        with pytest.raises(WebSocketException) as exc_info:
            await _connect_and_receive(client, f"/ws/events?token={token}")
        assert _close_code(exc_info.value) == 4401


async def test_missing_secret_token_rejected():
    """An unset GAQ JWT secret must also fail closed (not fall through to
    ``secret_key``, which callers might reasonably assume is unrelated)."""
    app = _make_app(global_async_queries_jwt_secret="")
    token = pyjwt.encode(
        {"channel": "some-channel", "sub": None}, REAL_SECRET, algorithm="HS256"
    )
    async with AsyncTestClient(app) as client:
        with pytest.raises(WebSocketException) as exc_info:
            await _connect_and_receive(client, f"/ws/events?token={token}")
        assert _close_code(exc_info.value) == 4401


async def test_full_channel_claim_rejected():
    """A channel claim of "full" collides with the reserved global-firehose
    stream key and must be rejected — accepting it would grant a live read
    of every user's async-query events.
    """
    app = _make_app(global_async_queries_jwt_secret=REAL_SECRET)
    token = pyjwt.encode(
        {"channel": "full", "sub": "1"},
        REAL_SECRET,
        algorithm="HS256",
    )
    async with AsyncTestClient(app) as client:
        with pytest.raises(WebSocketException) as exc_info:
            await _connect_and_receive(client, f"/ws/events?token={token}")
        assert _close_code(exc_info.value) == 4400


async def test_normal_channel_with_real_secret_not_rejected_at_auth_stage():
    """A properly signed token with an ordinary channel must pass BOTH the
    secret and channel-collision checks (only the per-user-limit / relay
    stages remain) — the hardening above must not reject legitimate
    connections.  Exercised by exhausting the per-user connection limit
    (0) so the handler still closes deterministically, but via
    ``WS_CLOSE_TOO_MANY_CONNECTIONS`` (4429) rather than 4401/4400 —
    proving auth and the channel check both passed first.
    """
    app = _make_app(global_async_queries_jwt_secret=REAL_SECRET, max_ws_per_user=0)
    token = pyjwt.encode(
        {"channel": "real-channel-uuid", "sub": "1"},
        REAL_SECRET,
        algorithm="HS256",
    )
    async with AsyncTestClient(app) as client:
        with pytest.raises(WebSocketException) as exc_info:
            await _connect_and_receive(client, f"/ws/events?token={token}")
        assert _close_code(exc_info.value) == 4429
