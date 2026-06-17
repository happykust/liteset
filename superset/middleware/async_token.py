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

Whenever an authenticated user makes a request, this middleware:

* Mints a per-browser ``channel_id`` when needed.  In the original
  code the channel id was stashed on the legacy session (a
  per-browser signed cookie); here the ``async-token`` cookie itself
  (already ``HttpOnly``) persists the channel across requests for that
  browser — exactly as the legacy session did.  A fresh ``uuid4`` is
  minted on first contact and reused via the cookie on subsequent
  requests; no Redis look-up is performed during minting.
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


def _resolve_secret_key(settings: Any) -> str:
    secret = getattr(
        settings,
        "global_async_queries_jwt_secret",
        None,
    )
    if secret is None:
        secret = getattr(settings, "secret_key", "")
    if hasattr(secret, "get_secret_value"):
        secret = secret.get_secret_value()  # type: ignore[union-attr]
    return str(secret)


def _decode_existing_cookie(
    raw: bytes | None,
    secret_key: str,
    cookie_name: str = "async-token",
) -> dict[str, Any] | None:
    """Return decoded JWT payload or None if missing/malformed/wrong key."""
    if not raw:
        return None
    cookie_header = raw.decode("utf-8", errors="replace")
    cookie: SimpleCookie = SimpleCookie()
    try:
        cookie.load(cookie_header)
    except Exception:  # noqa: BLE001
        return None
    morsel = cookie.get(cookie_name)
    if morsel is None:
        return None
    try:
        # verify_sub=False: the anonymous cookie carries ``sub=None``;
        # pyjwt >= 2.10 otherwise rejects a null sub, which would force
        # a needless re-mint on every anonymous response.
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
    """Compose a Set-Cookie header value; HttpOnly always set."""
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


class AsyncTokenMiddleware(ASGIMiddleware):
    """Mint / refresh the ``async-token`` JWT cookie on each authenticated
    HTTP response.
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
    def _resolve_cookie_config(
        scope: Scope,
    ) -> tuple[str, bool, str | None, str | None, str] | None:
        """Return (cookie_name, secure, samesite, domain, secret_key) or None."""
        app = scope.get("app")
        if app is None:
            return None
        app_state = getattr(app, "state", None)
        if app_state is None:
            return None
        settings = getattr(app_state, "settings", None)
        if settings is None:
            return None

        secret_key = _resolve_secret_key(settings)
        if not secret_key:
            return None

        cookie_name = getattr(
            settings, "global_async_queries_jwt_cookie_name", "async-token"
        )
        secure = bool(
            getattr(settings, "global_async_queries_jwt_cookie_secure", False)
        )
        samesite = getattr(settings, "global_async_queries_jwt_cookie_samesite", None)
        domain = getattr(settings, "global_async_queries_jwt_cookie_domain", None)
        return cookie_name, secure, samesite, domain, secret_key

    @staticmethod
    def _needs_token_refresh(
        existing: dict[str, Any] | None,
        user_id: int,
    ) -> bool:
        """Return True when the cookie is absent or its sub claim no
        longer matches the current user."""
        if existing is None:
            return True
        sub_claim = existing.get("sub")
        # The original cast user_id via ``str(user_id) if user_id else None``
        expected_sub = str(user_id) if user_id else None
        return sub_claim != expected_sub

    @staticmethod
    async def _build_cookie_header(scope: Scope) -> bytes | None:
        """Return the Set-Cookie header bytes, or None to skip."""
        user = scope.get("user")
        is_authed = bool(getattr(user, "is_authenticated", False))
        user_id: int = getattr(user, "id", None) if is_authed else None  # type: ignore[assignment]
        if not is_authed or not user_id:
            # Anonymous channel: set the cookie with ``sub=None`` when no
            # user is logged in so the polling endpoint can read events
            # for the duration of the anonymous session.
            user_id = 0

        cookie_config = AsyncTokenMiddleware._resolve_cookie_config(scope)
        if cookie_config is None:
            return None
        cookie_name, secure, samesite, domain, secret_key = cookie_config

        raw_cookie: bytes | None = None
        for name, value in scope.get("headers", []):
            if name == b"cookie":
                raw_cookie = value
                break
        existing = _decode_existing_cookie(
            raw_cookie, secret_key, cookie_name=cookie_name
        )

        if not AsyncTokenMiddleware._needs_token_refresh(existing, user_id):
            return None

        channel_id = str(uuid.uuid4())

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


def parse_channel_id_from_cookie(
    raw_cookie_header: str | None,
    secret_key: str,
    cookie_name: str = "async-token",
) -> str | None:
    """Return the ``channel`` claim from the async-token JWT, or None."""
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

    Shared by the chart-data submit path and the polling endpoint.
    Callers decide what to do with None — the writer raises HTTP 401;
    the reader falls back to ``user-{id}``.
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
