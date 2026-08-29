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
"""Session-based CSRF protection for the Superset CSRF token flow.

Token is generated via HMAC(secret, random_bytes) and stored
in the user's session cookie JWT claims.  The frontend fetches
it via ``GET /api/v1/security/csrf_token/`` and sends it back
in the ``X-CSRFToken`` header on every state-changing request.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
import urllib.parse
from typing import cast

from litestar.middleware.base import (
    DefineMiddleware,
    MiddlewareProtocol,
)
from litestar.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

# WARNING: single-process dev store only; use Redis in production.
_csrf_tokens: dict[str, str] = {}


def _hash_session_id(session_id: str) -> str:
    """Return a truncated SHA-256 hex digest of *session_id* to bind
    tokens to sessions."""
    return hashlib.sha256(session_id.encode()).hexdigest()[:16]


def generate_csrf_token(
    secret: str,
    session_id: str = "",
) -> str:
    """Generate an HMAC-signed CSRF token (``salt.timestamp.session_hash.sig``).

    The session hash binds the token to a specific session cookie so it
    cannot be replayed across sessions.

    Refuses to mint a token when *session_id* is falsy: ``sha256("")`` is a
    fixed, publicly computable constant, so a token "bound" to an empty
    session would authenticate any caller who also presents no session --
    which is exactly what a cross-site request does.  Returns ``""`` in that
    case; callers must treat an empty result as "no token available", not
    fall back to an unbound one.
    """
    if not session_id:
        logger.debug("Refusing to mint a CSRF token with an empty session binding")
        return ""
    salt = os.urandom(8).hex()
    ts = str(int(time.time()))
    sess_hash = _hash_session_id(session_id)
    payload = f"{salt}.{ts}.{sess_hash}"
    sig = hmac.new(
        secret.encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"{salt}.{ts}.{sess_hash}.{sig}"


def validate_csrf_token(
    token: str,
    secret: str,
    max_age: int = 604800,
    session_id: str = "",
) -> bool:
    """Validate an HMAC-signed CSRF token, rejecting cross-session
    replays and expired tokens.

    An empty *session_id* is always rejected outright, even when the token
    carries a hash that happens to match ``sha256("")``: that hash is a
    fixed constant computable without ever seeing a real session cookie, so
    honouring it as a legitimate binding would let a token minted (or
    fetched) with no session at all validate on behalf of any other caller
    who also presents no session -- the login-CSRF gap this binding exists
    to close.
    """
    if not token or "." not in token:
        return False
    if not session_id:
        return False
    try:
        parts = token.split(".")
        if len(parts) != 4:
            return False
        salt, ts_str, token_sess_hash, sig = parts
        expected_sess_hash = _hash_session_id(session_id)
        if not hmac.compare_digest(token_sess_hash, expected_sess_hash):
            return False
        payload = f"{salt}.{ts_str}.{token_sess_hash}"
        expected_sig = hmac.new(
            secret.encode(),
            payload.encode(),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            return False
        if max_age:
            ts = int(ts_str)
            if time.time() - ts > max_age:
                return False
        return True
    except (ValueError, TypeError):
        return False


def _extract_cookie(
    headers: dict[bytes, bytes],
    cookie_name: str,
) -> str:
    """Extract a named cookie value from raw ASGI headers, or empty string."""
    raw_cookie = headers.get(b"cookie", b"")
    if not raw_cookie:
        return ""
    cookie_str = raw_cookie.decode("utf-8", errors="ignore")
    for pair in cookie_str.split(";"):
        pair = pair.strip()
        if "=" not in pair:
            continue
        name, _, value = pair.partition("=")
        if name.strip() == cookie_name:
            return value.strip()
    return ""


def _extract_host(headers: dict[bytes, bytes]) -> str:
    """Return the (ProxyFix-corrected) ``Host`` header, or empty string."""
    return headers.get(b"host", b"").decode("latin-1", errors="ignore")


def _same_origin_https(candidate: str, expected_host: str) -> bool:
    """Return whether *candidate* (a ``Referer`` or ``Origin`` value) is an
    ``https://`` URL on *expected_host*.

    Mirrors ``flask_wtf.csrf.same_origin`` -- upstream builds
    ``good_referrer = f"https://{request.host}/"`` and compares scheme +
    netloc.  Here the comparison is against a corrected ``Host`` header
    (post-:class:`~superset.middleware.proxy_fix.ProxyFixMiddleware`) and
    accepts either header since modern browsers may omit ``Referer`` (a
    strict ``Referrer-Policy``) while always sending ``Origin`` on
    state-changing fetch/XHR requests.
    """
    if not candidate or not expected_host:
        return False
    try:
        parsed = urllib.parse.urlparse(candidate)
    except ValueError:
        return False
    if parsed.scheme.lower() != "https" or not parsed.netloc:
        return False
    return parsed.netloc.lower() == expected_host.lower()


class CSRFMiddleware(MiddlewareProtocol):
    """CSRF middleware compatible with the original
    CSRF flow used by Apache Superset.

    - GET /api/v1/security/csrf_token/ generates and
      returns the token
    - Frontend sends token in X-CSRFToken header
    - This middleware validates the header on
      POST/PUT/DELETE/PATCH
    - When ``ssl_strict`` and the (corrected) request scheme is
      ``https``, also requires a same-origin ``Referer``/``Origin``,
      mirroring Flask-WTF's ``WTF_CSRF_SSL_STRICT`` (default ``True``)
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        secret: str,
        header_name: str = "x-csrftoken",
        max_age: int = 604800,
        exclude_paths: list[str] | None = None,
        session_cookie_name: str = "session",
        ssl_strict: bool = True,
    ) -> None:
        self.app = app
        self.secret = secret
        self.header_name = header_name.lower()
        self.max_age = max_age
        self.exclude_paths = set(exclude_paths or [])
        self.session_cookie_name = session_cookie_name
        self.ssl_strict = ssl_strict

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = str(scope.get("method", "GET")).upper()
        path = scope.get("path", "/")

        # Safe methods pass through
        if method in _SAFE_METHODS:
            await self.app(scope, receive, send)
            return

        for exc in self.exclude_paths:
            if path.startswith(exc):
                await self.app(scope, receive, send)
                return

        route = scope.get("route_handler")
        if route is not None:
            opt = getattr(route, "opt", {}) or {}
            if opt.get("exclude_from_csrf"):
                await self.app(scope, receive, send)
                return

        headers = dict(scope.get("headers", []))

        if self._ssl_strict_enabled(scope) and not self._referer_origin_ok(
            scope, headers
        ):
            logger.warning("CSRF rejected: missing or cross-origin Referer/Origin")
            await self._send_csrf_failure(scope, send, headers)
            return

        token_bytes = headers.get(
            self.header_name.encode(),
            b"",
        )
        token = token_bytes.decode(
            "utf-8",
            errors="ignore",
        )

        session_id = _extract_cookie(headers, self.session_cookie_name)

        if not token or not validate_csrf_token(
            token,
            self.secret,
            self.max_age,
            session_id=session_id,
        ):
            logger.warning("Refresh CSRF token error")
            await self._send_csrf_failure(scope, send, headers)
            return

        await self.app(scope, receive, send)

    def _ssl_strict_enabled(self, scope: Scope) -> bool:
        """Resolve the effective ``ssl_strict`` setting for this request.

        Reads ``settings.wtf_csrf_ssl_strict`` when available so deployments
        can override the constructor default without redeploying the
        middleware; otherwise falls back to ``self.ssl_strict``.
        """
        settings = getattr(getattr(scope.get("app"), "state", None), "settings", None)
        if settings is not None and hasattr(settings, "wtf_csrf_ssl_strict"):
            return bool(settings.wtf_csrf_ssl_strict)
        return self.ssl_strict

    @staticmethod
    def _referer_origin_ok(scope: Scope, headers: dict[bytes, bytes]) -> bool:
        """Return True when the request is not HTTPS, or carries a
        same-origin ``Referer``/``Origin`` for the (corrected) Host.

        Skipped entirely for non-HTTPS requests so local development (and
        any deployment that terminates TLS before Litestar without
        forwarding ``X-Forwarded-Proto``) is unaffected.
        """
        scheme = str(scope.get("scheme", "http")).lower()
        if scheme != "https":
            return True
        host = _extract_host(headers)
        candidate = headers.get(b"referer", b"").decode(
            "latin-1", errors="ignore"
        ) or headers.get(b"origin", b"").decode("latin-1", errors="ignore")
        return _same_origin_https(candidate, host)

    async def _send_csrf_failure(
        self,
        scope: Scope,
        send: Send,
        headers: dict[bytes, bytes],
    ) -> None:
        """Send the CSRF-rejection response: a JSON 400 for API/JSON
        clients, a 302 redirect to ``/login`` for browser navigations."""
        import json as _json

        content_type_header = headers.get(b"content-type", b"").decode(
            "utf-8", errors="ignore"
        )
        mt = content_type_header.split(";")[0].strip().lower()
        is_json = mt == "application/json" or (
            mt.startswith("application/") and mt.endswith("+json")
        )

        if not is_json:
            qs = scope.get("query_string", b"")
            path = scope.get("path", "/")
            next_url = path + (
                ("?" + qs.decode("utf-8", errors="ignore")) if qs else ""
            )

            redirect_target = "/login?next=" + urllib.parse.quote(next_url, safe="")
            location = redirect_target.encode("utf-8")
            await send(
                {
                    "type": "http.response.start",
                    "status": 302,
                    "headers": [
                        (b"location", location),
                        (b"content-length", b"0"),
                    ],
                }
            )
            await send(cast("Message", {"type": "http.response.body", "body": b""}))
            return

        body = _json.dumps(
            {
                "errors": [
                    {
                        "message": "CSRF token verification failed",
                        "error_type": "GENERIC_BACKEND_ERROR",
                        "level": "error",
                        "extra": {
                            "issue_codes": [
                                {
                                    "code": 1011,
                                    "message": "Issue 1011 - Superset encountered"
                                    " an unexpected error.",
                                }
                            ]
                        },
                    }
                ],
            }
        ).encode()

        await send(
            {
                "type": "http.response.start",
                "status": 400,
                "headers": [
                    (b"content-type", b"application/json"),
                    (
                        b"content-length",
                        str(len(body)).encode(),
                    ),
                ],
            }
        )
        await send(
            cast(
                "Message",
                {
                    "type": "http.response.body",
                    "body": body,
                },
            )
        )


def create_csrf_middleware(
    secret: str,
    *,
    header_name: str = "X-CSRFToken",
    max_age: int = 604800,
    exclude_paths: list[str] | None = None,
    session_cookie_name: str = "session",
    ssl_strict: bool = True,
) -> DefineMiddleware:
    return DefineMiddleware(
        CSRFMiddleware,
        secret=secret,
        header_name=header_name,
        max_age=max_age,
        exclude_paths=exclude_paths,
        session_cookie_name=session_cookie_name,
        ssl_strict=ssl_strict,
    )
