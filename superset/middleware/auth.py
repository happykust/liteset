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
"""Authentication middleware for Superset.

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
from litestar.middleware import AbstractAuthenticationMiddleware, AuthenticationResult

logger = logging.getLogger(__name__)

_USER_CACHE_TTL: int = 60  # seconds
_PUBLIC_ROLE_CACHE_KEY: str = "auth:public_role_perms"
_PUBLIC_ROLE_CACHE_TTL: int = 300  # 5 minutes


@dataclass
class UnauthenticatedUser:
    """Placeholder for unauthenticated requests (public routes).

    When a Public role is configured (``auth_role_public``), anonymous
    users receive that role's permissions so that RBAC guards can
    allow access to public endpoints without requiring login.

    Implements the full ``UserProtocol`` interface so that Litestar's
    msgspec signature validation (``isinstance`` check) passes when
    ``current_user: UserProtocol`` is a handler parameter.
    """

    id: int = 0
    username: str = ""
    is_authenticated: bool = False
    roles: list[Any] = field(default_factory=list)
    permissions: set[tuple[str, str]] = field(default_factory=set)


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
    first_name: str = ""
    last_name: str = ""
    login_count: int = 0
    created_on: str = ""
    is_authenticated: bool = True
    roles: list[Any] = field(default_factory=list)
    permissions: set[tuple[str, str]] = field(default_factory=set)

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
            first_name=data.get("first_name", ""),
            last_name=data.get("last_name", ""),
            login_count=data.get("login_count", 0),
            created_on=data.get("created_on", ""),
            roles=[
                _CachedRole(id=r["id"], name=r["name"])
                for r in data.get("roles", [])
                if isinstance(r, dict) and "id" in r and "name" in r
            ],
            permissions={
                (p[0], p[1])
                for p in data.get("permissions", [])
                if isinstance(p, (list, tuple)) and len(p) == 2
            },
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


def _safe_int(value: Any) -> int | None:
    """Safely convert a value to int, returning None on failure."""
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


class SupersetAuthMiddleware(AbstractAuthenticationMiddleware):
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

        # 2. Try guest token header (X-GuestToken or configured header name).
        #    Must be checked BEFORE the generic JWT Bearer path so that
        #    embedded dashboard requests are recognised correctly.
        #    Also checks form data ``guest_token`` field for sendBeacon API.
        user = await self._authenticate_guest_token(connection)
        if user:
            return AuthenticationResult(user=user, auth="guest_token")

        # 3. Try JWT Bearer token (API access tokens)
        user = await self._authenticate_jwt(connection)
        if user:
            return AuthenticationResult(user=user, auth="jwt")

        # 4. Try API key
        user = await self._authenticate_api_key(connection)
        if user:
            return AuthenticationResult(user=user, auth="api_key")

        # Return anonymous user with Public role permissions (if configured).
        # This allows public routes (health checks, SPA assets, public dashboards)
        # to work without authentication.
        anon = await self._build_anonymous_user(connection)
        return AuthenticationResult(user=anon, auth=None)

    async def _build_anonymous_user(
        self, connection: ASGIConnection[Any, Any, Any, Any]
    ) -> UnauthenticatedUser:
        """Build an anonymous user, optionally with Public role permissions.

        When ``auth_role_public`` is set in config, the anonymous user
        receives that role's permissions so RBAC guards can allow access
        to public endpoints without requiring login.
        """
        settings = connection.app.state.settings
        role_name = getattr(settings, "auth_role_public", "")
        if not role_name:
            return UnauthenticatedUser()

        permissions: set[tuple[str, str]] = set()
        redis = getattr(connection.app.state, "redis", None)

        # Try Redis cache first
        if redis is not None:
            try:
                cached = await redis.get(_PUBLIC_ROLE_CACHE_KEY)
                if cached is not None:
                    perm_list = json.loads(cached)
                    permissions = {
                        (p[0], p[1])
                        for p in perm_list
                        if isinstance(p, (list, tuple)) and len(p) == 2
                    }
                    roles = [_CachedRole(id=0, name=role_name)] if permissions else []
                    return UnauthenticatedUser(roles=roles, permissions=permissions)
            except Exception:
                logger.debug("Redis error reading public role cache")

        # Cache miss -- resolve from DB
        permissions = await self._resolve_public_permissions(connection, role_name)

        # Populate cache (best-effort)
        if redis is not None:
            try:
                await redis.set(
                    _PUBLIC_ROLE_CACHE_KEY,
                    json.dumps(sorted(permissions)),
                    ex=_PUBLIC_ROLE_CACHE_TTL,
                )
            except Exception:
                logger.debug("Failed to cache public role permissions in Redis")

        roles = [_CachedRole(id=0, name=role_name)] if permissions else []
        return UnauthenticatedUser(roles=roles, permissions=permissions)

    @staticmethod
    async def _resolve_public_permissions(
        connection: ASGIConnection[Any, Any, Any, Any],
        role_name: str,
    ) -> set[tuple[str, str]]:
        """Load permissions for the named Public role from DB."""
        from superset.security.dao import AsyncSecurityDAO

        session_factory = connection.app.state.session_factory
        try:
            async with session_factory() as session:
                dao = AsyncSecurityDAO(session)
                return await dao.get_permissions_for_role_name(role_name)
        except Exception:
            logger.exception("Failed to resolve public role permissions")
            return set()

    async def _authenticate_cookie(  # noqa: C901
        self, connection: ASGIConnection[Any, Any, Any, Any]
    ) -> Any | None:
        """Authenticate via session cookie (JWT or itsdangerous)."""
        settings = connection.app.state.settings
        cookie_name = getattr(settings, "session_cookie_name", "session")
        cookie = connection.cookies.get(cookie_name)
        if not cookie:
            return None

        # Try JWT decode first (Liteset auth controller)
        import jwt as pyjwt

        secret_key = _get_secret_key(settings)
        user_id: int | None = None
        token_iat: int | None = None
        try:
            payload = pyjwt.decode(
                cookie,
                secret_key,
                algorithms=["HS256"],
            )
            user_id = payload.get("user_id")
            # Extract issued-at timestamp for blacklist check
            iat_raw = payload.get("iat")
            if iat_raw is not None:
                token_iat = int(iat_raw)
        except Exception:  # noqa: S110
            pass

        # Fallback: itsdangerous (Flask legacy)
        if user_id is None:
            decoder = self._get_or_create_decoder(connection)
            user_id = decoder.get_user_id(cookie)

        if user_id is None:
            return None

        # Check token blacklist: if the user logged out after this token
        # was issued, reject it.  The logout handler writes
        # ``auth:token_blacklist:{user_id}`` with the logout timestamp.
        redis = getattr(connection.app.state, "redis", None)
        if redis is not None and token_iat is not None:
            if await self._is_token_blacklisted(redis, user_id, token_iat):
                logger.debug(
                    "Rejecting blacklisted cookie token for user %d (iat=%d)",
                    user_id,
                    token_iat,
                )
                return None

        # Try Redis cache first
        if redis is not None:
            try:
                cached = await self._get_cached_user(redis, f"auth:user:{user_id}")
                if cached is not None:
                    self._set_sentry_user(cached)
                    return cached
            except Exception:
                logger.debug("Redis cache miss/error for user %d", user_id)

        # Cache miss — resolve from DB
        try:
            user = await self._resolve_user_from_db(
                connection,
                user_id,
            )
        except Exception:
            logger.exception(
                "Failed to resolve user %d from DB",
                user_id,
            )
            return None
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
        from superset.security.session_decoder import FlaskSessionDecoder

        decoder = getattr(connection.app.state, "_session_decoder", None)
        if decoder is None:
            settings = connection.app.state.settings
            secret_key = _get_secret_key(settings)
            max_age = getattr(settings, "session_max_age", None)
            decoder = FlaskSessionDecoder(secret_key=secret_key, max_age=max_age)
            connection.app.state._session_decoder = decoder
        return decoder

    async def _authenticate_guest_token(
        self, connection: ASGIConnection[Any, Any, Any, Any]
    ) -> Any | None:
        """Authenticate via the guest token header or form field.

        Mirrors the original ``get_guest_user_from_request`` in
        ``SupersetSecurityManager``:
        1. Reads the header named by ``GUEST_TOKEN_HEADER_NAME``
           (default ``X-GuestToken``).
        2. Falls back to ``guest_token`` in POST form body (used by the
           browser ``sendBeacon`` API which cannot set custom headers).
           Mirrors the original ``req.form.get("guest_token")``.
        3. Only active when the ``EMBEDDED_SUPERSET`` feature flag or
           ``embedded_superset`` setting is enabled.
        """
        settings = connection.app.state.settings

        # Check feature flag or settings
        feature_flags = getattr(settings, "feature_flags", {})
        embedded_enabled = getattr(
            settings, "embedded_superset", False
        ) or feature_flags.get("EMBEDDED_SUPERSET", False)
        if not embedded_enabled:
            return None

        # Read the configurable header name (default: X-GuestToken)
        header_name = getattr(settings, "guest_token_header_name", "X-GuestToken")
        raw_token = connection.headers.get(header_name.lower(), "")

        # Fallback: POST form data ``guest_token`` (sendBeacon API).
        # The original uses ``req.form.get("guest_token")`` which reads
        # from URL-encoded POST body.  In Litestar middleware, we read
        # the raw body and parse form data manually.
        if not raw_token:
            raw_token = await self._read_guest_token_from_body(connection)

        if not raw_token:
            return None

        return await self._resolve_guest_from_jwt(connection, raw_token)

    async def _authenticate_jwt(
        self, connection: ASGIConnection[Any, Any, Any, Any]
    ) -> Any | None:
        """Authenticate via JWT Bearer token (API access tokens only).

        Guest token authentication is handled separately by
        ``_authenticate_guest_token`` which reads the dedicated guest
        token header. This method handles ``Authorization: Bearer``
        API access tokens (type=access).
        """
        auth_header = connection.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            return None
        token = auth_header[7:]
        if not token:
            return None

        # Try API access token
        return await self._resolve_user_from_access_token(connection, token)

    async def _resolve_user_from_access_token(
        self,
        connection: ASGIConnection[Any, Any, Any, Any],
        token: str,
    ) -> CachedUser | None:
        """Resolve user from an API access token (type=access).

        Decodes the JWT, verifies type=access, then looks up the user
        from Redis cache or DB.  Rejects tokens whose ``iat`` predates
        a logout blacklist entry in Redis.
        """
        import jwt as pyjwt

        settings = connection.app.state.settings
        secret_key = _get_secret_key(settings)

        try:
            payload = pyjwt.decode(token, secret_key, algorithms=["HS256"])
        except pyjwt.PyJWTError:
            return None

        if payload.get("type") != "access":
            return None

        user_id = _safe_int(payload.get("sub"))
        if user_id is None:
            return None

        token_iat = _safe_int(payload.get("iat"))

        # Check token blacklist: if the user logged out after this token
        # was issued, reject it.
        redis = getattr(connection.app.state, "redis", None)
        if await self._check_blacklist(redis, user_id, token_iat):
            return None

        # Try Redis cache first
        if redis is not None:
            try:
                cached = await self._get_cached_user(redis, f"auth:user:{user_id}")
                if cached is not None:
                    return cached
            except Exception:
                logger.debug("Redis cache miss/error for access token user %d", user_id)

        # Resolve from DB
        return await self._resolve_user_from_db(connection, user_id)

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
    ) -> CachedUser | None:
        """Resolve user from DB via a short-lived session.

        Returns a CachedUser with pre-resolved permissions so that RBAC
        guards can check ``user.permissions`` without extra DB queries.

        NOTE: FAB supports ``builtin_roles`` (config key ``FAB_ROLES``)
        which are regex-based permission sets that bypass DB queries.
        Liteset does not currently have ``FAB_ROLES`` in its config, so
        builtin role handling is not implemented here.  If ``FAB_ROLES``
        support is needed in the future, the builtin role permissions
        should be merged into the returned permission set here, matching
        FAB's ``get_user_roles_permissions`` logic.
        """
        from superset.security.dao import AsyncSecurityDAO

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
            permissions = await dao.get_all_permissions_for_user_with_groups(user_id)
            # Serialize created_on to ISO string for cache/bootstrap compat
            created_on_raw = getattr(user, "created_on", None)
            created_on_str = ""
            if created_on_raw is not None:
                if hasattr(created_on_raw, "isoformat"):
                    created_on_str = created_on_raw.isoformat()
                else:
                    created_on_str = str(created_on_raw)
            return CachedUser(
                id=user.id,
                username=user.username,
                email=getattr(user, "email", ""),
                active=getattr(user, "active", 1),
                first_name=getattr(user, "first_name", ""),
                last_name=getattr(user, "last_name", ""),
                login_count=getattr(user, "login_count", 0) or 0,
                created_on=created_on_str,
                roles=[
                    _CachedRole(id=r.id, name=r.name)
                    for r in getattr(user, "roles", [])
                ],
                permissions=permissions,
            )

    @staticmethod
    async def _read_guest_token_from_body(
        connection: ASGIConnection[Any, Any, Any, Any],
    ) -> str:
        """Read ``guest_token`` from POST form body.

        Mirrors the original ``req.form.get("guest_token")``.  Supports
        both ``application/x-www-form-urlencoded`` and ``multipart/form-data``
        content types (url-encoded only; multipart is best-effort).
        Returns empty string if not found or not applicable.
        """
        from urllib.parse import parse_qs

        # Only attempt for POST/PUT/PATCH methods that may have a body
        method = str(connection.scope.get("method", "GET")).upper()
        if method not in {"POST", "PUT", "PATCH"}:
            return ""

        content_type = connection.headers.get("content-type", "")
        if "application/x-www-form-urlencoded" not in content_type:
            return ""

        try:
            # ASGIConnection doesn't expose .body(); read via scope
            body_bytes: bytes = b""
            if hasattr(connection, "body"):
                body_bytes = await connection.body()
            if not body_bytes:
                return ""
            parsed = parse_qs(body_bytes.decode("utf-8", errors="replace"))
            values = parsed.get("guest_token", [])
            return values[0] if values else ""
        except Exception:
            logger.debug("Failed to read guest_token from request body")
            return ""

    async def _resolve_guest_from_jwt(
        self,
        connection: ASGIConnection[Any, Any, Any, Any],
        token: str,
    ) -> Any | None:
        """Resolve guest user from JWT token.

        Mirrors the original ``get_guest_user_from_request`` +
        ``parse_jwt_guest_token`` logic: decodes the JWT with the
        configured secret, algorithm, and audience claim.
        """
        from superset.security.guest import GuestUser, parse_guest_token

        settings = connection.app.state.settings
        # Use dedicated guest token secret if configured, otherwise fall back
        guest_secret = getattr(settings, "guest_token_jwt_secret", "")
        if guest_secret:
            secret_key = guest_secret
        else:
            secret_key = _get_secret_key(settings)

        algo = getattr(settings, "guest_token_jwt_algo", "HS256")

        # Resolve audience: GUEST_TOKEN_JWT_AUDIENCE config or request URL.
        # Matches the original _get_guest_token_jwt_audience() logic.
        audience_setting = getattr(settings, "guest_token_jwt_audience", None)
        if audience_setting is not None:
            audience = (
                audience_setting() if callable(audience_setting) else audience_setting
            )
        else:
            # Fallback: derive from the request host (matches original get_url_host())
            host = connection.headers.get("host", "")
            scheme = connection.scope.get("scheme", "http")
            audience = f"{scheme}://{host}" if host else ""
        audience = str(audience) if audience else ""

        payload = parse_guest_token(
            token, secret_key, algorithm=algo, audience=audience
        )
        if payload is None:
            return None

        # Validate required claims (mirrors original get_guest_user_from_request)
        if payload.get("user") is None:
            logger.warning("Guest token does not contain a user claim")
            return None
        if payload.get("resources") is None:
            logger.warning("Guest token does not contain a resources claim")
            return None
        if payload.get("rls_rules") is None:
            logger.warning("Guest token does not contain an rls_rules claim")
            return None

        guest_user = GuestUser.from_token_payload(payload)

        # Load the Guest role from DB and merge its permissions.
        # Mirrors the original ``get_guest_user_from_token``:
        #   roles=[self.find_role(get_conf()["GUEST_ROLE_NAME"])]
        guest_role_name = getattr(settings, "guest_role_name", "Guest")
        try:
            await self._load_guest_role_permissions(
                connection, guest_user, guest_role_name
            )
        except Exception:
            logger.warning(
                "Failed to load Guest role '%s' from DB",
                guest_role_name,
                exc_info=True,
            )

        return guest_user

    @staticmethod
    async def _load_guest_role_permissions(
        connection: ASGIConnection[Any, Any, Any, Any],
        guest_user: Any,
        role_name: str,
    ) -> None:
        """Load Guest role from DB and merge its permissions into the GuestUser.

        Mirrors the original ``get_guest_user_from_token`` which sets:
            roles=[self.find_role(get_conf()["GUEST_ROLE_NAME"])]
        The Guest role contains permissions like ``can_read`` on ``Dashboard``,
        ``Chart``, etc. These are merged into the guest user's permission set.
        """
        from superset.security.dao import AsyncSecurityDAO

        session_factory = connection.app.state.session_factory
        async with session_factory() as session:
            dao = AsyncSecurityDAO(session)
            role = await dao.get_role_by_name(role_name)
            if role is None:
                logger.warning("Guest role '%s' not found in database", role_name)
                return

            # Set the role on the guest user
            guest_user.roles = [_CachedRole(id=role.id, name=role.name)]

            # Load and merge permissions from the Guest role
            role_perms = await dao.get_permissions_for_role_name(role_name)
            # Merge DB role permissions with the derived resource permissions
            guest_user.permissions = guest_user.permissions | role_perms

    @staticmethod
    async def _is_token_blacklisted(
        redis: Any,
        user_id: int,
        token_iat: int,
    ) -> bool:
        """Check if a token is blacklisted due to logout.

        On logout the auth controller writes
        ``auth:token_blacklist:{user_id}`` with the UNIX timestamp of
        the logout event.  Any token whose ``iat`` (issued-at) is
        earlier than that timestamp is considered revoked.

        Returns True if the token should be rejected, False otherwise.
        On Redis errors returns False (fail-open) to avoid blocking
        authentication when Redis is temporarily unavailable.
        """
        try:
            blacklist_ts_raw = await redis.get(f"auth:token_blacklist:{user_id}")
            if blacklist_ts_raw is None:
                return False
            blacklist_ts = int(blacklist_ts_raw)
            return token_iat <= blacklist_ts
        except Exception:
            logger.debug(
                "Redis error checking token blacklist for user %d",
                user_id,
            )
            return False

    async def _check_blacklist(
        self,
        redis: Any | None,
        user_id: int,
        token_iat: int | None,
    ) -> bool:
        """Convenience wrapper: check blacklist only when Redis and iat exist.

        Returns True if the token should be rejected. Logs the rejection
        and returns False when preconditions are not met (no Redis, no iat).
        """
        if redis is None or token_iat is None:
            return False
        blacklisted = await self._is_token_blacklisted(redis, user_id, token_iat)
        if blacklisted:
            logger.debug(
                "Rejecting blacklisted token for user %d (iat=%d)",
                user_id,
                token_iat,
            )
        return blacklisted

    async def _get_cached_user(self, redis: Any, cache_key: str) -> CachedUser | None:
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
        user_data = json.dumps(
            {
                "id": user.id,
                "username": user.username,
                "email": getattr(user, "email", ""),
                "active": getattr(user, "active", 1),
                "first_name": getattr(user, "first_name", ""),
                "last_name": getattr(user, "last_name", ""),
                "login_count": getattr(user, "login_count", 0) or 0,
                "created_on": getattr(user, "created_on", ""),
                "is_authenticated": True,
                "roles": roles,
                "permissions": sorted(
                    [list(p) for p in getattr(user, "permissions", set())]
                ),
            }
        )
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

            sentry_sdk.set_user(
                {
                    "id": str(getattr(user, "id", "")),
                    "email": getattr(user, "email", ""),
                    "username": getattr(user, "username", ""),
                }
            )
        except ImportError:
            pass
