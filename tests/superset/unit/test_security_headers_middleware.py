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
"""Unit tests for SecurityHeadersMiddleware._build_headers.

Covers the two regressions vs flask-talisman that were introduced in the
liteset port:

1. X-Frame-Options was hardcoded to SAMEORIGIN regardless of talisman_config
   (should be omitted when frame_options=False, DENY when frame_options=DENY,
   and ALLOW-FROM <domain> when frame_options=ALLOW-FROM).

2. content_security_policy_report_only and content_security_policy_report_uri
   were silently dropped (should change CSP header name to
   Content-Security-Policy-Report-Only and append "; report-uri <uri>" to the
   policy string respectively).
"""

from __future__ import annotations

from superset.middleware.security_headers import (  # noqa: E402
    _build_csp_string,
    SecurityHeadersMiddleware,
)


def _headers_dict(talisman_config: dict) -> dict[str, str]:
    """Call _build_headers with a minimal ASGI scope and return a plain dict."""
    scope: dict = {"type": "http", "scheme": "http", "headers": [], "state": {}}
    raw = SecurityHeadersMiddleware._build_headers(scope, talisman_config)
    return {k.decode(): v.decode() for k, v in raw}


# ---------------------------------------------------------------------------
# X-Frame-Options
# ---------------------------------------------------------------------------


def test_frame_options_default_is_sameorigin():
    """Default talisman_config (no frame_options key) → SAMEORIGIN."""
    h = _headers_dict({})
    assert h.get("x-frame-options") == "SAMEORIGIN"


def test_frame_options_deny():
    """frame_options=DENY → X-Frame-Options: DENY."""
    h = _headers_dict({"frame_options": "DENY"})
    assert h.get("x-frame-options") == "DENY"


def test_frame_options_false_omits_header():
    """frame_options=False → no X-Frame-Options header at all.

    Talisman: ``if not options['frame_options']: return`` (talisman.py:361).
    This is the critical case for sites that intentionally allow iframing.
    """
    h = _headers_dict({"frame_options": False})
    assert "x-frame-options" not in h


def test_frame_options_allow_from_appends_domain():
    """frame_options=ALLOW-FROM + frame_options_allow_from → ALLOW-FROM <domain>.

    Talisman: headers['X-Frame-Options'] += ' {}'.format(allow_from)
    (talisman.py:365-367).
    """
    h = _headers_dict(
        {
            "frame_options": "ALLOW-FROM",
            "frame_options_allow_from": "https://embed.example.com",
        }
    )
    assert h.get("x-frame-options") == "ALLOW-FROM https://embed.example.com"


def test_frame_options_allow_from_without_domain():
    # frame_options=ALLOW-FROM without allow_from set → value emitted without domain,
    # no crash (talisman.py:365: appends domain only when allow_from is set).
    h = _headers_dict({"frame_options": "ALLOW-FROM"})
    assert h.get("x-frame-options") == "ALLOW-FROM"


# ---------------------------------------------------------------------------
# CSP report-only
# ---------------------------------------------------------------------------


def test_csp_enforcing_by_default():
    """Default config → header name is 'content-security-policy' (enforcing)."""
    csp = {"default-src": "'self'"}
    h = _headers_dict({"content_security_policy": csp})
    assert "content-security-policy" in h
    assert "content-security-policy-report-only" not in h


def test_csp_report_only_changes_header_name():
    """content_security_policy_report_only=True → Content-Security-Policy-Report-Only.

    Talisman (talisman.py:390-391): appends '-Report-Only' to header name.
    The browser treats this as non-enforcing (logs violations, does not block).
    """
    csp = {"default-src": "'self'"}
    h = _headers_dict(
        {
            "content_security_policy": csp,
            "content_security_policy_report_only": True,
        }
    )
    assert "content-security-policy-report-only" in h
    assert "content-security-policy" not in h


def test_csp_report_only_false_keeps_enforcing():
    """Explicit report_only=False → enforcing header name (no change from default)."""
    csp = {"default-src": "'self'"}
    h = _headers_dict(
        {
            "content_security_policy": csp,
            "content_security_policy_report_only": False,
        }
    )
    assert "content-security-policy" in h
    assert "content-security-policy-report-only" not in h


# ---------------------------------------------------------------------------
# CSP report-uri
# ---------------------------------------------------------------------------


def test_csp_report_uri_appended():
    """content_security_policy_report_uri appends '; report-uri <uri>' to policy.

    Talisman:
        if self.content_security_policy_report_uri and 'report-uri' not in policy:
            policy += '; report-uri ' + self.content_security_policy_report_uri
    (talisman.py:385-387).
    """
    csp = {"default-src": "'self'"}
    h = _headers_dict(
        {
            "content_security_policy": csp,
            "content_security_policy_report_uri": "https://csp.example.com/report",
        }
    )
    assert "content-security-policy" in h
    assert h["content-security-policy"].endswith(
        "; report-uri https://csp.example.com/report"
    )


def test_csp_report_uri_not_appended_when_already_present():
    """report-uri already in CSP string → talisman does NOT append again."""
    csp = "default-src 'self'; report-uri https://existing.example.com/r"
    h = _headers_dict(
        {
            "content_security_policy": csp,
            "content_security_policy_report_uri": "https://other.example.com/r",
        }
    )
    assert h["content-security-policy"].count("report-uri") == 1
    assert "https://other.example.com/r" not in h["content-security-policy"]


