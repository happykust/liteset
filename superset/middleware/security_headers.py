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

Adds standard security headers to all HTTP responses, replacing the
upstream talisman functionality:
- X-Content-Type-Options
- X-Frame-Options
- X-XSS-Protection (only when talisman_config["x_xss_protection"] is True)
- Strict-Transport-Security (HTTPS only)
- Content-Security-Policy (configurable via TALISMAN_CONFIG / TALISMAN_DEV_CONFIG,
  with per-request nonce injected into directives listed in
  content_security_policy_nonce_in)
- Referrer-Policy
- Permissions-Policy (from talisman_config["permissions_policy"], defaulting to
  the upstream talisman DEFAULT_PERMISSIONS_POLICY = {"browsing-topics": "()"})

Respects TALISMAN_ENABLED: when False no security headers are injected.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Mapping
from typing import Any, cast

from litestar.middleware.base import ASGIMiddleware
from litestar.types import ASGIApp, Message, Receive, Scope, Send

# Matches upstream talisman NONCE_LENGTH = 32; secrets.token_urlsafe(32) gives
# ~43 URL-safe chars vs. talisman's 32 alphanumeric — equivalent security level.
_NONCE_LENGTH = 32

# Upstream talisman DEFAULT_PERMISSIONS_POLICY (talisman.py:48-51).
_DEFAULT_PERMISSIONS_POLICY: dict[str, str] = {"browsing-topics": "()"}

# Scope-state key under which the per-request CSP nonce is stored so that
# the Jinja2 template callable (registered in app.py) can retrieve it.
CSP_NONCE_SCOPE_KEY = "csp_nonce"

logger = logging.getLogger(__name__)


def _build_csp_string(
    csp: dict[str, str | list[str]] | str | None,
    nonce_in: list[str] | None = None,
    nonce: str | None = None,
) -> str | None:
    """Serialise a Talisman-style CSP to a header string, injecting
    nonce into listed directives."""
    if not csp:
        return None
    if isinstance(csp, str):
        # Upstream talisman _parse_policy() parses a string CSP into an OrderedDict
        # before injecting the nonce (talisman.py:307-327) — it does NOT
        # pass through unchanged.
        if not (nonce_in and nonce):
            return csp
        from collections import OrderedDict

        policy_dict: OrderedDict[str, str] = OrderedDict()
        for raw_part in csp.split(";"):
            tokens = raw_part.strip().split(" ")
            policy_dict[tokens[0]] = " ".join(tokens[1:])
        str_nonce_token = f"'nonce-{nonce}'"
        nonce_directives_set: set[str] = set(nonce_in)
        result_parts: list[str] = []
        for directive, content in policy_dict.items():
            result_part = f"{directive} {content}" if content else directive
            if directive in nonce_directives_set:
                result_part += f" {str_nonce_token}"
            result_parts.append(result_part)
        return "; ".join(result_parts)
    nonce_directives: set[str] = set(nonce_in or [])
    nonce_token = f"'nonce-{nonce}'" if nonce else None
    parts: list[str] = []
    for directive, sources in csp.items():
        if isinstance(sources, list):
            src_list = list(sources)
        else:
            src_list = [sources] if sources else []
        if nonce_token and directive in nonce_directives:
            src_list = src_list + [nonce_token]
        parts.append(f"{directive} {' '.join(src_list)}" if src_list else directive)
    return "; ".join(parts)


def _build_force_https_location(scope: Mapping[str, Any]) -> bytes | None:
    """Build an https:// redirect target from the (ProxyFix-corrected)
    Host header, or None."""
    host: bytes = b""
    for h_name, h_value in scope.get("headers", []):
        if h_name.lower() == b"host":
            host = h_value
            break
    if not host:
        return None
    path: str = scope.get("path", "/") or "/"
    raw_qs = scope.get("query_string", b"") or b""
    query = raw_qs.decode("latin-1", errors="replace") if raw_qs else ""
    target = b"https://" + host + path.encode("latin-1", errors="replace")
    if query:
        target += b"?" + query.encode("latin-1", errors="replace")
    return target


def _resolve_is_https(scope: Mapping[str, Any]) -> bool:
    """Return True when scope scheme is https or X-Forwarded-Proto is
    https (talisman.py:396-400)."""
    if scope.get("scheme") == "https":
        return True
    for h_name, h_value in scope.get("headers", []):
        if h_name.lower() == b"x-forwarded-proto":
            return h_value.lower() == b"https"
    return False


def _build_frame_options_value(talisman_config: dict[str, Any]) -> bytes | None:
    """Return X-Frame-Options header bytes (mirrors talisman.py:360-367),
    or None to omit."""
    frame_options: str | bool | None = talisman_config.get(
        "frame_options", "SAMEORIGIN"
    )
    if not frame_options:
        return None
    value = str(frame_options)
    if frame_options == "ALLOW-FROM":
        allow_from: str | None = talisman_config.get("frame_options_allow_from")
        if allow_from:
            value += f" {allow_from}"
    return value.encode("utf-8")


def _build_hsts_header_value(talisman_config: dict[str, Any]) -> str | None:
    """Return Strict-Transport-Security value (talisman.py:403-411) or
    None when disabled."""
    if not talisman_config.get("strict_transport_security", True):
        return None
    max_age: int = int(
        talisman_config.get("strict_transport_security_max_age", 31556926)
    )
    value = f"max-age={max_age}"
    if talisman_config.get("strict_transport_security_include_subdomains", True):
        value += "; includeSubDomains"
    if talisman_config.get("strict_transport_security_preload", False):
        value += "; preload"
    return value


