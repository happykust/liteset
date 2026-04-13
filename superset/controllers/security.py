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
"""Security controller — CSRF token, guest token, JWT login/refresh,
roles search and auth-related endpoints."""

from __future__ import annotations

import logging
import time
from typing import Any, Literal

import jwt as pyjwt
import msgspec
from litestar import Controller, get, post
from litestar.connection import Request
from litestar.datastructures import State
from litestar.di import Provide
from litestar.exceptions import NotAuthorizedException, ValidationException

from superset.controllers.base import extract_pagination
from superset.events import event_logger
from superset.guards.rbac import require_authentication, require_permission
from superset.params.rison import provide_rison_query
from superset.providers import provide_role_dao
from superset.security.guest import validate_guest_token_resources
from superset.typing import SecurityManagerProtocol

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# JWT helper functions
# ---------------------------------------------------------------------------


def _get_jwt_secret(settings: Any) -> str:
    """Extract JWT secret from settings (supports SecretStr and plain str)."""
    secret_key = settings.secret_key
    if hasattr(secret_key, "get_secret_value"):
        return secret_key.get_secret_value()
    return str(secret_key)


def _create_api_access_token(
    secret_key: str,
    *,
    user_id: int,
    expires_in: int = 900,
    fresh: bool = True,
) -> str:
    """Create a JWT access token for API authentication."""
    now = int(time.time())
    payload = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + expires_in,
        "type": "access",
        "fresh": fresh,
    }
    return pyjwt.encode(payload, secret_key, algorithm="HS256")


def _create_api_refresh_token(
    secret_key: str,
    *,
    user_id: int,
    expires_in: int = 86400 * 30,
) -> str:
    """Create a JWT refresh token for obtaining new access tokens."""
    now = int(time.time())
    payload = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + expires_in,
        "type": "refresh",
    }
    return pyjwt.encode(payload, secret_key, algorithm="HS256")


# ---------------------------------------------------------------------------
# Request / response schemas for login/refresh endpoints
# ---------------------------------------------------------------------------


class LoginRequest(msgspec.Struct):
    """POST body for ``/api/v1/security/login``."""

    username: str
    password: str
    provider: str = "db"
    refresh: bool = True


class LoginResponse(msgspec.Struct):
    """Response for successful login."""

    access_token: str
    refresh_token: str = ""


class RefreshResponse(msgspec.Struct):
    """Response for successful token refresh."""

    access_token: str


# ---------------------------------------------------------------------------
# Request / response schemas for guest_token endpoint
# ---------------------------------------------------------------------------


class GuestTokenUser(msgspec.Struct):
    """User info embedded in the guest token."""

    username: str
    first_name: str = ""
    last_name: str = ""


GuestTokenResourceType = Literal["dashboard"]


class GuestTokenResource(msgspec.Struct):
    """Resource access entry (type + id)."""

    type: GuestTokenResourceType
    id: str | int


class GuestTokenRlsRule(msgspec.Struct):
    """Row Level Security rule for the guest token."""

    clause: str
    dataset: int | None = None


class GuestTokenCreateSchema(msgspec.Struct):
    """POST body for ``/api/v1/security/guest_token/``."""

    user: GuestTokenUser
    resources: list[GuestTokenResource]
    rls: list[GuestTokenRlsRule]


