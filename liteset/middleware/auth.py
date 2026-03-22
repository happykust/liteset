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
"""Authentication middleware for Liteset.

Supports cookie session (Flask itsdangerous), JWT Bearer (guest tokens),
and API key authentication. Redis user cache with TTL 60s reduces DB
pool pressure.
"""
from __future__ import annotations

import json  # noqa: TID251
import logging
from dataclasses import dataclass, field
from typing import Any

from litestar.connection import ASGIConnection
from litestar.exceptions import NotAuthorizedException
from litestar.middleware import AbstractAuthenticationMiddleware, AuthenticationResult

logger = logging.getLogger(__name__)

_USER_CACHE_TTL: int = 60  # seconds


@dataclass
class UnauthenticatedUser:
    """Placeholder for unauthenticated requests (public routes)."""
    is_authenticated: bool = False


@dataclass
class CachedUser:
    """User object reconstituted from Redis cache.

    Provides the same attribute interface as ORM User objects so that
    downstream code (SecurityManager, guards) works uniformly.
    """
    id: int
    username: str
    email: str = ""
    active: int = 1
    is_authenticated: bool = True
    roles: list[Any] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CachedUser | None:
        """Reconstitute from a Redis JSON dict. Returns None on invalid data."""
        user_id = data.get("id")
        username = data.get("username")
        if user_id is None or username is None:
            return None
        return cls(
            id=user_id,
            username=username,
            email=data.get("email", ""),
            active=data.get("active", 1),
            roles=[
                _CachedRole(id=r["id"], name=r["name"])
                for r in data.get("roles", [])
                if isinstance(r, dict) and "id" in r and "name" in r
            ],
        )


@dataclass
class _CachedRole:
    """Lightweight role for CachedUser — supports .id and .name access."""
    id: int
    name: str


def _get_secret_key(settings: Any) -> str:
    """Extract secret key string from settings."""
    secret_key = settings.secret_key
    if hasattr(secret_key, "get_secret_value"):
        secret_key = secret_key.get_secret_value()
    return secret_key


