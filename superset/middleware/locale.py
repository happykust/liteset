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
"""Locale middleware.

Resolves the active request locale the same way the original upstream
locale selector did: a chosen-language cookie (the ASGI equivalent of the
``session["locale"]`` set by ``/lang/<locale>``) takes precedence, then the
``Accept-Language`` header's best match, then the default.

IMPORTANT — both the cookie and the ``Accept-Language`` match are constrained
to the **configured** ``LANGUAGES`` (``settings.languages``), exactly like
upstream's ``request.accept_languages.best_match(appbuilder.bm.languages) or
BABEL_DEFAULT_LOCALE``.  This is what keeps the backend English when only
English is enabled: without it a French/Chinese browser would get a
half-translated UI (translated backend strings, English React frontend) even
though the deployment never enabled those languages.  With the default
English-only ``LANGUAGES`` the resolved locale is always ``en``
(``LANGUAGES = {}`` makes ``best_match`` return ``None`` ->
``BABEL_DEFAULT_LOCALE``).
"""

from __future__ import annotations

from litestar.middleware.base import ASGIMiddleware
from litestar.types import ASGIApp, Receive, Scope, Send

from superset.i18n import _current_locale, set_locale


class LocaleMiddleware(ASGIMiddleware):
    """Resolve locale per-request: cookie > Accept-Language > default,
    bounded by the configured ``LANGUAGES``."""

    LANGUAGE_COOKIE_NAME = "language"

    async def handle(
        self, scope: Scope, receive: Receive, send: Send, next_app: ASGIApp
    ) -> None:
        token = None
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))

            allowed, default = _resolve_language_config(scope)

            locale = _extract_cookie_locale(
                headers.get(b"cookie", b""),
                self.LANGUAGE_COOKIE_NAME,
                allowed,
            )

            if locale is None:
                raw = headers.get(b"accept-language", b"")
                accept = raw.decode("utf-8", errors="replace")
                locale = _best_match(accept, allowed)

            if locale is None:
                locale = default

            token = set_locale(locale)
        try:
            await next_app(scope, receive, send)
        finally:
            if token is not None:
                _current_locale.reset(token)


def _resolve_language_config(scope: Scope) -> tuple[set[str], str]:
    """Return (allowed_languages, default_locale) from app settings.

    Falls back to (set(), "en") when settings are unavailable, which
    resolves every request to the default — never a half-translated UI.
    """
    default = "en"
    allowed: set[str] = set()
    app = scope.get("app")
    settings = getattr(getattr(app, "state", None), "settings", None)
    if settings is not None:
        default = str(getattr(settings, "babel_default_locale", "en") or "en")
        languages = getattr(settings, "languages", {}) or {}
        try:
            allowed = {str(k) for k in languages}
        except TypeError:
            allowed = set()
    return allowed, default


def _match_allowed(value: str, allowed: set[str]) -> str | None:
    """Match a language tag against the allowed set, trying normalized
    form then primary subtag."""
    if not value or not allowed:
        return None
    allowed_lower = {a.lower(): a for a in allowed}
    norm = value.strip().replace("-", "_")
    for candidate in (norm, norm.split("_", 1)[0]):
        hit = allowed_lower.get(candidate.lower())
        if hit is not None:
            return hit
    return None


def _extract_cookie_locale(
    raw_cookie: bytes, cookie_name: str, allowed: set[str]
) -> str | None:
    if not raw_cookie:
        return None
    cookie_str = raw_cookie.decode("utf-8", errors="replace")
    for pair in cookie_str.split(";"):
        pair = pair.strip()
        if "=" not in pair:
            continue
        name, _, value = pair.partition("=")
        if name.strip() == cookie_name:
            return _match_allowed(value.strip(), allowed)
    return None


def _best_match(header: str, allowed: set[str]) -> str | None:
    """Return the highest-quality allowed locale from an Accept-Language header."""
    if not header or not allowed:
        return None
    parsed: list[tuple[float, int, str]] = []
    for index, part in enumerate(header.split(",")):
        part = part.strip()
        if not part:
            continue
        tag, _, q = part.partition(";q=")
        try:
            quality = float(q) if q else 1.0
        except ValueError:
            quality = 1.0
        parsed.append((quality, index, tag.strip()))
    parsed.sort(key=lambda item: (-item[0], item[1]))
    for _quality, _index, tag in parsed:
        hit = _match_allowed(tag, allowed)
        if hit is not None:
            return hit
    return None
