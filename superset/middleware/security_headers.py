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
"""Security headers middleware for Superset.

Adds standard security headers to all HTTP responses, replacing
flask-talisman functionality:
- X-Content-Type-Options
- X-Frame-Options
- X-XSS-Protection
- Strict-Transport-Security (HTTPS only)
- Content-Security-Policy (configurable)
- Referrer-Policy
- Permissions-Policy
"""

from __future__ import annotations

from litestar.middleware.base import ASGIMiddleware
from litestar.types import ASGIApp, Message, Receive, Scope, Send

_DEFAULT_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "font-src 'self' data:; "
    "connect-src 'self' ws: wss:; "
    "worker-src 'self' blob:"
)


class SecurityHeadersMiddleware(ASGIMiddleware):
    """Inject security headers into every HTTP response.

    The Content-Security-Policy value is configurable via
    ``settings.content_security_policy`` on the application state.
    Falls back to ``default-src 'self'`` when not configured.
    """

    async def handle(
        self, scope: Scope, receive: Receive, send: Send, next_app: ASGIApp
    ) -> None:
        if scope["type"] != "http":
            await next_app(scope, receive, send)
            return

        # Resolve CSP from app settings (configurable)
        app = scope.get("app")
        csp = _DEFAULT_CSP
        if app is not None:
            settings = getattr(getattr(app, "state", None), "settings", None)
            if settings is not None:
                csp = getattr(settings, "content_security_policy", _DEFAULT_CSP)

        # Determine if the request arrived over HTTPS
        is_https = scope.get("scheme") == "https"

        # Build the static header list once per request
        headers: list[tuple[bytes, bytes]] = [
            (b"x-content-type-options", b"nosniff"),
            (b"x-frame-options", b"SAMEORIGIN"),
            (b"x-xss-protection", b"1; mode=block"),
            (b"referrer-policy", b"strict-origin-when-cross-origin"),
            (b"permissions-policy", b"geolocation=(), camera=(), microphone=()"),
            (b"content-security-policy", csp.encode("utf-8")),
        ]
        if is_https:
            headers.append(
                (
                    b"strict-transport-security",
                    b"max-age=31536000; includeSubDomains",
                )
            )

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                existing: list[tuple[bytes, bytes]] = list(
                    message.get("headers", [])
                )
                existing.extend(headers)
                message = {**message, "headers": existing}
            await send(message)

        await next_app(scope, receive, send_with_headers)
