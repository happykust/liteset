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

ASGI port of the FAB / flask-limiter rate limiting wired up in
``flask_appbuilder.security.manager.BaseSecurityManager``:

* ``RATELIMIT_ENABLED`` is the master kill switch (flask-limiter disables
  *all* limits — application and per-route — when this is False).  Upstream
  default is ``SUPERSET_ENV == "production"``.
* ``RATELIMIT_APPLICATION`` (default ``"50 per second"``) is the
  application-wide limit applied to *every* request, keyed by client IP
  (flask-limiter ``key_func=get_remote_address``).
* ``AUTH_RATE_LIMITED`` (default True) + ``AUTH_RATE_LIMIT``
  (default ``"5 per second"``) limit POST requests to the login endpoint —
  brute-force protection — mirroring
  ``limiter.limit(auth_rate_limit, methods=["POST"])(auth_view.blueprint)``.

Both limits are keyed by the client IP (``get_remote_address``), resolved
from ``scope["client"]`` *after* :class:`ProxyFixMiddleware` has applied the
trusted ``X-Forwarded-For``.  A login POST is subject to BOTH limits (just
like the flask-limiter application limit + the blueprint ``@limit``).

Enforcement uses a Redis sliding-window counter.  When Redis is unavailable
the middleware fails open (requests pass) so the application stays available.
"""

from __future__ import annotations

import logging
import re
import time
import uuid

from litestar.middleware.base import ASGIMiddleware
from litestar.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

# Paths excluded from the application rate limit (health probes).
_EXCLUDED_PATHS: frozenset[str] = frozenset(
    {
        "/api/v1/health",
        "/health",
        "/healthcheck",
        "/ping",
        "/healthz",
    }
)

# Login endpoint paths subject to the AUTH_RATE_LIMIT (POST only).  Mirrors
# the FAB auth_view blueprint which exposes ``/login/`` (see
# superset.controllers.auth.AuthController).
_LOGIN_PATHS: frozenset[str] = frozenset({"/login", "/login/"})

_REDIS_KEY_PREFIX = "ratelimit:"

# Map a flask-limiter / ``limits`` granularity word to its length in seconds.
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

# ``"50 per second"`` / ``"10 per 20 second"`` / ``"100 per hour"``
_PER_RE = re.compile(
    r"^\s*(\d+)\s*per\s*(\d+)?\s*([a-zA-Z]+)\s*$",
)
# ``"50/second"`` / ``"5/s"`` / ``"100/hour"``
_SLASH_RE = re.compile(r"^\s*(\d+)\s*/\s*(\d+)?\s*([a-zA-Z]+)\s*$")


def parse_rate_limit(spec: str | None) -> tuple[int, int] | None:
    """Parse a flask-limiter rate-limit string into ``(count, window_seconds)``.

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
      passes through unconditionally (mirrors flask-limiter ``RATELIMIT_ENABLED``).
    * ``ratelimit_application`` (str) — application-wide limit (all requests).
    * ``auth_rate_limited`` (bool) + ``auth_rate_limit`` (str) — login POST limit.

    When Redis is unavailable the middleware fails open.
    """

    async def handle(  # noqa: C901
        self, scope: Scope, receive: Receive, send: Send, next_app: ASGIApp
    ) -> None:
        if scope["type"] != "http":
            await next_app(scope, receive, send)
            return

        settings = getattr(getattr(scope.get("app"), "state", None), "settings", None)

        # Master kill switch — flask-limiter disables every limit (application
        # and per-route) when RATELIMIT_ENABLED is False.
        if settings is None or not getattr(settings, "ratelimit_enabled", False):
            await next_app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        method: str = str(scope.get("method", "GET")).upper()

        # Build the list of limits that apply to this request.
        # Each entry: (redis_namespace, count, window_seconds).
        checks: list[tuple[str, int, int]] = []

        # Application limit — every request except health probes.
        if path not in _EXCLUDED_PATHS:
            app_limit = parse_rate_limit(
                getattr(settings, "ratelimit_application", None)
            )
            if app_limit is not None:
                checks.append(("app", app_limit[0], app_limit[1]))

        # Auth limit — POST to the login endpoint, when AUTH_RATE_LIMITED.
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
        # No Redis — fail open.
        if redis is None:
            await next_app(scope, receive, send)
            return

        # Identity = client IP (flask-limiter ``get_remote_address``), resolved
        # after ProxyFixMiddleware has applied the trusted X-Forwarded-For.
        identity = _resolve_identity(scope)

        rl_headers: list[tuple[bytes, bytes]] | None = None
        now = time.time()
        for namespace, max_requests, window in checks:
            redis_key = f"{_REDIS_KEY_PREFIX}{namespace}:{identity}"
            try:
                remaining, reset_at = await _sliding_window_check(
                    redis, redis_key, max_requests, window, now
                )
            except Exception:
                # Redis error — fail open for this check.
                logger.debug("Rate limit Redis error, bypassing", exc_info=True)
                continue

            if remaining < 0:
                retry_after = str(int(reset_at - time.time()) + 1)
                await _send_429(send, max_requests, reset_at, retry_after)
                return

            # Surface headers from the tightest (lowest-remaining) limit.
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
    """Resolve the rate-limit key from the client IP.

    Mirrors flask-limiter's default ``key_func=get_remote_address`` which
    returns ``request.remote_addr`` — the client IP (already corrected by
    ProxyFixMiddleware from a trusted ``X-Forwarded-For`` when configured).
    """
    client = scope.get("client")
    if client:
        return f"ip:{client[0]}"
    return "ip:unknown"


async def _sliding_window_check(
    redis: object,
    key: str,
    max_requests: int,
    window: int,
    now: float,
) -> tuple[int, float]:
    """Execute a sliding-window rate-limit check using Redis sorted sets.

    Returns ``(remaining, reset_timestamp)``. ``remaining`` is negative
    when the limit has been exceeded.
    """
    window_start = now - window
    reset_at = now + window

    # Use a transactional pipeline (MULTI/EXEC) for atomicity
    pipe = redis.pipeline(transaction=True)  # type: ignore[attr-defined]
    # Remove entries outside the current window
    pipe.zremrangebyscore(key, 0, window_start)
    # Add current request with a unique member to avoid score collisions
    member = f"{now}:{uuid.uuid4().hex[:8]}"
    pipe.zadd(key, {member: now})
    # Count requests in current window
    pipe.zcard(key)
    # Set TTL so keys auto-expire
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
    """Send a 429 Too Many Requests response."""
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
