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
"""Unit tests for RateLimitMiddleware.

Pins the FAB / flask-limiter parity:

* ``RATELIMIT_ENABLED`` is the master switch — when False NOTHING is limited.
* ``RATELIMIT_APPLICATION`` limits every request, keyed by client IP.
* ``AUTH_RATE_LIMITED`` + ``AUTH_RATE_LIMIT`` limit login POSTs (brute force).
* Limit strings parse like the ``limits`` library ("50 per second", "5/s", ...).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from superset.middleware.rate_limit import parse_rate_limit, RateLimitMiddleware

# ---------------------------------------------------------------------------
# parse_rate_limit
# ---------------------------------------------------------------------------


def test_parse_per_second():
    assert parse_rate_limit("50 per second") == (50, 1)


def test_parse_auth_default():
    assert parse_rate_limit("5 per second") == (5, 1)


def test_parse_per_multiple():
    assert parse_rate_limit("10 per 20 second") == (10, 20)


def test_parse_per_hour():
    assert parse_rate_limit("100 per hour") == (100, 3600)


def test_parse_slash_form():
    assert parse_rate_limit("100/minute") == (100, 60)
    assert parse_rate_limit("5/s") == (5, 1)


def test_parse_invalid_returns_none():
    assert parse_rate_limit("") is None
    assert parse_rate_limit(None) is None
    assert parse_rate_limit("garbage") is None
    assert parse_rate_limit("0 per second") is None


# ---------------------------------------------------------------------------
# Fake Redis with a sliding-window emulation good enough for the middleware.
# ---------------------------------------------------------------------------


class _FakePipe:
    def __init__(self, store: dict[str, list[float]]) -> None:
        self._store = store
        self._ops: list[tuple[str, tuple[Any, ...]]] = []

    def zremrangebyscore(self, key: str, lo: float, hi: float) -> None:
        self._ops.append(("zrem", (key, lo, hi)))

    def zadd(self, key: str, mapping: dict[str, float]) -> None:
        self._ops.append(("zadd", (key, mapping)))

    def zcard(self, key: str) -> None:
        self._ops.append(("zcard", (key,)))

    def expire(self, key: str, ttl: int) -> None:
        self._ops.append(("expire", (key, ttl)))

    async def execute(self) -> list[Any]:
        results: list[Any] = []
        for op, args in self._ops:
            if op == "zrem":
                key, _lo, hi = args
                bucket = self._store.setdefault(key, [])
                self._store[key] = [t for t in bucket if t > hi]
                results.append(0)
            elif op == "zadd":
                key, mapping = args
                bucket = self._store.setdefault(key, [])
                bucket.extend(mapping.values())
                results.append(len(mapping))
            elif op == "zcard":
                (key,) = args
                results.append(len(self._store.get(key, [])))
            else:  # expire
                results.append(True)
        return results


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, list[float]] = {}

    def pipeline(self, transaction: bool = True) -> _FakePipe:
        return _FakePipe(self.store)


def _make_scope(
    *,
    settings: SimpleNamespace,
    redis: Any,
    method: str = "GET",
    path: str = "/api/v1/chart/",
    client_ip: str = "1.2.3.4",
) -> dict[str, Any]:
    app = SimpleNamespace(state=SimpleNamespace(settings=settings, redis=redis))
    return {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [],
        "client": (client_ip, 5555),
        "app": app,
    }


async def _run(
    middleware: RateLimitMiddleware, scope: dict[str, Any]
) -> dict[str, Any]:
    """Drive the middleware once; return {'status':..., 'headers': {...}}."""
    captured: dict[str, Any] = {"status": None, "headers": {}}

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            captured["status"] = message["status"]
            captured["headers"] = {
                k.decode(): v.decode() for k, v in message.get("headers", [])
            }

    async def next_app(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    await middleware.handle(scope, receive, send, next_app)
    return captured


def _settings(**overrides: Any) -> SimpleNamespace:
    base = {
        "ratelimit_enabled": True,
        "ratelimit_application": "3 per second",
        "auth_rate_limited": True,
        "auth_rate_limit": "2 per second",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# Master switch
# ---------------------------------------------------------------------------


async def test_disabled_switch_never_limits():
    """ratelimit_enabled=False -> pass through even far past the limit."""
    mw = RateLimitMiddleware()
    redis = _FakeRedis()
    settings = _settings(ratelimit_enabled=False)
    for _ in range(20):
        res = await _run(mw, _make_scope(settings=settings, redis=redis))
    assert res["status"] == 200


async def test_no_redis_fails_open():
    mw = RateLimitMiddleware()
    settings = _settings()
    for _ in range(20):
        res = await _run(mw, _make_scope(settings=settings, redis=None))
    assert res["status"] == 200


# ---------------------------------------------------------------------------
# Application limit
# ---------------------------------------------------------------------------


async def test_application_limit_enforced():
    """3 per second -> 4th request in the window is 429."""
    mw = RateLimitMiddleware()
    redis = _FakeRedis()
    settings = _settings()
    statuses = [
        (await _run(mw, _make_scope(settings=settings, redis=redis)))["status"]
        for _ in range(4)
    ]
    assert statuses == [200, 200, 200, 429]


async def test_application_limit_sets_headers():
    mw = RateLimitMiddleware()
    redis = _FakeRedis()
    settings = _settings()
    res = await _run(mw, _make_scope(settings=settings, redis=redis))
    assert res["headers"].get("x-ratelimit-limit") == "3"
    assert "x-ratelimit-remaining" in res["headers"]


async def test_health_path_excluded_from_app_limit():
    mw = RateLimitMiddleware()
    redis = _FakeRedis()
    settings = _settings()
    for _ in range(10):
        res = await _run(
            mw, _make_scope(settings=settings, redis=redis, path="/health")
        )
    assert res["status"] == 200


async def test_per_ip_keying():
    """Different client IPs have independent application-limit buckets."""
    mw = RateLimitMiddleware()
    redis = _FakeRedis()
    settings = _settings()
    for _ in range(3):
        await _run(mw, _make_scope(settings=settings, redis=redis, client_ip="1.1.1.1"))
    # A different IP starts fresh.
    res = await _run(
        mw, _make_scope(settings=settings, redis=redis, client_ip="2.2.2.2")
    )
    assert res["status"] == 200


# ---------------------------------------------------------------------------
# Auth limit (brute-force protection on login POST)
# ---------------------------------------------------------------------------


async def test_auth_limit_on_login_post():
    """POST /login/ -> 2 per second auth limit kicks in on the 3rd."""
    mw = RateLimitMiddleware()
    redis = _FakeRedis()
    # Make the application limit generous so the auth limit is what bites.
    settings = _settings(ratelimit_application="100 per second")
    statuses = [
        (
            await _run(
                mw,
                _make_scope(
                    settings=settings, redis=redis, method="POST", path="/login/"
                ),
            )
        )["status"]
        for _ in range(3)
    ]
    assert statuses == [200, 200, 429]


async def test_auth_limit_disabled_when_not_auth_rate_limited():
    mw = RateLimitMiddleware()
    redis = _FakeRedis()
    settings = _settings(
        ratelimit_application="100 per second", auth_rate_limited=False
    )
    for _ in range(10):
        res = await _run(
            mw,
            _make_scope(settings=settings, redis=redis, method="POST", path="/login/"),
        )
    assert res["status"] == 200


async def test_get_login_not_auth_limited():
    """Auth limit is POST-only; GET /login/ only hits the (generous) app limit."""
    mw = RateLimitMiddleware()
    redis = _FakeRedis()
    settings = _settings(ratelimit_application="100 per second")
    for _ in range(10):
        res = await _run(
            mw,
            _make_scope(settings=settings, redis=redis, method="GET", path="/login/"),
        )
    assert res["status"] == 200
