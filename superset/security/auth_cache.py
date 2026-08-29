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
"""Invalidation helpers for the Redis-backed auth cache.

``AuthenticationMiddleware`` caches the resolved user — including
``active``, ``roles`` and the flattened ``permissions`` set — in Redis for
``_USER_CACHE_TTL`` seconds.  Without invalidation, deactivating a user or
revoking a role would keep working for the rest of the TTL, which is not
acceptable for an authorization decision.

Two invalidation channels are provided:

``invalidate_user``
    Drops one user's cache entry.  Used whenever a specific user row
    changes (deactivation, password reset, role assignment, deletion).

``bump_auth_epoch``
    Increments a global counter.  Cached entries record the epoch they
    were minted under and are rejected once it moves, which invalidates
    *every* cached user at once.  Used for changes that affect an
    unbounded set of users — editing a role's permissions, changing a
    group's roles, or granting/revoking a permission-view.

Both are best-effort: a Redis outage must degrade to "no cache", never to
a failed request, so callers swallow errors and the middleware simply
falls back to the database.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Global cache generation counter.  Bumped on role/permission mutations.
AUTH_EPOCH_KEY = "auth:cache_epoch"

#: Prefix for per-user cache entries, keyed by id, username and email.
USER_CACHE_PREFIX = "auth:user:"


def user_cache_keys(
    user_id: Any,
    username: str | None = None,
    email: str | None = None,
) -> list[str]:
    """Return every Redis key a user may be cached under."""
    keys = [f"{USER_CACHE_PREFIX}{user_id}"]
    if username:
        keys.append(f"{USER_CACHE_PREFIX}{username}")
    if email:
        keys.append(f"{USER_CACHE_PREFIX}{email}")
    return keys


async def read_auth_epoch(redis: Any) -> str:
    """Return the current cache epoch as a string (``""`` when unset)."""
    if redis is None:
        return ""
    try:
        value = await redis.get(AUTH_EPOCH_KEY)
    except Exception:  # noqa: BLE001
        logger.debug("Failed to read auth cache epoch", exc_info=True)
        return ""
    return as_cache_str(value)


async def bump_auth_epoch(redis: Any) -> None:
    """Invalidate every cached user by moving the global epoch forward."""
    if redis is None:
        return
    try:
        await redis.incr(AUTH_EPOCH_KEY)
    except Exception:  # noqa: BLE001
        logger.warning("Failed to bump auth cache epoch", exc_info=True)


async def invalidate_user(
    redis: Any,
    user_id: Any,
    username: str | None = None,
    email: str | None = None,
) -> None:
    """Drop the cache entry for a single user."""
    if redis is None or user_id is None:
        return
    try:
        await redis.delete(*user_cache_keys(user_id, username, email))
    except Exception:  # noqa: BLE001
        logger.warning("Failed to invalidate auth cache for user %s", user_id)


def as_cache_str(value: Any) -> str:
    """Normalise a Redis reply to ``str`` (clients may or may not decode)."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    return str(value)


def sign_payload(payload: str, secret: str) -> str:
    """Return the HMAC tag for a cached authorization payload."""
    return hmac.new(
        secret.encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()


def verify_payload(payload: str, signature: str, secret: str) -> bool:
    """Constant-time check of a cached payload's HMAC tag.

    The cache holds the user's roles and flattened permission set, which the
    middleware trusts for authorization.  Without a signature, write access to
    Redis would be enough to grant arbitrary permissions — a materially lower
    bar than write access to the metadata database, and one that upstream
    never exposed (its session is a signed cookie and permissions always come
    from the database).
    """
    if not signature:
        return False
    return hmac.compare_digest(signature, sign_payload(payload, secret))


def sign_keyed_payload(cache_key: str, payload: str, secret: str) -> str:
    """Sign a cache payload together with the Redis key it is stored under.

    ``sign_payload`` alone authenticates the *content* but not its
    location: an attacker with Redis write access does not need to forge
    anything to escalate privileges, they can copy another user's
    legitimately signed envelope onto their own key (e.g.
    ``COPY auth:user:1 auth:user:42``) and the bare-payload signature still
    verifies.  Folding *cache_key* into the signed material makes a
    transplanted entry fail verification under its new key.
    """
    return sign_payload(f"{cache_key}|{payload}", secret)


def verify_keyed_payload(
    cache_key: str,
    payload: str,
    signature: str,
    secret: str,
) -> bool:
    """Constant-time check of a cache entry's HMAC tag, bound to both its
    payload and the Redis key it was read from.  See :func:`sign_keyed_payload`.
    """
    if not signature:
        return False
    return hmac.compare_digest(
        signature, sign_keyed_payload(cache_key, payload, secret)
    )


async def _client_from_settings(settings: Any) -> Any | None:
    """Open a short-lived Redis client from settings, or ``None``.

    Some invalidation points have no request and therefore no
    ``app.state.redis`` — the ``superset init`` CLI, and role synchronisation
    during an LDAP/OAuth login. Skipping invalidation there would serve a
    revoked grant (or a stale denial) for the rest of the cache TTL, so those
    callers resolve a client on demand instead. Only reached on an actual
    permission change, never on a hot path.
    """
    url = getattr(settings, "redis_url", None)
    if not url:
        return None
    try:
        from redis.asyncio import Redis

        return Redis.from_url(url, decode_responses=True)
    except Exception:  # noqa: BLE001
        logger.debug("Could not open Redis for auth-cache invalidation")
        return None


async def bump_auth_epoch_for_settings(settings: Any) -> None:
    """Invalidate every cached authorization payload, without a live client."""
    client = await _client_from_settings(settings)
    if client is None:
        return
    try:
        await bump_auth_epoch(client)
    finally:
        await client.aclose()


async def invalidate_user_for_settings(
    settings: Any,
    user_id: Any,
    username: str | None = None,
    email: str | None = None,
) -> None:
    """Drop one user's cache entry, without a live client."""
    client = await _client_from_settings(settings)
    if client is None:
        return
    try:
        await invalidate_user(client, user_id, username, email)
    finally:
        await client.aclose()