def _build_permissions_policy_string(policy: dict[str, str] | str | None) -> str | None:
    """Serialise a Talisman-style permissions-policy dict to a header string."""
    if not policy:
        return None
    if isinstance(policy, str):
        return policy
    return ", ".join(f"{k}={v}" for k, v in policy.items())


class SecurityHeadersMiddleware(ASGIMiddleware):
    """Inject security headers into every HTTP response.

    * When ``settings.talisman_enabled`` is ``False`` no headers are added.
    * The CSP is derived from ``settings.talisman_dev_config`` in debug mode,
      else from ``settings.talisman_config``.
    """

    @staticmethod
    def _resolve_settings(scope: Scope) -> object | None:
        app = scope.get("app")
        if app is None:
            return None
        return getattr(getattr(app, "state", None), "settings", None)

    @staticmethod
    def _resolve_talisman_config(settings: object | None) -> dict[str, Any]:
        if settings is None:
            return {}
        debug: bool = bool(getattr(settings, "debug", False))
        if debug:
            return getattr(settings, "talisman_dev_config", {}) or {}
        return getattr(settings, "talisman_config", {}) or {}

    @staticmethod
    def _inject_nonce(
        scope: Scope, talisman_config: dict[str, Any]
    ) -> tuple[list[str], str | None]:
        """Generate a per-request CSP nonce and store it in scope state
        for Jinja2 templates."""
        nonce_in: list[str] = (
            talisman_config.get("content_security_policy_nonce_in") or []
        )
        nonce: str | None = None
        if nonce_in:
            nonce = secrets.token_urlsafe(_NONCE_LENGTH)
            scope_state: dict[str, Any] = scope.setdefault("state", {})
            scope_state[CSP_NONCE_SCOPE_KEY] = nonce
        return nonce_in, nonce

    @staticmethod
    def _build_headers(
        scope: Scope, talisman_config: dict[str, Any]
    ) -> list[tuple[bytes, bytes]]:
        nonce_in, nonce = SecurityHeadersMiddleware._inject_nonce(
            scope, talisman_config
        )

        csp_dict = talisman_config.get("content_security_policy")
        csp_str = _build_csp_string(csp_dict, nonce_in=nonce_in, nonce=nonce)

        csp_report_uri: str | None = talisman_config.get(
            "content_security_policy_report_uri"
        )
        if csp_str and csp_report_uri and "report-uri" not in csp_str:
            csp_str += "; report-uri " + csp_report_uri

        csp_header_name: bytes = (
            b"content-security-policy-report-only"
            if talisman_config.get("content_security_policy_report_only", False)
            else b"content-security-policy"
        )

        frame_options_bytes: bytes | None = _build_frame_options_value(talisman_config)

        is_https = _resolve_is_https(scope)
        hsts_value = _build_hsts_header_value(talisman_config) if is_https else None

        x_xss_protection: bool = bool(talisman_config.get("x_xss_protection", False))

        raw_permissions_policy = talisman_config.get(
            "permissions_policy", _DEFAULT_PERMISSIONS_POLICY
        )
        permissions_policy_str = _build_permissions_policy_string(
            raw_permissions_policy
        )

        x_content_type_options: bool = bool(
            talisman_config.get("x_content_type_options", True)
        )

        referrer_policy: str = str(
            talisman_config.get("referrer_policy", "strict-origin-when-cross-origin")
        )

        headers: list[tuple[bytes, bytes]] = []
        if x_content_type_options:
            headers.append((b"x-content-type-options", b"nosniff"))
        if referrer_policy:
            headers.append((b"referrer-policy", referrer_policy.encode("utf-8")))
        if frame_options_bytes is not None:
            headers.append((b"x-frame-options", frame_options_bytes))
        if x_xss_protection:
            headers.append((b"x-xss-protection", b"1; mode=block"))
        if permissions_policy_str:
            headers.append(
                (b"permissions-policy", permissions_policy_str.encode("utf-8"))
            )
        if csp_str:
            headers.append((csp_header_name, csp_str.encode("utf-8")))
        if hsts_value:
            headers.append((b"strict-transport-security", hsts_value.encode("utf-8")))
        return headers

    async def handle(
        self, scope: Scope, receive: Receive, send: Send, next_app: ASGIApp
    ) -> None:
        if scope["type"] != "http":
            await next_app(scope, receive, send)
            return

        settings = self._resolve_settings(scope)

        talisman_enabled: bool = True
        if settings is not None:
            talisman_enabled = bool(getattr(settings, "talisman_enabled", True))
        if not talisman_enabled:
            await next_app(scope, receive, send)
            return

        talisman_config = self._resolve_talisman_config(settings)

        if talisman_config.get("force_https", False) and not _resolve_is_https(scope):
            location = _build_force_https_location(scope)
            if location is not None:
                status = (
                    301 if talisman_config.get("force_https_permanent", False) else 302
                )
                await send(
                    {
                        "type": "http.response.start",
                        "status": status,
                        "headers": [
                            (b"location", location),
                            (b"content-length", b"0"),
                        ],
                    }
                )
                await send(cast("Message", {"type": "http.response.body", "body": b""}))
                return

        headers = self._build_headers(scope, talisman_config)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                existing: list[tuple[bytes, bytes]] = list(message.get("headers", []))
                existing.extend(headers)
                message = {**message, "headers": existing}
            await send(message)

        await next_app(scope, receive, send_with_headers)
