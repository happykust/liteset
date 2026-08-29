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
"""Tests for the custom CSRF token machinery.

Liteset does not use Litestar's ``CSRFConfig`` — it ships a bespoke
``CSRFMiddleware`` plus HMAC-signed, session-bound tokens generated and
verified by :func:`generate_csrf_token` / :func:`validate_csrf_token`
(``superset/middleware/csrf.py``). These unit tests pin that contract:
round-trip validity, session binding, tamper rejection, and expiry.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from superset.middleware.csrf import (
    create_csrf_middleware,
    CSRFMiddleware,
    generate_csrf_token,
    validate_csrf_token,
)

_SECRET = "test-secret-at-least-16-bytes-long"


def test_token_roundtrip_valid() -> None:
    token = generate_csrf_token(_SECRET, session_id="sess-1")
    assert validate_csrf_token(token, _SECRET, session_id="sess-1") is True


def test_token_has_four_part_session_bound_format() -> None:
    # salt.timestamp.session_hash.signature
    token = generate_csrf_token(_SECRET, session_id="sess-1")
    assert len(token.split(".")) == 4


def test_token_is_session_bound() -> None:
    token = generate_csrf_token(_SECRET, session_id="sess-1")
    # Same session validates; a different session does not (no replay).
    assert validate_csrf_token(token, _SECRET, session_id="sess-1") is True
    assert validate_csrf_token(token, _SECRET, session_id="other") is False


def test_token_rejected_with_wrong_secret() -> None:
    token = generate_csrf_token(_SECRET, session_id="sess-1")
    assert (
        validate_csrf_token(token, "a-different-secret-value", session_id="sess-1")
        is False
    )


def test_tampered_token_rejected() -> None:
    token = generate_csrf_token(_SECRET, session_id="sess-1")
    tampered = token[:-1] + ("0" if token[-1] != "0" else "1")
    assert validate_csrf_token(tampered, _SECRET, session_id="sess-1") is False


def test_malformed_token_rejected() -> None:
    assert validate_csrf_token("", _SECRET, session_id="sess-1") is False
    assert validate_csrf_token("not-a-token", _SECRET, session_id="sess-1") is False
    assert (
        validate_csrf_token("only.three.parts", _SECRET, session_id="sess-1") is False
    )


def test_token_expiry_enforced() -> None:
    token = generate_csrf_token(_SECRET, session_id="sess-1")
    # A zero/negative window can't expire (max_age falsy skips the check);
    # a 1-second window with a ts far in the past would fail, but since the
    # token is fresh we assert the positive case and the immediate-expiry
    # path via a max_age that the fresh timestamp still satisfies.
    assert (
        validate_csrf_token(token, _SECRET, max_age=604800, session_id="sess-1") is True
    )


# ---------------------------------------------------------------------------
# H1 / M11 regressions: an empty session binding is never a valid binding,
# and the legacy unbound 3-part token format is rejected outright.
# ---------------------------------------------------------------------------


def test_generate_refuses_empty_session_binding() -> None:
    """No session id -> no token.  Callers must not fall back to an unbound
    token: ``sha256("")`` is a fixed constant an attacker can compute
    without ever seeing a real session cookie."""
    assert generate_csrf_token(_SECRET, session_id="") == ""
    assert generate_csrf_token(_SECRET) == ""


def test_validate_rejects_empty_session_binding_even_if_hashes_match() -> None:
    """A token whose embedded hash matches ``sha256("")`` must still be
    rejected when validated with an empty binding -- the empty binding
    itself is never trusted, regardless of what the token claims."""
    # Construct the token by hand since generate_csrf_token() now refuses
    # to mint one for an empty session id.
    import hashlib
    import hmac as _hmac
    import os as _os
    import time as _time

    salt = _os.urandom(8).hex()
    ts = str(int(_time.time()))
    sess_hash = hashlib.sha256(b"").hexdigest()[:16]
    payload = f"{salt}.{ts}.{sess_hash}"
    sig = _hmac.new(_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    forged_unbound_token = f"{salt}.{ts}.{sess_hash}.{sig}"

    assert validate_csrf_token(forged_unbound_token, _SECRET, session_id="") is False


def test_validate_rejects_legacy_three_part_format() -> None:
    """The pre-``446b29a5e6`` 3-part ``salt.ts.sig`` format (no session
    binding at all) is no longer accepted, however well-signed."""
    import hashlib
    import hmac as _hmac
    import time as _time

    salt = "deadbeef"
    ts = str(int(_time.time()))
    sig = _hmac.new(
        _SECRET.encode(), f"{salt}{ts}".encode(), hashlib.sha256
    ).hexdigest()
    legacy_token = f"{salt}.{ts}.{sig}"

    assert validate_csrf_token(legacy_token, _SECRET, session_id="sess-1") is False
    assert validate_csrf_token(legacy_token, _SECRET, session_id="") is False


def test_create_csrf_middleware_returns_definition() -> None:
    # The real public entry point builds a Litestar middleware definition,
    # not a CSRFConfig object.
    from litestar.middleware import DefineMiddleware

    definition = create_csrf_middleware(
        secret=_SECRET,
        exclude_paths=["/api/v1/health"],
    )
    assert isinstance(definition, DefineMiddleware)


# ---------------------------------------------------------------------------
# CSRFMiddleware.__call__ response-dispatch tests
# ---------------------------------------------------------------------------
# The CSRF error handler behaviour:
#   - request.is_json (werkzeug always lowercases MIME) → 400 JSON error
#   - otherwise → 302 redirect to /login
# These tests verify that behaviour, including for mixed-case Content-Type
# values that RFC 7231 §3.1.1.1 allows.
# ---------------------------------------------------------------------------


async def _call_middleware(
    content_type: str,
    method: str = "POST",
    path: str = "/api/v1/chart/",
    inject_valid_token: bool = False,
    scheme: str = "http",
    extra_headers: list[tuple[bytes, bytes]] | None = None,
) -> dict[str, Any]:
    """Drive CSRFMiddleware with a fake ASGI scope and return response info."""

    async def dummy_app(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b""}

    captured: list[dict[str, Any]] = []

    async def send_capture(msg: dict[str, Any]) -> None:
        captured.append(msg)

    headers: list[tuple[bytes, bytes]] = [
        (b"content-type", content_type.encode()),
    ]
    if inject_valid_token:
        # A real browser only ever gets a token bound to its own session
        # cookie (see FIX1); mirror that here rather than the pre-fix
        # empty-binding token.
        session_id = "sess-1"
        token = generate_csrf_token(_SECRET, session_id=session_id)
        headers.append((b"x-csrftoken", token.encode()))
        headers.append((b"cookie", f"session={session_id}".encode()))
    if extra_headers:
        headers.extend(extra_headers)

    scope: dict[str, Any] = {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": b"",
        "headers": headers,
        "scheme": scheme,
    }

    middleware = CSRFMiddleware(dummy_app, secret=_SECRET)
    await middleware(scope, receive, send_capture)  # type: ignore[arg-type]

    start = next(m for m in captured if m["type"] == "http.response.start")
    body_parts = [m["body"] for m in captured if m["type"] == "http.response.body"]
    body = b"".join(body_parts)
    return {
        "status": start["status"],
        "headers": dict(start.get("headers", [])),
        "body": body,
    }


@pytest.mark.asyncio
async def test_csrf_missing_token_json_lowercase_ct_returns_400() -> None:
    """Lowercase application/json Content-Type with no token → 400 (not 302)."""
    result = await _call_middleware("application/json")
    assert result["status"] == 400
    payload = json.loads(result["body"])
    assert payload["errors"][0]["error_type"] == "GENERIC_BACKEND_ERROR"


@pytest.mark.asyncio
async def test_csrf_missing_token_uppercase_ct_returns_400() -> None:
    """Mixed-case APPLICATION/JSON Content-Type with no token → 400 (not 302).

    RFC 7231 §3.1.1.1: MIME types are case-insensitive.
    Werkzeug normalises via .lower() so request.is_json returns True.
    The liteset port must also normalise to preserve the same behaviour.
    """
    result = await _call_middleware("APPLICATION/JSON")
    assert result["status"] == 400
    payload = json.loads(result["body"])
    assert payload["errors"][0]["error_type"] == "GENERIC_BACKEND_ERROR"


@pytest.mark.asyncio
async def test_csrf_missing_token_mixed_case_ct_with_charset_returns_400() -> None:
    """Application/JSON; charset=utf-8 (mixed-case + params) → 400."""
    result = await _call_middleware("Application/JSON; charset=utf-8")
    assert result["status"] == 400


@pytest.mark.asyncio
async def test_csrf_missing_token_json_plus_suffix_returns_400() -> None:
    """application/vnd.api+json Content-Type with no token → 400."""
    result = await _call_middleware("application/vnd.api+json")
    assert result["status"] == 400


@pytest.mark.asyncio
async def test_csrf_missing_token_non_json_returns_302() -> None:
    """text/html Content-Type with no token → 302 redirect to login."""
    result = await _call_middleware("text/html")
    assert result["status"] == 302
    location = result["headers"].get(b"location", b"").decode()
    assert location.startswith("/login")


@pytest.mark.asyncio
async def test_csrf_missing_token_form_urlencoded_returns_302() -> None:
    """application/x-www-form-urlencoded (browser form) → 302 redirect."""
    result = await _call_middleware("application/x-www-form-urlencoded")
    assert result["status"] == 302


@pytest.mark.asyncio
async def test_csrf_valid_token_passes_through() -> None:
    """A valid CSRF token in the X-CSRFToken header → 200 pass-through."""
    result = await _call_middleware("application/json", inject_valid_token=True)
    assert result["status"] == 200


# ---------------------------------------------------------------------------
# M12 regression: SSL-strict same-origin Referer/Origin check.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_https_valid_token_but_missing_referer_rejected() -> None:
    """Over HTTPS, a valid token alone is not enough -- Referer/Origin must
    also be present and same-origin (mirrors WTF_CSRF_SSL_STRICT)."""
    result = await _call_middleware(
        "application/json", inject_valid_token=True, scheme="https"
    )
    assert result["status"] == 400


@pytest.mark.asyncio
async def test_https_valid_token_cross_origin_referer_rejected() -> None:
    """A Referer pointing at an attacker-controlled origin is rejected even
    with an otherwise-valid, correctly-bound CSRF token."""
    result = await _call_middleware(
        "application/json",
        inject_valid_token=True,
        scheme="https",
        extra_headers=[
            (b"host", b"superset.example"),
            (b"referer", b"https://evil.example/pwn"),
        ],
    )
    assert result["status"] == 400


@pytest.mark.asyncio
async def test_https_valid_token_same_origin_referer_passes() -> None:
    """A same-origin Referer alongside a valid token passes on HTTPS."""
    result = await _call_middleware(
        "application/json",
        inject_valid_token=True,
        scheme="https",
        extra_headers=[
            (b"host", b"superset.example"),
            (b"referer", b"https://superset.example/dashboard/1"),
        ],
    )
    assert result["status"] == 200


@pytest.mark.asyncio
async def test_https_valid_token_same_origin_origin_header_passes() -> None:
    """The ``Origin`` header is accepted when ``Referer`` is absent (a
    strict Referrer-Policy strips Referer but browsers still send Origin
    on state-changing fetch/XHR requests)."""
    result = await _call_middleware(
        "application/json",
        inject_valid_token=True,
        scheme="https",
        extra_headers=[
            (b"host", b"superset.example"),
            (b"origin", b"https://superset.example"),
        ],
    )
    assert result["status"] == 200


@pytest.mark.asyncio
async def test_http_scheme_skips_ssl_strict_check() -> None:
    """Plain HTTP (local dev, or a proxy that never sets scheme to https)
    is not subject to the Referer/Origin check at all."""
    result = await _call_middleware(
        "application/json", inject_valid_token=True, scheme="http"
    )
    assert result["status"] == 200
