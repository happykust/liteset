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
"""ASGI port of Apache Superset's ``apply_http_headers`` after-request hook.

The original Flask ``register_request_handlers`` registered a
``@app.after_request`` callback that merged three settings dicts onto
every response:

* ``OVERRIDE_HTTP_HEADERS`` — wins over anything already on the response
  (deprecated in upstream, kept for back-compat).
* ``HTTP_HEADERS`` — also unconditionally overwrites.
* ``DEFAULT_HTTP_HEADERS`` — only applied if the corresponding header
  isn't already set by a downstream handler.

This middleware reproduces that exact precedence on the ASGI side by
intercepting the ``http.response.start`` message.  Header values are
encoded as UTF-8 ``bytes`` per the Litestar / ASGI contract.
"""

from __future__ import annotations

from typing import Any

from litestar.middleware.base import ASGIMiddleware
from litestar.types import ASGIApp, Message, Receive, Scope, Send


def _to_bytes(value: Any) -> bytes:
    """Encode a header value to bytes, matching werkzeug's lenient behaviour."""
    if isinstance(value, bytes):
        return value
    return str(value).encode("latin-1", errors="replace")


def _normalize(name: Any) -> bytes:
    """Header names are case-insensitive — store/compare as lowercase bytes."""
    if isinstance(name, bytes):
        return name.lower()
    return str(name).lower().encode("latin-1", errors="replace")


class HTTPHeadersMiddleware(ASGIMiddleware):
    """Apply Superset's ``HTTP_HEADERS`` / ``DEFAULT_HTTP_HEADERS`` settings.

    Reads the three header dicts from ``app.state.settings`` so user
    overrides pushed via ``superset_config.py`` reach every response.
    """

    async def handle(
        self, scope: Scope, receive: Receive, send: Send, next_app: ASGIApp
    ) -> None:
        if scope["type"] != "http":
            await next_app(scope, receive, send)
            return

        settings = None
        app = scope.get("app")
        if app is not None:
            settings = getattr(getattr(app, "state", None), "settings", None)

        override: dict[str, Any] = (
            getattr(settings, "override_http_headers", {}) or {}
        )
        merged: dict[str, Any] = getattr(settings, "http_headers", {}) or {}
        defaults: dict[str, Any] = (
            getattr(settings, "default_http_headers", {}) or {}
        )

        if not override and not merged and not defaults:
            await next_app(scope, receive, send)
            return

        # Pre-encode everything once per request — header dicts are
        # tiny and immutable for the lifetime of the app, so the cost
        # is negligible.
        unconditional: list[tuple[bytes, bytes]] = []
        for source in (override, merged):
            for k, v in source.items():
                unconditional.append((_normalize(k), _to_bytes(v)))

        default_pairs: list[tuple[bytes, bytes]] = [
            (_normalize(k), _to_bytes(v)) for k, v in defaults.items()
        ]

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                existing: list[tuple[bytes, bytes]] = list(
                    message.get("headers", [])
                )
                # OVERRIDE_HTTP_HEADERS / HTTP_HEADERS — replace any
                # existing headers with the same (case-insensitive) name.
                for name, value in unconditional:
                    existing = [
                        (k, v) for k, v in existing if k.lower() != name
                    ]
                    existing.append((name, value))
                # DEFAULT_HTTP_HEADERS — only added when missing.
                existing_names = {k.lower() for k, _ in existing}
                for name, value in default_pairs:
                    if name not in existing_names:
                        existing.append((name, value))
                message = {**message, "headers": existing}
            await send(message)

        await next_app(scope, receive, send_with_headers)
