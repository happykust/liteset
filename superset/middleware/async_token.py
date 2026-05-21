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
"""Async-token JWT cookie middleware.

1:1 port of the ``@app.after_request`` handler installed by
``superset_old.async_events.async_query_manager.AsyncQueryManager
.register_request_handlers``.

Whenever an authenticated user makes a request, this middleware:

* Looks up (or mints) a stable ``channel_id`` for the user.  In the
  original Flask code the channel id was stashed on the Flask session;
  here we persist it in Redis under
  ``async-channels:user:{user_id}`` with a long TTL so that the
  channel survives across requests / process restarts.  The first
  request mints a fresh ``uuid4`` and stores it; subsequent requests
  reuse the same id.
* Encodes ``{channel, sub}`` as an HS256 JWT (the same shape the
  original used; ``sub`` is the stringified user id).
* Sets the cookie named by ``GLOBAL_ASYNC_QUERIES_JWT_COOKIE_NAME``
  (default ``async-token``) with ``HttpOnly`` plus the ``secure`` /
  ``samesite`` / ``domain`` flags from settings.

The cookie is only refreshed when:

* it is missing from the incoming request, **or**
* the cookie's ``sub`` claim does not match the authenticated user
  (i.e. the user logged out and another user logged in within the
  same browser session).

This matches the four-condition reset logic in the original
``register_request_handlers``.
"""

from __future__ import annotations

import logging
import uuid
from http.cookies import SimpleCookie
from typing import Any

import jwt as pyjwt
from litestar.middleware.base import ASGIMiddleware
from litestar.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

# Long TTL for the per-user channel id record.  The cookie itself has
# no explicit Max-Age (matches the Flask original which let the
# session cookie's lifetime govern it); if Redis evicts the entry, we
# transparently mint a new channel id on the next request.
_CHANNEL_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 days


def _channel_key(user_id: int | str | None) -> str:
    """Redis key under which the per-user channel id is persisted."""
    if user_id is None or user_id == 0:
        return "async-channels:user:anonymous"
    return f"async-channels:user:{user_id}"


def _resolve_secret_key(settings: Any) -> str:
    """Return the JWT signing key (string)."""
    secret = getattr(
        settings,
        "global_async_queries_jwt_secret",
        None,
    )
    if secret is None:
        secret = getattr(settings, "secret_key", "")
    if hasattr(secret, "get_secret_value"):
        secret = secret.get_secret_value()
    return str(secret)


def _decode_existing_cookie(
    raw: bytes | None, secret_key: str
) -> dict[str, Any] | None:
    """Decode the incoming ``async-token`` cookie if present.

    Returns the decoded payload dict, or ``None`` if the cookie is
    missing / malformed / signed with a different key.
    """
    if not raw:
        return None
    cookie_header = raw.decode("utf-8", errors="replace")
    cookie: SimpleCookie = SimpleCookie()
    try:
        cookie.load(cookie_header)
    except Exception:  # noqa: BLE001
        return None
    morsel = None
    for name in ("async-token",):
        morsel = cookie.get(name)
        if morsel:
            break
    if morsel is None:
        return None
    try:
        # verify_sub=False: the anonymous cookie carries ``sub=None`` (1:1 with
        # the original Flask handler); pyjwt >= 2.10 otherwise rejects a null
        # sub, which would force a needless re-mint on every anonymous response.
        return pyjwt.decode(
            morsel.value,
            secret_key,
            algorithms=["HS256"],
            options={"verify_sub": False},
        )
    except pyjwt.PyJWTError:
        return None


def _build_set_cookie(
    name: str,
    token: str,
    *,
    secure: bool,
    samesite: str | None,
    domain: str | None,
) -> bytes:
    """Compose a ``Set-Cookie`` header value for the async-token JWT.

    Mirrors the original Flask ``response.set_cookie`` invocation:
    ``HttpOnly`` is always set; ``Secure``, ``SameSite`` and ``Domain``
    are conditional on the matching settings.
    """
    parts: list[str] = [f"{name}={token}"]
    parts.append("Path=/")
    parts.append("HttpOnly")
    if secure:
        parts.append("Secure")
    if samesite:
        parts.append(f"SameSite={samesite}")
    if domain:
        parts.append(f"Domain={domain}")
    return "; ".join(parts).encode("ascii")


