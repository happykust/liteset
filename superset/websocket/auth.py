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
"""WebSocket authentication utilities.

Litestar's AbstractAuthenticationMiddleware only handles HTTP requests.
WebSocket connections authenticate via JWT token passed as a query parameter
during the handshake, or via an HTTP-only cookie as a fallback.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from http.cookies import SimpleCookie
from typing import Any

import jwt as pyjwt

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WebSocketAuthResult:
    """Result of WebSocket authentication."""

    user_id: int
    channel: str


def validate_origin(origin: str, allowed_origins: list[str]) -> bool:
    """Validate the Origin header against allowed origins.

    CORS headers do not apply to WebSocket upgrade requests, so origin
    must be validated explicitly to prevent cross-site WebSocket hijacking.

    Args:
        origin: The Origin header value from the WebSocket handshake.
        allowed_origins: List of allowed origins. Empty list or ["*"] allows all.

    Returns:
        True if the origin is allowed, False otherwise.
    """
    if not allowed_origins or "*" in allowed_origins:
        return True
    return origin in allowed_origins


def _extract_cookie_token(headers: dict[str, str], cookie_name: str) -> str | None:
    """Extract a JWT token from the cookie header."""
    cookie_header = headers.get("cookie", "")
    if not cookie_header:
        return None
    cookie: SimpleCookie = SimpleCookie()
    cookie.load(cookie_header)
    morsel = cookie.get(cookie_name)
    return morsel.value if morsel else None


async def authenticate_websocket(
    socket: Any,
    jwt_secret: str,
    jwt_algorithm: str = "HS256",
    cookie_name: str = "async-token",
    session_cookie_name: str = "session",
) -> WebSocketAuthResult | None:
    """Authenticate a WebSocket connection.

    Attempts authentication in order:
    1. JWT token from query parameter ``?token=<jwt>``
    2. JWT token from HTTP-only cookie (``async-token``)
    3. Session cookie (legacy itsdangerous / Liteset JWT) — fallback for
       browser WebSocket connections that carry the standard session cookie.

    Args:
        socket: Litestar WebSocket instance (or mock with query_params/headers).
        jwt_secret: Secret key for JWT/session verification.
        jwt_algorithm: JWT algorithm (default: HS256).
        cookie_name: Name of the async-token cookie.
        session_cookie_name: Name of the session cookie (legacy compat).

    Returns:
        WebSocketAuthResult if authentication succeeds, None otherwise.
    """
    # Lowercase keys when flattening: ``socket.headers`` is a case-insensitive
    # ``Headers``/CIMultiDict, but ``dict(...)`` preserves the original casing
    # (e.g. ``Cookie``), so a later ``headers.get("cookie")`` would miss it.
    if hasattr(socket.headers, "items"):
        headers = {str(k).lower(): v for k, v in socket.headers.items()}
    else:
        headers = socket.headers

    # 1. Try query parameter first
    token = socket.query_params.get("token")

    # 2. Fallback to async-token cookie
    if not token:
        token = _extract_cookie_token(headers, cookie_name)

    if token:
        try:
            # verify_sub=False: anonymous async-token cookies carry ``sub=None``
            # (minted by AsyncTokenMiddleware, 1:1 with the original); pyjwt
            # >= 2.10 otherwise raises ``InvalidSubjectError`` on a null sub,
            # which would reject every anonymous GAQ WebSocket connection.
            payload = pyjwt.decode(
                token,
                jwt_secret,
                algorithms=[jwt_algorithm],
                options={"verify_sub": False},
            )
            # Anonymous async-token cookies are minted with ``sub=None`` (1:1
            # with the original ``async_query_manager.init_app`` which signs
            # ``{"channel": ..., "sub": get_user_id()}`` and ``get_user_id()``
            # returns ``None`` for anonymous users).  The original Node sidecar
            # routed purely on the ``channel`` claim and never required a
            # numeric ``sub``; treat a missing/None ``sub`` as the anonymous
            # user (id 0, matching the guest convention) so the socket can be
            # closed cleanly with 4401 only when truly unauthenticated rather
            # than blowing up with ``int(None) → TypeError``.
            raw_sub = payload.get("sub")
            user_id = 0 if raw_sub is None else int(raw_sub)
            channel = payload.get("channel", "")
            return WebSocketAuthResult(user_id=user_id, channel=channel)
        except (pyjwt.InvalidTokenError, KeyError, TypeError, ValueError) as exc:
            logger.debug("WebSocket JWT auth failed: %s", exc)

    # 3. Fallback to session cookie (browser WS connections carry it)
    # Return channel="" so the caller can resolve the real per-session channel
    # from Redis (key: async-channels:user:{user_id}).  Fabricating a bogus
    # "events:{user_id}" channel here would cause catch-up reads to fail because
    # AsyncTokenMiddleware stores the real channel UUID under a different key.
    session_cookie = _extract_cookie_token(headers, session_cookie_name)
    if session_cookie:
        session_user_id = _resolve_user_id_from_session(session_cookie, jwt_secret)
        if session_user_id is not None:
            return WebSocketAuthResult(
                user_id=session_user_id,
                channel="",
            )

    logger.debug("WebSocket auth failed: no valid credentials")
    return None


def _resolve_user_id_from_session(cookie: str, secret_key: str) -> int | None:
    """Extract user_id from a session cookie (JWT or itsdangerous).

    Mirrors the logic in SupersetAuthMiddleware._authenticate_cookie.
    """
    # Try JWT session first (Liteset auth controller sets this)
    try:
        payload = pyjwt.decode(cookie, secret_key, algorithms=["HS256"])
        uid = payload.get("user_id")
        if uid is not None:
            return int(uid)
    except Exception:  # noqa: S110
        pass

    # Fallback: itsdangerous (legacy session cookie)
    try:
        from superset.security.session_decoder import FlaskSessionDecoder

        decoder = FlaskSessionDecoder(secret_key=secret_key)
        uid = decoder.get_user_id(cookie)
        if uid is not None:
            return int(uid)
    except Exception:  # noqa: S110
        pass

    return None
