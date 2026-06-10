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
"""Session-based CSRF protection compatible with Flask-WTF.

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

from litestar.middleware.base import (
    DefineMiddleware,
    MiddlewareProtocol,
)
from litestar.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

# Module-level token store keyed by a session identifier.
# In production Redis should be used; this dict suffices
# for single-process dev servers.
_csrf_tokens: dict[str, str] = {}


def _hash_session_id(session_id: str) -> str:
    """Return a truncated SHA-256 hex digest of *session_id*.

    The hash is included in the token so that a token generated
    for one session cannot be reused by a different session.
    An empty string yields a deterministic hash for the
    unauthenticated case (e.g. login page).
    """
    return hashlib.sha256(session_id.encode()).hexdigest()[:16]


def generate_csrf_token(
    secret: str,
    session_id: str = "",
) -> str:
    """Generate an HMAC-signed CSRF token bound to a session.

    The token format is ``salt.timestamp.session_hash.signature``
    where *session_hash* is a truncated SHA-256 of the session
    cookie value.  Including the session hash in the HMAC payload
    means tokens cannot be replayed across sessions.

    When *session_id* is empty (e.g. the login page), the token
    is still valid but bound to the empty-session hash.
    """
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
    """Validate an HMAC-signed, session-bound CSRF token.

    The token must have been generated with the same *session_id*
    (or the same empty string for unauthenticated pages).
    """
    if not token or "." not in token:
        return False
    try:
        parts = token.split(".")
        # New 4-part format: salt.ts.session_hash.sig
        if len(parts) == 4:
            salt, ts_str, token_sess_hash, sig = parts
            # Verify the session hash matches the current session
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
        # Legacy 3-part format: salt.ts.sig (transition period)
        elif len(parts) == 3:
            salt, ts_str, sig = parts
            data = f"{salt}{ts_str}"
            expected_sig = hmac.new(
                secret.encode(),
                data.encode(),
                hashlib.sha256,
            ).hexdigest()
            if not hmac.compare_digest(sig, expected_sig):
                return False
            logger.warning(
                "Legacy CSRF token without session binding accepted; "
                "this will be rejected in a future release."
            )
        else:
            return False
        # Check expiry
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
    """Extract a named cookie value from raw ASGI headers.

    Returns an empty string when the cookie is not present.
    """
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


class CSRFMiddleware(MiddlewareProtocol):
    """CSRF middleware compatible with the original
    Flask-WTF flow used by Apache Superset.

    - GET /api/v1/security/csrf_token/ generates and
      returns the token
    - Frontend sends token in X-CSRFToken header
    - This middleware validates the header on
      POST/PUT/DELETE/PATCH
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
    ) -> None:
        self.app = app
        self.secret = secret
        self.header_name = header_name.lower()
        self.max_age = max_age
        self.exclude_paths = set(exclude_paths or [])
        self.session_cookie_name = session_cookie_name

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

        # Check excluded paths
        for exc in self.exclude_paths:
            if path.startswith(exc):
                await self.app(scope, receive, send)
                return

        # Check opt exclude_from_csrf on route
        route = scope.get("route_handler")
        if route is not None:
            opt = getattr(route, "opt", {}) or {}
            if opt.get("exclude_from_csrf"):
                await self.app(scope, receive, send)
                return

        # Extract token from header
        headers = dict(scope.get("headers", []))
        token_bytes = headers.get(
            self.header_name.encode(),
            b"",
        )
        token = token_bytes.decode(
            "utf-8",
            errors="ignore",
        )

        # Extract session cookie for session-bound validation
        session_id = _extract_cookie(headers, self.session_cookie_name)

        if not token or not validate_csrf_token(
            token,
            self.secret,
            self.max_age,
            session_id=session_id,
        ):
            import json as _json

            logger.warning("Refresh CSRF token error")

            # Mirror the original Flask handler:
            # - JSON requests → 400 JSON error (CSRFError inherits BadRequest)
            # - non-JSON requests → 302 redirect to login
            content_type_header = headers.get(b"content-type", b"").decode(
                "utf-8", errors="ignore"
            )
            mt = content_type_header.split(";")[0].strip().lower()
            is_json = mt == "application/json" or (
                mt.startswith("application/") and mt.endswith("+json")
            )

            if not is_json:
                # Non-JSON (browser form) → redirect to login
                # Mirrors original redirect_to_login()
                qs = scope.get("query_string", b"")
                path = scope.get("path", "/")
                next_url = path + (
                    ("?" + qs.decode("utf-8", errors="ignore")) if qs else ""
                )
                import urllib.parse as _parse

                redirect_target = "/login?next=" + _parse.quote(next_url, safe="")
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
                await send(
                    {"type": "http.response.body", "body": b""}  # type: ignore[arg-type]
                )
                return

            # JSON request → 400 with GENERIC_BACKEND_ERROR (mirrors show_http_exception
            # which uses ex.code=400 since CSRFError inherits werkzeug BadRequest)
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
                {  # type: ignore[arg-type]
                    "type": "http.response.body",
                    "body": body,
                }
            )
            return

        await self.app(scope, receive, send)


def create_csrf_middleware(
    secret: str,
    *,
    header_name: str = "X-CSRFToken",
    max_age: int = 604800,
    exclude_paths: list[str] | None = None,
    session_cookie_name: str = "session",
) -> DefineMiddleware:
    """Create CSRF middleware definition."""
    return DefineMiddleware(
        CSRFMiddleware,
        secret=secret,
        header_name=header_name,
        max_age=max_age,
        exclude_paths=exclude_paths,
        session_cookie_name=session_cookie_name,
    )
