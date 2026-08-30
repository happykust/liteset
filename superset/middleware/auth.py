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

Supports cookie session (legacy itsdangerous), JWT Bearer (guest tokens),
and API key authentication. Redis user cache with TTL 300s (5 min) reduces
DB pool pressure.
"""

from __future__ import annotations

import json  # noqa: TID251
import logging
from dataclasses import dataclass, field
from typing import Any

from litestar.connection import ASGIConnection
from litestar.middleware import AbstractAuthenticationMiddleware, AuthenticationResult

from superset.security.auth_cache import (
    as_cache_str,
    AUTH_EPOCH_KEY,
    read_auth_epoch,
    sign_keyed_payload,
    verify_keyed_payload,
)
from superset.utils.core import set_current_user

logger = logging.getLogger(__name__)

# long enough to absorb bot/load-generator bursts, short enough that
# permission changes propagate within a reasonable window
_USER_CACHE_TTL: int = 300
_PUBLIC_ROLE_CACHE_KEY: str = "auth:public_role_perms"
_PUBLIC_ROLE_CACHE_TTL: int = 300


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
    """User reconstituted from Redis cache; same interface as ORM User."""

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
    id: int
    name: str


def _get_secret_key(settings: Any) -> str:
    secret_key = settings.secret_key
    if hasattr(secret_key, "get_secret_value"):
        secret_key = secret_key.get_secret_value()
    return secret_key


def _safe_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


class SupersetAuthMiddleware(AbstractAuthenticationMiddleware):
    """Multi-strategy authentication middleware.

    Tries in order:
    1. Session cookie (legacy itsdangerous) -> user_id -> DB lookup
    2. JWT Bearer token (guest tokens for embedded dashboards)

    Performance: Resolved users are cached in Redis (TTL ``_USER_CACHE_TTL``,
    300s). On Redis failure, falls back to direct DB lookup.
    """

    async def authenticate_request(
        self, connection: ASGIConnection[Any, Any, Any, Any]
    ) -> AuthenticationResult:
        user = await self._authenticate_cookie(connection)
        if user:
            set_current_user(user)
            return AuthenticationResult(user=user, auth="cookie")

        # Must be checked BEFORE the generic JWT Bearer path so that
        # embedded dashboard requests are recognised correctly.
        user = await self._authenticate_guest_token(connection)
        if user:
            set_current_user(user)
            return AuthenticationResult(user=user, auth="guest_token")

        user = await self._authenticate_jwt(connection)
        if user:
            set_current_user(user)
            return AuthenticationResult(user=user, auth="jwt")

        anon = await self._build_anonymous_user(connection)
        set_current_user(anon)
        return AuthenticationResult(user=anon, auth=None)

    async def _build_anonymous_user(
        self, connection: ASGIConnection[Any, Any, Any, Any]
    ) -> UnauthenticatedUser:
        """Build an anonymous user, loading Public role permissions when
        auth_role_public is configured."""
        settings = connection.app.state.settings
        role_name = getattr(settings, "auth_role_public", "")
        if not role_name:
            return UnauthenticatedUser()

        redis = getattr(connection.app.state, "redis", None)
        secret_key = _get_secret_key(settings)

        if redis is not None:
            try:
                cached = await self._get_cached_public_role_perms(
                    redis, role_name, secret_key
                )
                if cached is not None:
                    roles = [_CachedRole(id=0, name=role_name)] if cached else []
                    return UnauthenticatedUser(roles=roles, permissions=cached)
            except Exception:
                logger.debug("Redis error reading public role cache")

        permissions = await self._resolve_public_permissions(connection, role_name)

        if redis is not None:
            try:
                await self._cache_public_role_perms(
                    redis, role_name, permissions, secret_key
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
        from superset.security.dao import AsyncSecurityDAO

        session_factory = connection.app.state.session_factory
        try:
            async with session_factory() as session:
                dao = AsyncSecurityDAO(session)
                return await dao.get_permissions_for_role_name(role_name)
        except Exception:
            logger.exception("Failed to resolve public role permissions")
            return set()

    @staticmethod
    async def _get_cached_public_role_perms(
        redis: Any,
        role_name: str,
        secret: str,
    ) -> set[tuple[str, str]] | None:
        """Read+verify the signed Public-role permission cache entry.

        Mirrors ``_get_cached_user``: the entry is a signed envelope keyed
        to the current auth epoch, so a Redis write cannot grant anonymous
        callers permissions the database never assigned to the role, and a
        role/permission mutation (``bump_auth_epoch``) invalidates it
        immediately rather than after the full TTL.  Also binds the role
        name into the signed payload so a rename of ``AUTH_ROLE_PUBLIC``
        cannot resurrect a stale grant under the new name.
        """
        data, raw_epoch = await redis.mget(_PUBLIC_ROLE_CACHE_KEY, AUTH_EPOCH_KEY)
        if data is None:
            return None
        try:
            envelope = json.loads(data)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(envelope, dict):
            return None

        payload = envelope.get("data")
        if not isinstance(payload, str) or not verify_keyed_payload(
            _PUBLIC_ROLE_CACHE_KEY, payload, str(envelope.get("sig", "")), secret
        ):
            logger.warning(
                "Discarding public-role auth cache entry: missing or invalid signature"
            )
            return None

        try:
            parsed = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(parsed, dict):
            return None
        if parsed.get("role") != role_name:
            return None
        if str(parsed.get("epoch", "")) != as_cache_str(raw_epoch):
            return None

        perm_list = parsed.get("permissions")
        if not isinstance(perm_list, list):
            return None
        return {
            (p[0], p[1])
            for p in perm_list
            if isinstance(p, (list, tuple)) and len(p) == 2
        }

    @staticmethod
    async def _cache_public_role_perms(
        redis: Any,
        role_name: str,
        permissions: set[tuple[str, str]],
        secret: str,
    ) -> None:
        epoch = await read_auth_epoch(redis)
        payload = json.dumps(
            {
                "epoch": epoch,
                "role": role_name,
                "permissions": sorted(list(p) for p in permissions),
            }
        )
        await redis.set(
            _PUBLIC_ROLE_CACHE_KEY,
            json.dumps(
                {
                    "sig": sign_keyed_payload(_PUBLIC_ROLE_CACHE_KEY, payload, secret),
                    "data": payload,
                }
            ),
            ex=_PUBLIC_ROLE_CACHE_TTL,
        )

    async def _authenticate_cookie(  # noqa: C901
        self, connection: ASGIConnection[Any, Any, Any, Any]
    ) -> Any | None:
        settings = connection.app.state.settings
        cookie_name = getattr(settings, "session_cookie_name", "session")
        cookie = connection.cookies.get(cookie_name)
        if not cookie:
            return None

        import jwt as pyjwt

        secret_key = _get_secret_key(settings)
        user_id: int | None = None
        token_iat: int | None = None
        try:
            # ``require: ["exp"]`` + the "type" == "session" check below
            # together ensure only a cookie minted by ``_create_session_cookie``
            # authenticates -- not just any HS256/SECRET_KEY JWT carrying a
            # ``user_id`` claim.  Without this, the signed ``state`` JWT the
            # database-OAuth2 flow puts in a query parameter (sent to a
            # third-party IdP, and so exposed in its logs / Referer / browser
            # history) would double as a fully authenticated session cookie.
            payload = pyjwt.decode(
                cookie,
                secret_key,
                algorithms=["HS256"],
                options={"require": ["exp"]},
            )
            if payload.get("type") == "session":
                user_id = payload.get("user_id")
                iat_raw = payload.get("iat")
                if iat_raw is not None:
                    token_iat = int(iat_raw)
            else:
                logger.debug("Rejecting cookie JWT without a 'session' type claim")
        except Exception:  # noqa: S110
            pass

        is_legacy_cookie = False
        if user_id is None:
            decoder = self._get_or_create_decoder(connection)
            user_id = decoder.get_user_id(cookie)
            is_legacy_cookie = user_id is not None

        if user_id is None:
            return None

        # Check token blacklist: if the user logged out after this token
        # was issued, reject it.  The logout handler writes
        # ``auth:token_blacklist:{user_id}`` with the logout timestamp.
        redis = getattr(connection.app.state, "redis", None)
        if redis is not None:
            if is_legacy_cookie:
                # Legacy itsdangerous cookies carry no "iat"-equivalent
                # claim this middleware can read, so a per-token timestamp
                # comparison isn't possible: any blacklist entry for this
                # user must revoke every legacy cookie outright.
                if await self._is_user_blacklisted(redis, user_id):
                    logger.debug(
                        "Rejecting blacklisted legacy cookie for user %d",
                        user_id,
                    )
                    return None
            elif token_iat is not None and await self._is_token_blacklisted(
                redis, user_id, token_iat
            ):
                logger.debug(
                    "Rejecting blacklisted cookie token for user %d (iat=%d)",
                    user_id,
                    token_iat,
                )
                return None

        if redis is not None:
            try:
                cached = await self._get_cached_user(
                    redis, f"auth:user:{user_id}", user_id, secret_key
                )
                if cached is not None:
                    self._set_sentry_user(cached)
                    return cached
            except Exception:
                logger.debug("Redis cache miss/error for user %d", user_id)

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

        if redis is not None:
            try:
                await self._cache_user(redis, user, secret_key)
            except Exception:
                logger.debug("Failed to cache user %d in Redis", user_id)

        self._set_sentry_user(user)

        return user

    @staticmethod
    def _get_or_create_decoder(
        connection: ASGIConnection[Any, Any, Any, Any],
    ) -> Any:
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

        1. Reads the header named by ``GUEST_TOKEN_HEADER_NAME``
           (default ``X-GuestToken``).
        2. Falls back to ``guest_token`` in POST form body (used by the
           browser ``sendBeacon`` API which cannot set custom headers).
        3. Only active when the ``EMBEDDED_SUPERSET`` feature flag or
           ``embedded_superset`` setting is enabled.
        """
        settings = connection.app.state.settings

        feature_flags = getattr(settings, "feature_flags", {})
        embedded_enabled = getattr(
            settings, "embedded_superset", False
        ) or feature_flags.get("EMBEDDED_SUPERSET", False)
        if not embedded_enabled:
            return None

        header_name = getattr(settings, "guest_token_header_name", "X-GuestToken")
        raw_token = connection.headers.get(header_name.lower(), "")

        # Fallback: POST form data ``guest_token``
        # (sendBeacon API cannot set custom headers).
        if not raw_token:
            raw_token = await self._read_guest_token_from_body(connection)

        if not raw_token:
            return None

        return await self._resolve_guest_from_jwt(connection, raw_token)

    async def _authenticate_jwt(
        self, connection: ASGIConnection[Any, Any, Any, Any]
    ) -> Any | None:
        """Authenticate via ``Authorization: Bearer`` API access tokens
        (type=access)."""
        auth_header = connection.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            return None
        token = auth_header[7:]
        if not token:
            return None

        return await self._resolve_user_from_access_token(connection, token)

    async def _resolve_user_from_access_token(  # noqa: C901
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

        redis = getattr(connection.app.state, "redis", None)
        if await self._check_blacklist(redis, user_id, token_iat):
            return None

        cache_secret = _get_secret_key(connection.app.state.settings)
        if redis is not None:
            try:
                cached = await self._get_cached_user(
                    redis, f"auth:user:{user_id}", user_id, cache_secret
                )
                if cached is not None:
                    return cached
            except Exception:
                logger.debug("Redis cache miss/error for access token user %d", user_id)

        user = await self._resolve_user_from_db(connection, user_id)
        if user is None:
            return None

        # Populate Redis cache so the next JWT-authenticated request
        # for this user skips the DB round-trip entirely.  Mirrors the
        # cookie-auth path (``_authenticate_session_cookie``) which has
        # always cached on cache miss.  Without this, every Locust /
        # bot request repeatedly drains two metadata pool connections
        # (user lookup + permissions resolution) just on auth — the
        # dominant pool-exhaustion source under load.
        if redis is None:
            logger.warning(
                "JWT auth: app.state.redis is None — cache write skipped, "
                "every request will hit DB twice for user %d",
                user_id,
            )
        else:
            try:
                await self._cache_user(redis, user, cache_secret)
                logger.debug("JWT auth: cached user %d in Redis", user_id)
            except Exception:
                # Surface the real failure instead of silently masking it.
                # Without this, a JSON-encoding bug or a redis-client
                # mismatch makes every request fall back to DB while the
                # operator has no signal in logs.
                logger.exception("JWT auth: failed to cache user %d in Redis", user_id)

        return user

    async def _resolve_user_from_db(
        self,
        connection: ASGIConnection[Any, Any, Any, Any],
        user_id: int,
    ) -> CachedUser | None:
        """Resolve user from DB via a short-lived session.

        Returns a CachedUser with pre-resolved permissions so that RBAC
        guards can check ``user.permissions`` without extra DB queries.

        NOTE: the upstream app-builder supports ``builtin_roles`` (config
        key ``FAB_ROLES``) which are regex-based permission sets that bypass
        DB queries.  Liteset does not currently have ``FAB_ROLES`` in its
        config, so builtin role handling is not implemented here.  If
        ``FAB_ROLES`` support is needed in the future, the builtin role
        permissions should be merged into the returned permission set here,
        matching the upstream ``get_user_roles_permissions`` logic.
        """
        from superset.security.dao import AsyncSecurityDAO

        session_factory = connection.app.state.session_factory
        async with session_factory() as session:
            dao = AsyncSecurityDAO(session)
            user = await dao.get_user_by_id(user_id)
            if user is None:
                return None
            active = getattr(user, "active", None)
            if active is not None and not active:
                return None
            permissions = await dao.get_all_permissions_for_user_with_groups(user_id)
            created_on_raw = getattr(user, "created_on", None)
            created_on_str = ""
            if created_on_raw is not None:
                if hasattr(created_on_raw, "isoformat"):
                    created_on_str = created_on_raw.isoformat()
                else:
                    created_on_str = str(created_on_raw)
            # Group-inherited roles count. Flask-AppBuilder's
            # ``get_user_roles`` returns ``user.roles`` plus every role reached
            # through the user's groups, and callers such as ``is_admin`` read
            # this list by name — so building it from direct roles alone denies
            # a user whose Admin role comes via a group. Permissions were
            # already resolved across both (``get_all_permissions_for_user_
            # with_groups``); this brings the role list in line with them.
            roles = {
                r.id: _CachedRole(id=r.id, name=r.name)
                for r in getattr(user, "roles", [])
            }
            for group in await dao.get_user_groups(user_id):
                for group_role in await dao.get_group_roles(group[0]):
                    roles.setdefault(
                        group_role[0], _CachedRole(id=group_role[0], name=group_role[1])
                    )

            return CachedUser(
                id=user.id,
                username=user.username,
                email=getattr(user, "email", ""),
                active=getattr(user, "active", 1),
                first_name=getattr(user, "first_name", ""),
                last_name=getattr(user, "last_name", ""),
                login_count=getattr(user, "login_count", 0) or 0,
                created_on=created_on_str,
                roles=list(roles.values()),
                permissions=permissions,
            )

    @staticmethod
    async def _read_guest_token_from_body(
        connection: ASGIConnection[Any, Any, Any, Any],
    ) -> str:
        """Read ``guest_token`` from URL-encoded POST body (sendBeacon fallback)."""
        from urllib.parse import parse_qs

        method = str(connection.scope.get("method", "GET")).upper()
        if method not in {"POST", "PUT", "PATCH"}:
            return ""

        content_type = connection.headers.get("content-type", "")
        if "application/x-www-form-urlencoded" not in content_type:
            return ""

        try:
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
        from superset.security.guest import GuestUser, parse_guest_token

        settings = connection.app.state.settings
        guest_secret = getattr(settings, "guest_token_jwt_secret", "")
        if guest_secret:
            secret_key = guest_secret
        else:
            secret_key = _get_secret_key(settings)

        algo = getattr(settings, "guest_token_jwt_algo", "HS256")

        # Resolve audience: GUEST_TOKEN_JWT_AUDIENCE config or WEBDRIVER_BASEURL.
        # get_url_host() returns app.config["WEBDRIVER_BASEURL"] — a configured
        # base URL, NOT the attacker-controllable Host request header.
        audience_setting = getattr(settings, "guest_token_jwt_audience", None)
        if audience_setting is not None:
            audience = (
                audience_setting() if callable(audience_setting) else audience_setting
            )
        else:
            audience = getattr(settings, "webdriver_baseurl", "")
        audience = str(audience) if audience else ""

        payload = parse_guest_token(
            token, secret_key, algorithm=algo, audience=audience
        )
        if payload is None:
            return None

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
        from superset.security.dao import AsyncSecurityDAO

        session_factory = connection.app.state.session_factory
        async with session_factory() as session:
            dao = AsyncSecurityDAO(session)
            role = await dao.get_role_by_name(role_name)
            if role is None:
                logger.warning("Guest role '%s' not found in database", role_name)
                return

            guest_user.roles = [_CachedRole(id=role.id, name=role.name)]

            role_perms = await dao.get_permissions_for_role_name(role_name)
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
            # Fail open so a Redis outage cannot lock everyone out, but log at
            # WARNING: this silently skips token revocation, so an operator must
            # be able to see it happened.
            logger.warning(
                "Redis error checking token blacklist for user %d; "
                "token revocation NOT enforced for this request",
                user_id,
            )
            return False

    @staticmethod
    async def _is_user_blacklisted(redis: Any, user_id: int) -> bool:
        """Check for *any* blacklist entry for *user_id*, with no per-token
        timestamp comparison.

        Used for the legacy itsdangerous cookie path, which carries no
        "iat"-equivalent claim this middleware can read: without a
        timestamp to compare, presence of a blacklist entry must revoke
        every legacy cookie for that user outright, not just ones issued
        after it.  Fails open (returns False) on Redis errors.
        """
        try:
            return await redis.get(f"auth:token_blacklist:{user_id}") is not None
        except Exception:
            logger.warning(
                "Redis error checking legacy cookie blacklist for user %d; "
                "cookie revocation NOT enforced for this request",
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

    async def _get_cached_user(
        self,
        redis: Any,
        cache_key: str,
        expected_user_id: int,
        secret: str,
    ) -> CachedUser | None:
        # Fetch the entry and the global cache epoch in one round-trip.  The
        # epoch is bumped by every role / permission / group mutation, so a
        # stale entry (one minted before the change) is rejected here rather
        # than being trusted for the rest of its TTL.
        data, raw_epoch = await redis.mget(cache_key, AUTH_EPOCH_KEY)
        if data is None:
            return None
        try:
            envelope = json.loads(data)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(envelope, dict):
            return None

        # The payload carries the user's roles and permissions and is trusted
        # for authorization, so it must be authenticated: whoever can write to
        # Redis must not thereby be able to grant themselves permissions.
        # The signature covers *cache_key* as well as the payload so that an
        # entry copied onto a different key (``COPY auth:user:1
        # auth:user:42``) does not verify under its new key.
        payload = envelope.get("data")
        if not isinstance(payload, str) or not verify_keyed_payload(
            cache_key, payload, str(envelope.get("sig", "")), secret
        ):
            logger.warning(
                "Discarding auth cache entry %s: missing or invalid signature",
                cache_key,
            )
            return None

        try:
            parsed = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            return None
        if str(parsed.get("epoch", "")) != as_cache_str(raw_epoch):
            return None
        # Belt-and-suspenders alongside the key-bound signature above: even
        # if an entry's signature verified for *this* key, the payload's own
        # ``id`` must match the user being looked up under it.
        if _safe_int(parsed.get("id")) != expected_user_id:
            logger.warning(
                "Discarding auth cache entry %s: payload id does not match "
                "the requested user id",
                cache_key,
            )
            return None
        user = CachedUser.from_dict(parsed)
        if user is None:
            return None
        if not user.active:
            return None
        return user

    async def _cache_user(self, redis: Any, user: Any, secret: str) -> None:
        roles = [
            {"id": r.id, "name": r.name}
            for r in getattr(user, "roles", [])
            if hasattr(r, "id") and hasattr(r, "name")
        ]
        # Stamp the entry with the epoch it was minted under so a later
        # role/permission change invalidates it (see ``_get_cached_user``).
        epoch = await read_auth_epoch(redis)
        payload = json.dumps(
            {
                "epoch": epoch,
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
        cache_key = f"auth:user:{user.id}"
        await redis.set(
            cache_key,
            json.dumps(
                {
                    "sig": sign_keyed_payload(cache_key, payload, secret),
                    "data": payload,
                }
            ),
            ex=_USER_CACHE_TTL,
        )

    @staticmethod
    def _set_sentry_user(user: Any) -> None:
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
