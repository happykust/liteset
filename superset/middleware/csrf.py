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
from typing import Any

from litestar.connection import ASGIConnection
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


def generate_csrf_token(secret: str) -> str:
    """Generate an HMAC-signed CSRF token (compatible with
    Flask-WTF's ``generate_csrf`` output format).
    """
    salt = os.urandom(8).hex()
    ts = str(int(time.time()))
    data = f"{salt}{ts}"
    sig = hmac.new(
        secret.encode(), data.encode(), hashlib.sha256,
    ).hexdigest()
    return f"{salt}.{ts}.{sig}"


def validate_csrf_token(
    token: str, secret: str, max_age: int = 604800,
) -> bool:
    """Validate an HMAC-signed CSRF token."""
    if not token or "." not in token:
        return False
    try:
        parts = token.split(".", 2)
        if len(parts) != 3:
            return False
        salt, ts_str, sig = parts
        # Check signature
        data = f"{salt}{ts_str}"
        expected = hmac.new(
            secret.encode(),
            data.encode(),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return False
        # Check expiry
        if max_age:
            ts = int(ts_str)
            if time.time() - ts > max_age:
                return False
        return True
    except (ValueError, TypeError):
        return False


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
    ) -> None:
        self.app = app
        self.secret = secret
        self.header_name = header_name.lower()
        self.max_age = max_age
        self.exclude_paths = set(exclude_paths or [])

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET").upper()
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
            self.header_name.encode(), b"",
        )
        token = token_bytes.decode(
            "utf-8", errors="ignore",
        )

        if not token or not validate_csrf_token(
            token, self.secret, self.max_age,
        ):
            # Return 403 with JSON error
            import json

            body = json.dumps({
                "errors": [{
                    "message": (
                        "CSRF token verification failed"
                    ),
                    "error_type": "CSRF_ERROR",
                    "level": "error",
                    "extra": {},
                }],
                "message": (
                    "CSRF token verification failed"
                ),
            }).encode()

            await send({
                "type": "http.response.start",
                "status": 403,
                "headers": [
                    (b"content-type", b"application/json"),
                    (
                        b"content-length",
                        str(len(body)).encode(),
                    ),
                ],
            })
            await send({
                "type": "http.response.body",
                "body": body,
            })
            return

        await self.app(scope, receive, send)


def create_csrf_middleware(
    secret: str,
    *,
    header_name: str = "X-CSRFToken",
    max_age: int = 604800,
    exclude_paths: list[str] | None = None,
) -> DefineMiddleware:
    """Create CSRF middleware definition."""
    return DefineMiddleware(
        CSRFMiddleware,
        secret=secret,
        header_name=header_name,
        max_age=max_age,
        exclude_paths=exclude_paths,
    )