class SecurityController(Controller):
    """Security-related API endpoints."""

    path = "/api/v1/security"
    tags = ["Security"]
    dependencies = {
        "role_dao": Provide(provide_role_dao, sync_to_thread=False),
        "rison_params": Provide(provide_rison_query),
    }

    @get(
        "/csrf_token/",
        guards=[require_authentication],
    )
    async def csrf_token(
        self,
        request: Request[Any, Any, Any],
    ) -> dict[str, str]:
        """Generate and return a CSRF token.

        Compatible with Flask-WTF's ``generate_csrf()``.
        Frontend stores this and sends it in ``X-CSRFToken``
        header on POST/PUT/DELETE requests.
        """
        from superset.middleware.csrf import (
            generate_csrf_token,
        )

        settings = getattr(
            request.app.state,
            "settings",
            None,
        )
        secret = ""
        cookie_name = "session"
        if settings:
            sk = settings.secret_key
            if hasattr(sk, "get_secret_value"):
                sk = sk.get_secret_value()
            secret = str(sk)
            cookie_name = getattr(settings, "session_cookie_name", "session")

        # Extract the session cookie to bind the token
        session_id = request.cookies.get(cookie_name, "")

        token = generate_csrf_token(secret, session_id=session_id)
        event_logger.log("security.csrf_token")
        return {"result": token}

    @post(
        "/guest_token/",
        guards=[require_permission("can_grant_guest_token", "Security")],
    )
    async def guest_token(
        self,
        data: GuestTokenCreateSchema,
        request: Request[Any, Any, Any],
        security_manager: SecurityManagerProtocol,
        state: State,
    ) -> dict[str, str]:
        """POST /api/v1/security/guest_token/ — create a guest access token.

        Creates a short-lived JWT for embedded dashboard access.
        Requires the ``can_grant_guest_token`` permission on the
        ``Security`` resource.

        The request body must contain:
        - ``user``: dict with ``username`` (required), ``first_name``,
          ``last_name``.
        - ``resources``: list of ``{type, id}`` dicts (supported types:
          ``dashboard``, ``chart``).
        - ``rls``: list of Row Level Security rule dicts (``{clause}``).

        Returns ``{"token": "<jwt_string>"}`` on success.
        """
        from superset.exceptions import SupersetValidationException

        # Convert msgspec Structs to plain dicts for downstream functions
        resources_raw: list[dict[str, Any]] = [
            {"type": r.type, "id": r.id} for r in data.resources
        ]

        # Validate resource entries (schema + DB existence checks)
        # The session is obtained from the security_manager's DAO, which
        # shares the same request-scoped AsyncSession.
        session = security_manager.dao.session  # type: ignore[attr-defined]
        errors = await validate_guest_token_resources(resources_raw, session)
        if errors:
            raise SupersetValidationException(
                f"Invalid guest token resources: {'; '.join(errors)}"
            )

        # Apply GUEST_TOKEN_VALIDATOR_HOOK if configured (mirrors Superset's
        # current_app.config["GUEST_TOKEN_VALIDATOR_HOOK"]).
        settings = state.settings
        guest_token_validator_hook = getattr(
            settings, "guest_token_validator_hook", None
        )
        if guest_token_validator_hook is not None:
            if not callable(guest_token_validator_hook):
                raise SupersetValidationException(
                    "Guest token validator hook is not callable"
                )
            token_payload: dict[str, Any] = {
                "user": {
                    "username": data.user.username,
                    "first_name": data.user.first_name,
                    "last_name": data.user.last_name,
                },
                "resources": resources_raw,
                "rls": [msgspec.structs.asdict(r) for r in data.rls],
            }
            if not guest_token_validator_hook(token_payload):
                raise SupersetValidationException("Guest token validation failed")

        secret_key = getattr(settings, "guest_token_jwt_secret", "")
        if not secret_key:
            # Fall back to the main secret_key if no dedicated guest secret
            main_secret = getattr(settings, "secret_key", None)
            if main_secret is not None:
                # Support both SecretStr and plain str
                secret_key = (
                    main_secret.get_secret_value()
                    if hasattr(main_secret, "get_secret_value")
                    else str(main_secret)
                )
        if not secret_key:
            raise SupersetValidationException(
                "Guest token creation requires a configured secret key "
                "(guest_token_jwt_secret or secret_key)"
            )

        exp_seconds: int = getattr(settings, "guest_token_jwt_exp_seconds", 3600)

        # Resolve audience: GUEST_TOKEN_JWT_AUDIENCE config or request URL.
        # Matches the original _get_guest_token_jwt_audience() logic.
        audience_setting = getattr(settings, "guest_token_jwt_audience", None)
        if audience_setting is not None:
            audience = (
                audience_setting() if callable(audience_setting) else audience_setting
            )
        else:
            host = request.headers.get("host", "")
            scheme = request.scope.get("scheme", "http")
            audience = f"{scheme}://{host}" if host else ""
        audience = str(audience) if audience else ""

        user_dict: dict[str, Any] = {
            "username": data.user.username,
            "first_name": data.user.first_name,
            "last_name": data.user.last_name,
        }
        rls_raw: list[dict[str, Any]] = [msgspec.structs.asdict(r) for r in data.rls]

        token = security_manager.create_guest_access_token(
            secret_key=secret_key,
            user=user_dict,
            resources=resources_raw,
            rls=rls_raw,
            exp_seconds=exp_seconds,
            audience=audience,
        )
        event_logger.log("security.guest_token", extra={"username": data.user.username})
        return {"token": token}

    @get(
        "/roles/search/",
        guards=[require_permission("list_roles", "Security")],
    )
    async def search_roles(
        self,
        role_dao: Any,
        rison_params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """GET /api/v1/security/roles/search/ -- paginated role search.

        Supports Rison query parameters:
        - ``page``, ``page_size`` for pagination
        - ``order_column`` (id | name), ``order_direction`` (asc | desc)
        - ``filters`` with ``col=name`` for name substring matching
        """
        from superset.schemas.security import RoleResponse, RolesSearchResponse

        params = rison_params or {}
        page, page_size = extract_pagination(rison_params)
        order_column = params.get("order_column", "id")
        order_direction = params.get("order_direction", "asc")

        # Extract name filter from rison filters list
        name_filter: str | None = None
        for f in params.get("filters", []):
            if f.get("col") == "name":
                name_filter = f.get("value")

        # Validate order_column to prevent arbitrary column access
        if order_column not in ("id", "name"):
            order_column = "id"

        roles, total = await role_dao.search(
            name_filter=name_filter,
            order_column=order_column,
            order_direction=order_direction,
            page=page,
            page_size=page_size,
        )

        result = [
            RoleResponse(
                id=role.id,
                name=role.name,
                user_ids=[u.id for u in (role.user or [])],
                permission_ids=[p.id for p in (role.permissions or [])],
                group_ids=[g.id for g in (role.groups or [])],
            )
            for role in roles
        ]

        event_logger.log("security.search_roles")

        return msgspec.to_builtins(
            RolesSearchResponse(
                result=result,
                count=total,
                ids=[r.id for r in result],
            )
        )

    # -----------------------------------------------------------------
    # Login / Refresh endpoints
    # -----------------------------------------------------------------

    @post(
        "/login",
        opt={"exclude_from_auth": True},
        status_code=200,
    )
    async def login(
        self,
        data: LoginRequest,
        state: State,
    ) -> LoginResponse | dict[str, str]:
        """POST /api/v1/security/login -- authenticate and return JWT tokens.

        Supports ``provider=db`` (database auth) currently.
        Returns access_token and optionally refresh_token.
        """
        settings = state.settings
        secret_key = _get_jwt_secret(settings)

        # Validate provider
        valid_providers = {"db", "ldap"}
        if data.provider not in valid_providers:
            raise ValidationException(detail=f"Invalid provider: {data.provider}")

        # Check provider is allowed
        auth_type = getattr(settings, "auth_type", 1)
        allow_multiple = getattr(settings, "api_login_allow_multiple_providers", False)
        provider_type_map = {"db": 1, "ldap": 2}
        if not allow_multiple and provider_type_map.get(data.provider) != auth_type:
            raise ValidationException(detail=f"Provider '{data.provider}' not allowed")

        if not data.username:
            raise ValidationException(detail="Username is required")

        # Only DB auth is implemented
        if data.provider == "ldap":
            raise ValidationException(detail="LDAP provider not yet implemented")

        # Authenticate via DAO
        from superset.security.dao import AsyncSecurityDAO

        session_factory = state.session_factory
        async with session_factory() as session:
            dao = AsyncSecurityDAO(session)
            user = await dao.get_user_by_username(data.username)

            if user is None:
                raise NotAuthorizedException(detail="Invalid credentials")

            # Check active status
            if not getattr(user, "active", 1):
                raise NotAuthorizedException(detail="User is inactive")

            # Verify password
            if not self._check_password(user.password, data.password):
                raise NotAuthorizedException(detail="Invalid credentials")

            # Create tokens
            access_expires = getattr(settings, "jwt_access_token_expires", 900)
            access_token = _create_api_access_token(
                secret_key,
                user_id=user.id,
                expires_in=access_expires,
                fresh=True,
            )

            result: dict[str, str] = {"access_token": access_token}

            if data.refresh:
                refresh_expires = getattr(
                    settings, "jwt_refresh_token_expires", 86400 * 30
                )
                refresh_token = _create_api_refresh_token(
                    secret_key,
                    user_id=user.id,
                    expires_in=refresh_expires,
                )
                result["refresh_token"] = refresh_token

            event_logger.log(
                "security.login",
                extra={"username": data.username, "provider": data.provider},
            )
            return result

    @post(
        "/refresh",
        opt={"exclude_from_auth": True},
        status_code=200,
    )
    async def refresh(
        self,
        request: Request[Any, Any, Any],
        state: State,
    ) -> RefreshResponse | dict[str, str]:
        """POST /api/v1/security/refresh -- exchange refresh token for new access token.

        Requires a valid refresh token in the Authorization: Bearer header.
        Returns a new non-fresh access token.
        """
        settings = state.settings
        secret_key = _get_jwt_secret(settings)

        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            raise NotAuthorizedException(detail="Missing refresh token")

        token = auth_header[7:]
        if not token:
            raise NotAuthorizedException(detail="Missing refresh token")

        try:
            payload = pyjwt.decode(token, secret_key, algorithms=["HS256"])
        except pyjwt.PyJWTError as exc:
            raise NotAuthorizedException(
                detail="Invalid or expired refresh token"
            ) from exc

        if payload.get("type") != "refresh":
            raise NotAuthorizedException(detail="Invalid token type (expected refresh)")

        user_id_str = payload.get("sub")
        if not user_id_str:
            raise NotAuthorizedException(detail="Invalid refresh token (no sub)")

        try:
            user_id = int(user_id_str)
        except (ValueError, TypeError) as exc:
            raise NotAuthorizedException(
                detail="Invalid refresh token (bad sub)"
            ) from exc

        access_expires = getattr(settings, "jwt_access_token_expires", 900)
        access_token = _create_api_access_token(
            secret_key,
            user_id=user_id,
            expires_in=access_expires,
            fresh=False,
        )

        event_logger.log("security.refresh", extra={"user_id": user_id})
        return {"access_token": access_token}

    @staticmethod
    def _check_password(stored_hash: str, password: str) -> bool:
        """Verify password against a werkzeug-compatible hash.

        Supports scrypt and pbkdf2 formats used by Flask-AppBuilder.
        """
        from superset.utils.password import check_password_hash

        return check_password_hash(stored_hash, password)