def test_csp_report_uri_without_csp_policy():
    """report_uri set but no CSP policy → no CSP header emitted (no crash)."""
    h = _headers_dict(
        {
            "content_security_policy": False,
            "content_security_policy_report_uri": "https://csp.example.com/report",
        }
    )
    assert "content-security-policy" not in h
    assert "content-security-policy-report-only" not in h


def test_csp_report_only_with_report_uri():
    """Both report_only and report_uri set → report-only header WITH appended uri."""
    csp = {"default-src": "'self'"}
    h = _headers_dict(
        {
            "content_security_policy": csp,
            "content_security_policy_report_only": True,
            "content_security_policy_report_uri": "https://csp.example.com/report",
        }
    )
    assert "content-security-policy-report-only" in h
    assert "content-security-policy" not in h
    assert (
        "; report-uri https://csp.example.com/report"
        in h["content-security-policy-report-only"]
    )


# ---------------------------------------------------------------------------
# _build_csp_string — standalone unit tests (these existed implicitly; make
# explicit to guard the string format on which report-uri appending relies)
# ---------------------------------------------------------------------------


def test_build_csp_string_dict():
    result = _build_csp_string({"default-src": "'self'", "object-src": "'none'"})
    assert result == "default-src 'self'; object-src 'none'"


def test_build_csp_string_falsy_returns_none():
    assert _build_csp_string(None) is None
    assert _build_csp_string(False) is None  # type: ignore[arg-type]
    assert _build_csp_string({}) is None


def test_build_csp_string_str_passthrough():
    policy = "default-src 'self'; object-src 'none'"
    assert _build_csp_string(policy) == policy


def test_referrer_policy_honours_config():
    """A configured ``referrer_policy`` is emitted verbatim (flask-talisman
    emits ``self.referrer_policy``, whatever was passed to init_app)."""
    headers = _headers_dict({"referrer_policy": "no-referrer"})
    assert headers["referrer-policy"] == "no-referrer"


def test_referrer_policy_default():
    headers = _headers_dict({})
    assert headers["referrer-policy"] == "strict-origin-when-cross-origin"


def test_x_content_type_options_false_omits_header():
    """``x_content_type_options: False`` suppresses the header (talisman
    emits it only when the option is truthy; init_app default True)."""
    headers = _headers_dict({"x_content_type_options": False})
    assert "x-content-type-options" not in headers


def test_x_content_type_options_default_emits_nosniff():
    headers = _headers_dict({})
    assert headers["x-content-type-options"] == "nosniff"


# ---------------------------------------------------------------------------
# force_https (R19-03) — HTTP->HTTPS redirect, mirroring talisman _force_https
# ---------------------------------------------------------------------------

from types import SimpleNamespace  # noqa: E402

from superset.middleware.security_headers import (  # noqa: E402
    _build_force_https_location,
)


def _make_scope(scheme: str = "http", host: str = "superset.example.com",
                path: str = "/dashboard/1/", query: bytes = b"") -> dict:
    return {
        "type": "http",
        "scheme": scheme,
        "path": path,
        "query_string": query,
        "headers": [(b"host", host.encode())],
        "state": {},
        "app": None,
    }


async def _drive(scope: dict, talisman_config: dict, talisman_enabled: bool = True):
    """Drive SecurityHeadersMiddleware.handle once; return (status, headers)."""
    settings = SimpleNamespace(
        debug=False,
        talisman_enabled=talisman_enabled,
        talisman_config=talisman_config,
        talisman_dev_config=talisman_config,
    )
    scope["app"] = SimpleNamespace(state=SimpleNamespace(settings=settings))
    captured: dict = {"status": None, "headers": {}}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            captured["status"] = message["status"]
            captured["headers"] = {
                k.decode(): v.decode() for k, v in message.get("headers", [])
            }

    async def next_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    await SecurityHeadersMiddleware().handle(scope, receive, send, next_app)
    return captured


def test_force_https_location_builds_https_url():
    loc = _build_force_https_location(
        _make_scope(path="/c/", query=b"a=1&b=2")
    )
    assert loc == b"https://superset.example.com/c/?a=1&b=2"


async def test_force_https_redirects_http_to_https():
    res = await _drive(_make_scope(scheme="http"), {"force_https": True})
    assert res["status"] == 302
    assert res["headers"]["location"] == "https://superset.example.com/dashboard/1/"


async def test_force_https_permanent_uses_301():
    res = await _drive(
        _make_scope(scheme="http"),
        {"force_https": True, "force_https_permanent": True},
    )
    assert res["status"] == 301


async def test_force_https_no_redirect_when_already_https():
    res = await _drive(_make_scope(scheme="https"), {"force_https": True})
    assert res["status"] == 200


async def test_force_https_respects_x_forwarded_proto():
    scope = _make_scope(scheme="http")
    scope["headers"].append((b"x-forwarded-proto", b"https"))
    res = await _drive(scope, {"force_https": True})
    assert res["status"] == 200


async def test_no_redirect_when_force_https_disabled():
    res = await _drive(_make_scope(scheme="http"), {"force_https": False})
    assert res["status"] == 200


async def test_no_redirect_when_force_https_absent_default():
    # Default (key absent) must NOT redirect — Superset ships force_https=False.
    res = await _drive(_make_scope(scheme="http"), {})
    assert res["status"] == 200
