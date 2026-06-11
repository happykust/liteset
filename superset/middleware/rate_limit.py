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

Uses a sliding-window counter backed by Redis to enforce per-user
(or per-IP for unauthenticated requests) rate limits. Adds standard
rate-limit headers and returns 429 Too Many Requests when exceeded.
"""

from __future__ import annotations

import logging
import time
import uuid

from litestar.middleware.base import ASGIMiddleware
from litestar.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

# Paths excluded from rate limiting
_EXCLUDED_PATHS: frozenset[str] = frozenset(
    {
        "/api/v1/health",
        "/health",
        "/healthcheck",
        "/ping",
        "/healthz",
    }
)

_DEFAULT_RATE_LIMIT = 1000  # requests (relaxed for development; tighten in production)
_DEFAULT_RATE_LIMIT_WINDOW = 60  # seconds
_REDIS_KEY_PREFIX = "ratelimit:"


class RateLimitMiddleware(ASGIMiddleware):
    """Sliding-window rate limiter using Redis.

    Configuration is read from ``settings`` on ``app.state``:
    - ``rate_limit_per_minute`` (int): max requests per window (default 100)
    - ``rate_limit_window_seconds`` (int): window size in seconds (default 60)

    When Redis is unavailable the middleware is bypassed (fail-open)
    so that the application remains available.
    """

    async def handle(
        self, scope: Scope, receive: Receive, send: Send, next_app: ASGIApp
    ) -> None:
        if scope["type"] != "http":
            await next_app(scope, receive, send)
            return

        # Skip excluded health-check paths
        path: str = scope.get("path", "")
        if path in _EXCLUDED_PATHS:
            await next_app(scope, receive, send)
            return

        app = scope.get("app")
        redis = getattr(getattr(app, "state", None), "redis", None)

        # No Redis — fail open
        if redis is None:
            await next_app(scope, receive, send)
            return

        # Resolve settings
        settings = getattr(getattr(app, "state", None), "settings", None)
        max_requests = (
            getattr(settings, "rate_limit_per_minute", _DEFAULT_RATE_LIMIT)
            if settings
            else _DEFAULT_RATE_LIMIT
        )
        window = (
            getattr(settings, "rate_limit_window_seconds", _DEFAULT_RATE_LIMIT_WINDOW)
            if settings
            else _DEFAULT_RATE_LIMIT_WINDOW
        )

        # Determine client identity: user_id if authenticated, else IP
        identity = _resolve_identity(scope)
        redis_key = f"{_REDIS_KEY_PREFIX}{identity}"

        try:
            now = time.time()
            remaining, reset_at = await _sliding_window_check(
                redis, redis_key, max_requests, window, now
            )
        except Exception:
            # Redis error — fail open
            logger.debug("Rate limit Redis error, bypassing", exc_info=True)
            await next_app(scope, receive, send)
            return

        # ``remaining < 0`` — the current request is already counted by
        # ``_sliding_window_check`` (zadd before zcard), so ``remaining == 0``
        # IS the exact ``max_requests``-th request and must be allowed;
        # ``<= 0`` capped the effective limit at ``max_requests - 1``.
        if remaining < 0:
            # 429 Too Many Requests
            retry_after = str(int(reset_at - time.time()) + 1)
            await _send_429(send, max_requests, reset_at, retry_after)
            return

        # Attach rate-limit headers to the response
        rl_headers: list[tuple[bytes, bytes]] = [
            (b"x-ratelimit-limit", str(max_requests).encode()),
            (b"x-ratelimit-remaining", str(max(remaining, 0)).encode()),
            (b"x-ratelimit-reset", str(int(reset_at)).encode()),
        ]

        async def send_with_rl_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                existing: list[tuple[bytes, bytes]] = list(message.get("headers", []))
                existing.extend(rl_headers)
                message = {**message, "headers": existing}
            await send(message)

        await next_app(scope, receive, send_with_rl_headers)


def _resolve_identity(scope: Scope) -> str:
    """Resolve a rate-limit identity key from the ASGI scope.

    Prefers ``user.id`` (set by auth middleware) over client IP.
    """
    user = scope.get("user")
    if user is not None:
        user_id = getattr(user, "id", None)
        if user_id is not None:
            return f"user:{user_id}"

    # Fall back to client IP
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
