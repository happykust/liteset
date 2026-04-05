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
"""Locale middleware with user pref > cookie > Accept-Language > default."""

from __future__ import annotations

from litestar.middleware.base import ASGIMiddleware
from litestar.types import ASGIApp, Receive, Scope, Send

from superset.i18n import _current_locale, set_locale

# Supported locale codes; cookie values not in this set are ignored.
SUPPORTED_LOCALES: frozenset[str] = frozenset(
    {
        "ar",
        "cs",
        "de",
        "en",
        "es",
        "fr",
        "he",
        "it",
        "ja",
        "ko",
        "nl",
        "pl",
        "pt",
        "ru",
        "sk",
        "sl",
        "uk",
        "zh",
    }
)


class LocaleMiddleware(ASGIMiddleware):
    """Resolve locale per-request: cookie > Accept-Language > default."""

    LANGUAGE_COOKIE_NAME = "language"

    async def handle(
        self, scope: Scope, receive: Receive, send: Send, next_app: ASGIApp
    ) -> None:
        token = None
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))

            # 1. Check ``language`` cookie
            locale = _extract_cookie_locale(
                headers.get(b"cookie", b""),
                self.LANGUAGE_COOKIE_NAME,
            )

            # 2. Fall back to Accept-Language header
            if locale is None:
                raw = headers.get(b"accept-language", b"en")
                accept = raw.decode("utf-8", errors="replace")
                locale = _parse_accept_language(accept)

            token = set_locale(locale)
        try:
            await next_app(scope, receive, send)
        finally:
            if token is not None:
                _current_locale.reset(token)


def _extract_cookie_locale(raw_cookie: bytes, cookie_name: str) -> str | None:
    """Extract locale from a specific cookie in the raw Cookie header.

    Returns ``None`` if the cookie is absent or empty.
    """
    if not raw_cookie:
        return None
    cookie_str = raw_cookie.decode("utf-8", errors="replace")
    for pair in cookie_str.split(";"):
        pair = pair.strip()
        if "=" not in pair:
            continue
        name, _, value = pair.partition("=")
        if name.strip() == cookie_name:
            lang = value.strip()
            if lang:
                normalized = lang.split("-")[0].lower()
                if normalized in SUPPORTED_LOCALES:
                    return normalized
                return None
    return None


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