class LitesetAuthMiddleware(AbstractAuthenticationMiddleware):
    """Multi-strategy authentication middleware.

    Tries in order:
    1. Session cookie (Flask itsdangerous) -> user_id -> DB lookup
    2. JWT Bearer token (guest tokens for embedded dashboards)
    3. API key header (stub — Superset has no native support)

    Performance: Resolved users are cached in Redis (TTL 60s).
    On Redis failure, falls back to direct DB lookup.
    """

    async def authenticate_request(
        self, connection: ASGIConnection[Any, Any, Any, Any]
    ) -> AuthenticationResult:
        # 1. Try cookie session auth
        user = await self._authenticate_cookie(connection)
        if user:
            return AuthenticationResult(user=user, auth="cookie")

        # 2. Try JWT Bearer token
        user = await self._authenticate_jwt(connection)
        if user:
            return AuthenticationResult(user=user, auth="jwt")

        # 3. Try API key
        user = await self._authenticate_api_key(connection)
        if user:
            return AuthenticationResult(user=user, auth="api_key")

        raise NotAuthorizedException(detail="Not authenticated")

    async def _authenticate_cookie(
        self, connection: ASGIConnection[Any, Any, Any, Any]
    ) -> Any | None:
        """Authenticate via Flask session cookie (itsdangerous)."""
        settings = connection.app.state.settings
        cookie_name = getattr(settings, "session_cookie_name", "session")
        cookie = connection.cookies.get(cookie_name)
        if not cookie:
            return None

        # Cache FlaskSessionDecoder on app.state to avoid per-request creation
        decoder = self._get_or_create_decoder(connection)
        user_id = decoder.get_user_id(cookie)
        if user_id is None:
            return None

        # Try Redis cache first
        redis = getattr(connection.app.state, "redis", None)
        if redis is not None:
            try:
                cached = await self._get_cached_user(redis, f"auth:user:{user_id}")
                if cached is not None:
                    return cached
            except Exception:
                logger.debug("Redis cache miss/error for user %d", user_id)

        # Cache miss — resolve from DB
        user = await self._resolve_user_from_db(connection, user_id)
        if user is None:
            return None

        # Populate cache (best-effort)
        if redis is not None:
            try:
                await self._cache_user(redis, user)
            except Exception:
                logger.debug("Failed to cache user %d in Redis", user_id)

        # Set sentry user context
        self._set_sentry_user(user)

        return user

    @staticmethod
    def _get_or_create_decoder(
        connection: ASGIConnection[Any, Any, Any, Any],
    ) -> Any:
        """Cache FlaskSessionDecoder on app.state to avoid per-request creation."""
        from liteset.security.session_decoder import FlaskSessionDecoder

        decoder = getattr(connection.app.state, "_session_decoder", None)
        if decoder is None:
            secret_key = _get_secret_key(connection.app.state.settings)
            decoder = FlaskSessionDecoder(secret_key=secret_key)
            connection.app.state._session_decoder = decoder
        return decoder

    async def _authenticate_jwt(
        self, connection: ASGIConnection[Any, Any, Any, Any]
    ) -> Any | None:
        """Authenticate via JWT Bearer token (guest tokens only)."""
        # Check embedded_superset feature flag
        settings = connection.app.state.settings
        if not getattr(settings, "embedded_superset", False):
            return None

        auth_header = connection.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            return None
        token = auth_header[7:]
        if not token:
            return None
        return await self._resolve_guest_from_jwt(connection, token)

    async def _authenticate_api_key(
        self, connection: ASGIConnection[Any, Any, Any, Any]
    ) -> Any | None:
        """Authenticate via API key header.

        Stub — Superset does not have native API key support.
        Reserved for future extension.
        """
        api_key = connection.headers.get("x-api-key")
        if not api_key:
            return None
        logger.debug("API key auth attempted but not implemented")
        return None

    async def _resolve_user_from_db(
        self,
        connection: ASGIConnection[Any, Any, Any, Any],
        user_id: int,
    ) -> Any | None:
        """Resolve user from DB via a short-lived session."""
        from liteset.security.dao import AsyncSecurityDAO

        session_factory = connection.app.state.session_factory
        async with session_factory() as session:
            dao = AsyncSecurityDAO(session)
            user = await dao.get_user_by_id(user_id)
            if user is None:
                return None
            # FAB uses Integer column for active (0/1)
            active = getattr(user, "active", None)
            if active is not None and not active:
                return None
            return user
        return None

    async def _resolve_guest_from_jwt(
        self,
        connection: ASGIConnection[Any, Any, Any, Any],
        token: str,
    ) -> Any | None:
        """Resolve guest user from JWT token."""
        from liteset.security.guest import GuestUser, parse_guest_token

        settings = connection.app.state.settings
        # Use dedicated guest token secret if configured, otherwise fall back
        guest_secret = getattr(settings, "guest_token_jwt_secret", "")
        if guest_secret:
            secret_key = guest_secret
        else:
            secret_key = _get_secret_key(settings)

        algo = getattr(settings, "guest_token_jwt_algo", "HS256")
        payload = parse_guest_token(token, secret_key, algorithm=algo)
        if payload is None:
            return None
        return GuestUser.from_token_payload(payload)

    async def _get_cached_user(
        self, redis: Any, cache_key: str
    ) -> CachedUser | None:
        """Try to load user from Redis cache.

        Returns a CachedUser dataclass with the same attribute interface
        as ORM User objects, including roles and active status.
        """
        data = await redis.get(cache_key)
        if data is None:
            return None
        try:
            parsed = json.loads(data)
        except (json.JSONDecodeError, TypeError):
            return None
        user = CachedUser.from_dict(parsed)
        if user is None:
            return None
        # Reject inactive users from cache
        if not user.active:
            return None
        return user

    async def _cache_user(self, redis: Any, user: Any) -> None:
        """Cache resolved user in Redis (TTL 60s).

        Stores roles and active status so that cached users retain
        their permission context and deactivated users are rejected.
        """
        roles = [
            {"id": r.id, "name": r.name}
            for r in getattr(user, "roles", [])
            if hasattr(r, "id") and hasattr(r, "name")
        ]
        user_data = json.dumps({
            "id": user.id,
            "username": user.username,
            "email": getattr(user, "email", ""),
            "active": getattr(user, "active", 1),
            "is_authenticated": True,
            "roles": roles,
        })
        await redis.set(
            f"auth:user:{user.id}",
            user_data,
            ex=_USER_CACHE_TTL,
        )

    @staticmethod
    def _set_sentry_user(user: Any) -> None:
        """Set Sentry user context after successful auth."""
        try:
            import sentry_sdk
            sentry_sdk.set_user({
                "id": str(getattr(user, "id", "")),
                "email": getattr(user, "email", ""),
                "username": getattr(user, "username", ""),
            })
        except ImportError:
            pass
