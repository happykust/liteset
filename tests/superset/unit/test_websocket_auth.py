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

from unittest.mock import MagicMock

import jwt as pyjwt

from superset.websocket.auth import (
    authenticate_websocket,
    validate_origin,
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
    assert (
        validate_origin(
            "https://superset.example.com", ["https://superset.example.com"]
        )
        is True
    )


def test_validate_origin_disallowed():
    assert (
        validate_origin("https://evil.com", ["https://superset.example.com"]) is False
    )


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
    result = await authenticate_websocket(
        socket, jwt_secret="some-secret-key-32-bytes-long!!"
    )
    assert result is None


async def test_authenticate_websocket_no_token():
    socket = _make_socket()
    result = await authenticate_websocket(
        socket, jwt_secret="some-secret-key-32-bytes-long!!"
    )
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
    result = await authenticate_websocket(
        socket, jwt_secret=secret, cookie_name="async-token"
    )
    assert result is not None
    assert result.user_id == 42


async def test_authenticate_websocket_anonymous_null_sub():
    """Anonymous async-token cookies carry ``sub=None``.

    Regression: pyjwt >= 2.10 raises ``InvalidSubjectError`` on a null sub
    unless ``options={"verify_sub": False}`` is passed; without it every
    anonymous GAQ WebSocket connection failed to authenticate.
    """
    secret = "test-secret-key-that-is-32-bytes!"
    token = pyjwt.encode({"channel": "ch-anon", "sub": None}, secret, algorithm="HS256")
    socket = _make_socket(query_params={"token": token})
    result = await authenticate_websocket(socket, jwt_secret=secret)
    assert result is not None
    assert result.user_id == 0  # anonymous → id 0
    assert result.channel == "ch-anon"


# ---------------------------------------------------------------------------
# New tests for Task 1: GAQ JWT secret + cookie name + session fallback fixes
# ---------------------------------------------------------------------------


async def test_gaq_secret_differs_from_secret_key_succeeds():
    """async-token signed with GAQ secret must be accepted when passed as jwt_secret.

    This mirrors the fix in events.py: use global_async_queries_jwt_secret
    (not secret_key) to verify the async-token cookie.
    """
    gaq_secret = "test-secret-change-me"  # default GAQ secret
    secret_key = "a-completely-different-secret!!"  # different from GAQ secret

    token = pyjwt.encode(
        {"channel": "uuid-channel-1", "sub": "7"},
        gaq_secret,
        algorithm="HS256",
    )
    # Token is in the cookie, not the query param
    socket = _make_socket(headers={"cookie": f"async-token={token}"})

    # Verify with GAQ secret succeeds
    result = await authenticate_websocket(socket, jwt_secret=gaq_secret)
    assert result is not None
    assert result.user_id == 7
    assert result.channel == "uuid-channel-1"

    # Verify that using wrong secret_key fails (proves the secrets really differ)
    socket2 = _make_socket(headers={"cookie": f"async-token={token}"})
    result2 = await authenticate_websocket(socket2, jwt_secret=secret_key)
    assert result2 is None


async def test_custom_cookie_name_honored():
    """Authentication succeeds with a non-default cookie name."""
    secret = "test-secret-key-that-is-32-bytes!"
    custom_cookie = "my-custom-async-token"
    token = pyjwt.encode(
        {"channel": "ch-custom", "sub": "99"}, secret, algorithm="HS256"
    )

    # Present token under custom cookie name
    socket = _make_socket(headers={"cookie": f"{custom_cookie}={token}"})
    result = await authenticate_websocket(
        socket, jwt_secret=secret, cookie_name=custom_cookie
    )
    assert result is not None
    assert result.user_id == 99
    assert result.channel == "ch-custom"


async def test_custom_cookie_name_mismatch_fails():
    """Auth fails when the cookie name does not match the expected one."""
    secret = "test-secret-key-that-is-32-bytes!"
    token = pyjwt.encode({"channel": "ch-x", "sub": "55"}, secret, algorithm="HS256")

    # Token is in "async-token" but caller says to look in "other-name"
    socket = _make_socket(headers={"cookie": f"async-token={token}"})
    result = await authenticate_websocket(
        socket, jwt_secret=secret, cookie_name="other-name"
    )
    # No query param, wrong cookie name → falls to session-cookie fallback
    # Session cookie is also absent, so result is None
    assert result is None


async def test_session_cookie_fallback_returns_empty_channel():
    """Session-cookie fallback must return channel='' (not 'events:{user_id}').

    The real per-session channel is stored in Redis under
    async-channels:user:{user_id}; fabricating 'events:{id}' here would
    cause catch-up reads against a non-existent stream.
    """
    import time

    secret = "test-session-secret-32-bytes!!!"
    # Mint a JWT-style session cookie in the exact shape
    # ``controllers.auth._create_session_cookie`` / ``utils.machine_auth``
    # produce: {"type": "session", "user_id": ..., "iat": ..., "exp": ...}.
    # A type-less / exp-less token is now rejected — see
    # ``_resolve_user_id_from_session``.
    session_token = pyjwt.encode(
        {
            "type": "session",
            "user_id": 13,
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,
        },
        secret,
        algorithm="HS256",
    )

    socket = _make_socket(headers={"cookie": f"session={session_token}"})
    result = await authenticate_websocket(
        socket,
        jwt_secret=secret,
        # Intentionally omit "async-token" cookie so path 1 & 2 both miss
        cookie_name="async-token",
        session_cookie_name="session",
    )
    assert result is not None
    assert result.user_id == 13
    # CRITICAL: channel must be empty string, NOT "events:13"
    assert result.channel == ""
    assert result.channel != f"events:{result.user_id}"


async def test_typeless_secret_key_jwt_not_accepted_as_session():
    """A SECRET_KEY-signed JWT carrying ``user_id`` but no ``type: "session"``
    claim must NOT authenticate via the session-cookie fallback.

    The database OAuth2 flow
    signs a ``state`` JWT with the SAME key/algorithm and the SAME
    ``user_id`` claim shape, then puts it in a URL query parameter sent
    off-origin to a third-party IdP — exposed in IdP logs, Referer headers,
    and browser history.  Without the ``type == "session"`` + ``require:
    ["exp"]`` check, that token would double as a fully authenticated
    WebSocket session cookie.  No itsdangerous fallback exists for this
    shape, so a type-less token must resolve to no user at all — not a
    degraded/anonymous success.
    """
    import time

    secret = "test-session-secret-32-bytes!!!"

    # Missing "type" claim (matches the OAuth2 "state" JWT shape).
    no_type_token = pyjwt.encode(
        {"user_id": 13, "exp": int(time.time()) + 3600},
        secret,
        algorithm="HS256",
    )
    socket = _make_socket(headers={"cookie": f"session={no_type_token}"})
    result = await authenticate_websocket(
        socket,
        jwt_secret=secret,
        cookie_name="async-token",
        session_cookie_name="session",
    )
    assert result is None

    # "type" present but wrong value must also be rejected.
    wrong_type_token = pyjwt.encode(
        {"type": "access", "user_id": 13, "exp": int(time.time()) + 3600},
        secret,
        algorithm="HS256",
    )
    socket2 = _make_socket(headers={"cookie": f"session={wrong_type_token}"})
    result2 = await authenticate_websocket(
        socket2,
        jwt_secret=secret,
        cookie_name="async-token",
        session_cookie_name="session",
    )
    assert result2 is None

    # Missing "exp" claim entirely must also be rejected (require: ["exp"]).
    no_exp_token = pyjwt.encode(
        {"type": "session", "user_id": 13},
        secret,
        algorithm="HS256",
    )
    socket3 = _make_socket(headers={"cookie": f"session={no_exp_token}"})
    result3 = await authenticate_websocket(
        socket3,
        jwt_secret=secret,
        cookie_name="async-token",
        session_cookie_name="session",
    )
    assert result3 is None