async def _resolve_channel_id(
    app_state: Any,
    user_id: int | str,
) -> str:
    """Return the stable channel id for ``user_id``, creating one if needed.

    Uses the process-wide ``redis`` client on ``app.state`` when
    available; falls back to a fresh ``uuid4`` (no persistence) if
    Redis is unreachable.
    """
    redis = getattr(app_state, "redis", None)
    if redis is None:
        return str(uuid.uuid4())

    key = _channel_key(user_id)
    try:
        existing = await redis.get(key)
    except Exception:
        logger.debug("Redis GET failed for async channel key %s", key, exc_info=True)
        return str(uuid.uuid4())

    if existing:
        if isinstance(existing, bytes):
            return existing.decode("utf-8", errors="replace")
        return str(existing)

    channel_id = str(uuid.uuid4())
    try:
        await redis.set(key, channel_id, ex=_CHANNEL_TTL_SECONDS)
    except Exception:
        logger.debug("Redis SET failed for async channel key %s", key, exc_info=True)
    return channel_id


async def _invalidate_channel_id(app_state: Any, user_id: int | str) -> str:
    """Mint a fresh channel id and persist it, replacing any previous entry.

    Called when the cookie's ``sub`` claim does not match the current
    user (mirrors the original's "user_id != session['async_user_id']"
    branch).
    """
    redis = getattr(app_state, "redis", None)
    channel_id = str(uuid.uuid4())
    if redis is None:
        return channel_id
    try:
        await redis.set(_channel_key(user_id), channel_id, ex=_CHANNEL_TTL_SECONDS)
    except Exception:
        logger.debug(
            "Redis SET failed when rotating async channel for user %s",
            user_id,
            exc_info=True,
        )
    return channel_id


class AsyncTokenMiddleware(ASGIMiddleware):
    """Mint / refresh the ``async-token`` JWT cookie on each authenticated
    HTTP response.

    Mirrors ``AsyncQueryManager.register_request_handlers`` from the
    original Flask Superset.
    """

    async def handle(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        next_app: ASGIApp,
    ) -> None:
        if scope["type"] != "http":
            await next_app(scope, receive, send)
            return

        # Defer all JWT decisions until we know the response is going
        # out.  We can't pull the user / settings off ``scope`` until
        # the auth middleware has populated them, which has happened by
        # the time ``http.response.start`` is sent.
        async def send_with_async_token(message: Message) -> None:
            if message["type"] != "http.response.start":
                await send(message)
                return

            try:
                cookie_header = await self._build_cookie_header(scope)
            except Exception:  # noqa: BLE001
                logger.debug("AsyncTokenMiddleware failed", exc_info=True)
                cookie_header = None

            if cookie_header is not None:
                headers: list[tuple[bytes, bytes]] = list(message.get("headers", []))
                headers.append((b"set-cookie", cookie_header))
                message = {**message, "headers": headers}

            await send(message)

        await next_app(scope, receive, send_with_async_token)

    @staticmethod
    async def _build_cookie_header(scope: Scope) -> bytes | None:
        """Decide whether to emit a ``Set-Cookie`` header for this response.

        Returns the header value (bytes) or ``None`` to skip.
        """
        # Resolve the authenticated user.  Litestar's auth middleware
        # populates ``scope["user"]`` with the authenticated entity
        # (CachedUser / GuestUser / UnauthenticatedUser).  Anonymous
        # callers do not get an async-token cookie — the original only
        # minted it when ``get_user_id()`` returned a value or the
        # session claimed an ``async_user_id``.
        user = scope.get("user")
        is_authed = bool(getattr(user, "is_authenticated", False))
        user_id: int | None = getattr(user, "id", None) if is_authed else None
        if not is_authed or not user_id:
            # Mirror the original "anonymous channel" behaviour: the
            # original still set a cookie with ``sub=None`` when no
            # user was logged in, so the polling endpoint could read
            # events for the duration of the anonymous session.
            user_id = 0

        app = scope.get("app")
        if app is None:
            return None
        app_state = getattr(app, "state", None)
        if app_state is None:
            return None
        settings = getattr(app_state, "settings", None)
        if settings is None:
            return None

        cookie_name = getattr(
            settings, "global_async_queries_jwt_cookie_name", "async-token"
        )
        secure = bool(
            getattr(settings, "global_async_queries_jwt_cookie_secure", False)
        )
        samesite = getattr(
            settings, "global_async_queries_jwt_cookie_samesite", None
        )
        domain = getattr(
            settings, "global_async_queries_jwt_cookie_domain", None
        )
        secret_key = _resolve_secret_key(settings)
        if not secret_key:
            return None

        # Find the existing async-token cookie, if any.
        raw_cookie: bytes | None = None
        for name, value in scope.get("headers", []):
            if name == b"cookie":
                raw_cookie = value
                break
        existing = _decode_existing_cookie(raw_cookie, secret_key)

        # Decide whether we need to mint a fresh token.
        needs_refresh = existing is None
        if existing is not None:
            sub_claim = existing.get("sub")
            # The original cast user_id via ``str(user_id) if user_id else None``
            expected_sub = str(user_id) if user_id else None
            if sub_claim != expected_sub:
                needs_refresh = True

        if not needs_refresh:
            return None

        # Resolve / rotate the per-user channel id.
        if existing is None:
            channel_id = await _resolve_channel_id(app_state, user_id)
        else:
            channel_id = await _invalidate_channel_id(app_state, user_id)

        sub = str(user_id) if user_id else None
        token = pyjwt.encode(
            {"channel": channel_id, "sub": sub},
            secret_key,
            algorithm="HS256",
        )
        if isinstance(token, bytes):
            token = token.decode("ascii")

        return _build_set_cookie(
            cookie_name,
            token,
            secure=secure,
            samesite=samesite,
            domain=domain,
        )


