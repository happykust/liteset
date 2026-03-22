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
"""Accept-Language locale middleware."""

from __future__ import annotations

from litestar.middleware.base import ASGIMiddleware
from litestar.types import ASGIApp, Receive, Scope, Send

from liteset.i18n import _current_locale, set_locale


class LocaleMiddleware(ASGIMiddleware):
    """Parse Accept-Language header and set locale per-request."""

    async def handle(
        self, scope: Scope, receive: Receive, send: Send, next_app: ASGIApp
    ) -> None:
        token = None
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            raw = headers.get(b"accept-language", b"en")
            accept = raw.decode("utf-8", errors="replace")
            locale = _parse_accept_language(accept)
            token = set_locale(locale)
        try:
            await next_app(scope, receive, send)
        finally:
            if token is not None:
                _current_locale.reset(token)


def _parse_accept_language(header: str) -> str:
    """Extract primary language tag from Accept-Language header."""
    if not header:
        return "en"
    # Take the first language tag (highest priority)
    first = header.split(",")[0].strip()
    # Remove quality factor
    lang = first.split(";")[0].strip()
    # Normalize: "en-US" -> "en"
    return lang.split("-")[0].lower() or "en"
