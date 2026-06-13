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
"""Unit tests for ProxyFixMiddleware._get_trusted_value.

Pins the trusted-value extraction to werkzeug ProxyFix._get_real_value
semantics:

    if len(values) >= trusted:
        return values[-trusted]
    return None

A shorter-than-configured forwarded chain must yield None rather than the
LEFTMOST (client-controllable) value: returning the leftmost entry under a
multi-proxy config (count >= 2) would let an attacker spoof X-Forwarded-For /
-Proto / -Host by sending fewer values than configured.
"""

from __future__ import annotations

from superset.middleware.proxy_fix import _get_trusted_value


def _xff(raw: str | None) -> bytes | None:
    return raw.encode("latin-1") if raw is not None else None


# ---------------------------------------------------------------------------
# Common path: enough values present (matches werkzeug values[-trusted]).
# ---------------------------------------------------------------------------


def test_single_proxy_single_value():
    assert _get_trusted_value(_xff("1.2.3.4"), 1) == "1.2.3.4"


def test_single_proxy_picks_rightmost():
    # 2 proxies appended; trust 1 -> rightmost (nearest trusted proxy).
    assert _get_trusted_value(_xff("client, proxy1"), 1) == "proxy1"


def test_two_proxies_picks_second_from_right():
    assert _get_trusted_value(_xff("client, proxy1, proxy2"), 2) == "proxy1"


def test_exact_count_returns_leftmost_when_equal():
    # len == trusted -> values[-trusted] == leftmost, which is correct here.
    assert _get_trusted_value(_xff("a, b"), 2) == "a"


# ---------------------------------------------------------------------------
# shorter-than-configured chain MUST return None (werkzeug parity),
# NOT the leftmost client-controlled value.
# ---------------------------------------------------------------------------


def test_fewer_values_than_trusted_returns_none():
    # x_for=2 but only ONE forwarded value present (attacker-supplied):
    # werkzeug discards it; liteset must too.
    assert _get_trusted_value(_xff("attacker-spoofed"), 2) is None


def test_two_values_three_trusted_returns_none():
    assert _get_trusted_value(_xff("a, b"), 3) is None


def test_spoofed_proto_with_high_trust_returns_none():
    # x_proto=2, single spoofed value -> must NOT be trusted as https.
    assert _get_trusted_value(_xff("https"), 2) is None


# ---------------------------------------------------------------------------
# Degenerate inputs.
# ---------------------------------------------------------------------------


def test_absent_header_returns_none():
    assert _get_trusted_value(None, 1) is None


def test_zero_trusted_returns_none():
    assert _get_trusted_value(_xff("1.2.3.4"), 0) is None


def test_empty_value_returns_none():
    assert _get_trusted_value(_xff(""), 1) is None