# ---------------------------------------------------------------------------
# Helpers exposed for the polling endpoint
# ---------------------------------------------------------------------------


def parse_channel_id_from_cookie(
    raw_cookie_header: str | None,
    secret_key: str,
    cookie_name: str = "async-token",
) -> str | None:
    """Decode the ``async-token`` cookie and return its ``channel`` claim.

    Helper for the polling endpoint (``async_event.get_events``) so the
    poll reads from the same channel that the chart-data submission
    wrote to.  Returns ``None`` if the cookie is missing or invalid.
    """
    if not raw_cookie_header:
        return None
    cookie: SimpleCookie = SimpleCookie()
    try:
        cookie.load(raw_cookie_header)
    except Exception:  # noqa: BLE001
        return None
    morsel = cookie.get(cookie_name)
    if morsel is None:
        return None
    try:
        # verify_sub=False so an anonymous cookie (``sub=None``) still yields its
        # channel claim under pyjwt >= 2.10 (matches the original lax decode).
        payload = pyjwt.decode(
            morsel.value,
            secret_key,
            algorithms=["HS256"],
            options={"verify_sub": False},
        )
    except pyjwt.PyJWTError:
        return None
    channel = payload.get("channel")
    return str(channel) if channel else None


def resolve_async_channel_id_from_request(
    request: Any,
    settings: Any,
) -> str | None:
    """Resolve the async-query channel id from the request's async-token cookie.

    Single shared helper used by both the chart-data submit path
    (:func:`superset.controllers.chart.ChartController.get_chart_data` and
    ``data``) and the polling endpoint
    (:func:`superset.controllers.async_event.AsyncEventController.get_events`).

    Returns the ``channel`` claim from the JWT, or ``None`` if the cookie is
    missing or invalid.  Callers decide what to do with ``None`` — the writer
    raises HTTP 401; the reader falls back to ``user-{id}``.
    """
    try:
        if settings is None:
            return None
        secret = _resolve_secret_key(settings)
        if not secret:
            return None
        cookie_name = getattr(
            settings, "global_async_queries_jwt_cookie_name", "async-token"
        )
        raw_cookie: str | None = None
        for header_name, header_value in request.scope.get("headers", []):
            if header_name == b"cookie":
                raw_cookie = header_value.decode("utf-8", errors="replace")
                break
        return parse_channel_id_from_cookie(raw_cookie, secret, cookie_name=cookie_name)
    except Exception:  # noqa: BLE001
        logger.debug("Failed to parse async-token cookie", exc_info=True)
        return None


__all__ = [
    "AsyncTokenMiddleware",
    "parse_channel_id_from_cookie",
    "resolve_async_channel_id_from_request",
]
