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
"""Rate-limiting middleware for Superset.

ASGI port of the rate limiting wired up in the upstream
``BaseSecurityManager``:

* ``RATELIMIT_ENABLED`` is the master kill switch (the upstream limiter
  disables *all* limits — application and per-route — when this is False).
  Upstream default is ``SUPERSET_ENV == "production"``.
* ``RATELIMIT_APPLICATION`` (default ``"50 per second"``) is the
  application-wide limit applied to *every* request, keyed by client IP
  (upstream ``key_func=get_remote_address``).
* ``AUTH_RATE_LIMITED`` (default True) + ``AUTH_RATE_LIMIT``
  (default ``"5 per second"``) limit POST requests to the login endpoint —
  brute-force protection — mirroring
  ``limiter.limit(auth_rate_limit, methods=["POST"])(auth_view.blueprint)``.

Both limits are keyed by the client IP (``get_remote_address``), resolved
from ``scope["client"]`` *after* :class:`ProxyFixMiddleware` has applied the
trusted ``X-Forwarded-For``.  A login POST is subject to BOTH limits (just
like the upstream application limit + the blueprint ``@limit``).

Enforcement uses a Redis sliding-window counter.  When Redis is unavailable
the middleware falls back to an in-process sliding-window store (mirroring
Flask-Limiter's default in-memory backend when ``RATELIMIT_STORAGE_URI`` is
unset) so limits still apply -- per worker process, not cluster-wide -- and
logs a one-time warning so operators notice the degraded, non-shared mode.
A genuine Redis *error* (as opposed to Redis simply not being configured)
still fails that one check open, so a transient outage cannot take the
application down.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid

from litestar.middleware.base import ASGIMiddleware
from litestar.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

_EXCLUDED_PATHS: frozenset[str] = frozenset(
    {
        "/api/v1/health",
        "/health",
        "/healthcheck",
        "/ping",
        "/healthz",
    }
)

# Browser login form (session cookie flow) and the JSON API login endpoint
# (POST /api/v1/security/login, used by API/CLI clients) both need brute
# force protection.
_LOGIN_PATHS: frozenset[str] = frozenset(
    {"/login", "/login/", "/api/v1/security/login"}
)

_REDIS_KEY_PREFIX = "ratelimit:"

_GRANULARITY_SECONDS: dict[str, int] = {
    "second": 1,
    "sec": 1,
    "s": 1,
    "minute": 60,
    "min": 60,
    "m": 60,
    "hour": 3600,
    "h": 3600,
    "day": 86400,
    "d": 86400,
}

_PER_RE = re.compile(
    r"^\s*(\d+)\s*per\s*(\d+)?\s*([a-zA-Z]+)\s*$",
)
_SLASH_RE = re.compile(r"^\s*(\d+)\s*/\s*(\d+)?\s*([a-zA-Z]+)\s*$")


def parse_rate_limit(spec: str | None) -> tuple[int, int] | None:
    """Parse an upstream rate-limit string into ``(count, window_seconds)``.

    Supports the ``limits``-library formats used by Superset config:

    * ``"<count> per [<multiple>] <granularity>"`` — e.g. ``"50 per second"``,
      ``"10 per 20 second"``.
    * ``"<count>/[<multiple>]<granularity>"`` — e.g. ``"5/second"``.

    ``granularity`` is one of second / minute / hour / day (and common
    abbreviations).  Returns ``None`` when *spec* is empty or unparseable
    (callers then skip that limit rather than crash).
    """
    if not spec:
        return None
    for pattern in (_PER_RE, _SLASH_RE):
        match = pattern.match(spec)
        if not match:
            continue
        count = int(match.group(1))
        multiple = int(match.group(2)) if match.group(2) else 1
        granularity = match.group(3).lower().rstrip("s") or "second"
        unit_seconds = _GRANULARITY_SECONDS.get(granularity)
        if unit_seconds is None:
            # Retry with the original (un-stripped) word for "s"/"sec"/etc.
            unit_seconds = _GRANULARITY_SECONDS.get(match.group(3).lower())
        if unit_seconds is None or count <= 0:
            return None
        return count, unit_seconds * multiple
    return None


class RateLimitMiddleware(ASGIMiddleware):
    """Apply Superset's application + auth rate limits, keyed by client IP.

    Reads from ``app.state.settings``:

    * ``ratelimit_enabled`` (bool) — master switch; when False the middleware
      passes through unconditionally.
    * ``ratelimit_application`` (str) — application-wide limit (all requests).
    * ``auth_rate_limited`` (bool) + ``auth_rate_limit`` (str) — login POST limit.

    When Redis is unavailable (``app.state.redis`` is ``None``) limits are
    still enforced via an in-process fallback store — see
    :func:`_memory_sliding_window_check`.  A Redis *error* on an otherwise
    configured client still fails that one check open, so a transient
    outage never blocks the application.
    """

    async def handle(  # noqa: C901
        self, scope: Scope, receive: Receive, send: Send, next_app: ASGIApp
    ) -> None:
        if scope["type"] != "http":
            await next_app(scope, receive, send)
            return

        settings = getattr(getattr(scope.get("app"), "state", None), "settings", None)

        if settings is None or not getattr(settings, "ratelimit_enabled", False):
            await next_app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        method: str = str(scope.get("method", "GET")).upper()

        checks: list[tuple[str, int, int]] = []

        if path not in _EXCLUDED_PATHS:
            app_limit = parse_rate_limit(
                getattr(settings, "ratelimit_application", None)
            )
            if app_limit is not None:
                checks.append(("app", app_limit[0], app_limit[1]))

        if (
            method == "POST"
            and path in _LOGIN_PATHS
            and getattr(settings, "auth_rate_limited", False)
        ):
            auth_limit = parse_rate_limit(getattr(settings, "auth_rate_limit", None))
            if auth_limit is not None:
                checks.append(("auth", auth_limit[0], auth_limit[1]))

        if not checks:
            await next_app(scope, receive, send)
            return

        redis = getattr(getattr(scope.get("app"), "state", None), "redis", None)
        if redis is None:
            _warn_no_shared_store_once()

        identity = _resolve_identity(scope)

        rl_headers: list[tuple[bytes, bytes]] | None = None
        now = time.time()
        for namespace, max_requests, window in checks:
            key = f"{_REDIS_KEY_PREFIX}{namespace}:{identity}"
            try:
                if redis is not None:
                    remaining, reset_at = await _sliding_window_check(
                        redis, key, max_requests, window, now
                    )
                else:
                    remaining, reset_at = await _memory_sliding_window_check(
                        key, max_requests, window, now
                    )
            except Exception:
                # Redis error — fail open for this check.  The in-process
                # fallback never raises, so this only ever fires for a real
                # Redis client.
                logger.debug("Rate limit store error, bypassing", exc_info=True)
                continue

            if remaining < 0:
                retry_after = str(int(reset_at - time.time()) + 1)
                await _send_429(send, max_requests, reset_at, retry_after)
                return

            if rl_headers is None or remaining < int(rl_headers[1][1]):
                rl_headers = [
                    (b"x-ratelimit-limit", str(max_requests).encode()),
                    (b"x-ratelimit-remaining", str(max(remaining, 0)).encode()),
                    (b"x-ratelimit-reset", str(int(reset_at)).encode()),
                ]

        captured_headers = rl_headers or []

        async def send_with_rl_headers(message: Message) -> None:
            if message["type"] == "http.response.start" and captured_headers:
                existing: list[tuple[bytes, bytes]] = list(message.get("headers", []))
                existing.extend(captured_headers)
                message = {**message, "headers": existing}
            await send(message)

        await next_app(scope, receive, send_with_rl_headers)


def _resolve_identity(scope: Scope) -> str:
    if client := scope.get("client"):
        return f"ip:{client[0]}"
    return "ip:unknown"


# ---------------------------------------------------------------------------
# In-process fallback store — used when ``app.state.redis`` is ``None``
# (REDIS_URL unset, or the Redis client failed to initialise).  Mirrors
# Flask-Limiter's default in-memory backend: sliding-window counters keyed
# per (namespace, client identity), enforced per worker process rather than
# cluster-wide.  Never raises — a lock-protected in-process dict cannot fail
# the way a network call to Redis can, so callers do not need a fail-open
# path for this branch.
# ---------------------------------------------------------------------------
_memory_windows: dict[str, list[float]] = {}
_memory_store_lock = asyncio.Lock()

#: A key idle for longer than the largest window it could belong to carries no
#: information; sweeping is amortised so it costs nothing per request.
_MEMORY_KEY_MAX_IDLE: int = 3600
_MEMORY_PURGE_INTERVAL: int = 60
_last_memory_purge: float = 0.0

_no_shared_store_warning_logged = False


def _warn_no_shared_store_once() -> None:
    """Log once per process that rate limiting is running without a shared
    (Redis) store, so operators notice the degraded, per-worker-only mode."""
    global _no_shared_store_warning_logged
    if _no_shared_store_warning_logged:
        return
    _no_shared_store_warning_logged = True
    logger.warning(
        "RATELIMIT_ENABLED is set but no shared Redis store is available "
        "(REDIS_URL is not configured, or the Redis client failed to "
        "initialise); falling back to an in-process sliding-window store. "
        "Limits are enforced per worker process, not cluster-wide."
    )


async def _memory_sliding_window_check(
    key: str,
    max_requests: int,
    window: int,
    now: float,
) -> tuple[int, float]:
    """In-process equivalent of :func:`_sliding_window_check`.

    Returns ``(remaining, reset_timestamp)``; remaining is negative when
    exceeded.  Not shared across worker processes or hosts.
    """
    window_start = now - window
    reset_at = now + window

    async with _memory_store_lock:
        timestamps = _memory_windows.setdefault(key, [])
        cutoff = 0
        while cutoff < len(timestamps) and timestamps[cutoff] <= window_start:
            cutoff += 1
        if cutoff:
            del timestamps[:cutoff]
        timestamps.append(now)
        current_count = len(timestamps)
        _purge_idle_windows(now)

    remaining = max_requests - current_count
    return remaining, reset_at


def _purge_idle_windows(now: float) -> None:
    """Drop keys whose whole window has expired.

    Trimming timestamps inside a key is not enough: the key itself would
    survive forever. The key space is ``(namespace, client identity)`` and the
    identity derives from the client address, so it is attacker-influenced —
    unbounded growth here would be a memory-exhaustion vector inside the very
    code meant to absorb abusive traffic. Called under ``_memory_store_lock``.
    """
    global _last_memory_purge
    if now - _last_memory_purge < _MEMORY_PURGE_INTERVAL:
        return
    _last_memory_purge = now
    horizon = now - _MEMORY_KEY_MAX_IDLE
    for stale_key in [
        k for k, ts in _memory_windows.items() if not ts or ts[-1] <= horizon
    ]:
        del _memory_windows[stale_key]


async def _sliding_window_check(
    redis: object,
    key: str,
    max_requests: int,
    window: int,
    now: float,
) -> tuple[int, float]:
    """Execute a sliding-window rate-limit check using Redis sorted sets.

    Returns ``(remaining, reset_timestamp)``; remaining is negative when exceeded.
    """
    window_start = now - window
    reset_at = now + window

    pipe = redis.pipeline(transaction=True)  # type: ignore[attr-defined]
    pipe.zremrangebyscore(key, 0, window_start)
    member = f"{now}:{uuid.uuid4().hex[:8]}"
    pipe.zadd(key, {member: now})
    pipe.zcard(key)
    pipe.expire(key, window + 1)

    results = await pipe.execute()
    current_count: int = results[2]
    remaining = max_requests - current_count

    return remaining, reset_at


async def _send_429(
    send: Send,
    limit: int,
    reset_at: float,
    retry_after: str,
) -> None:
    body = b'{"message": "Rate limit exceeded. Please try again later.", "status": 429}'
    headers: list[tuple[bytes, bytes]] = [
        (b"content-type", b"application/json"),
        (b"retry-after", retry_after.encode()),
        (b"x-ratelimit-limit", str(limit).encode()),
        (b"x-ratelimit-remaining", b"0"),
        (b"x-ratelimit-reset", str(int(reset_at)).encode()),
    ]
    from typing import Any as _Any, cast as _cast

    await send(
        _cast(
            _Any,
            {
                "type": "http.response.start",
                "status": 429,
                "headers": headers,
            },
        )
    )
    await send(
        _cast(
            _Any,
            {
                "type": "http.response.body",
                "body": body,
            },
        )
    )
