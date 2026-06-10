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
from superset.guards.rbac import require_permission
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
    """User info embedded in the guest token.

    All fields are optional to match the original ``UserSchema`` (Marshmallow)
    where none of the fields are ``required=True``, and ``GuestTokenUser``
    (``TypedDict``, ``total=False``) — every key is optional.

    Fields use ``None`` as sentinel so callers can distinguish "not provided"
    from an explicit empty string.  ``_to_sparse_dict()`` produces the sparse
    dict passed to the JWT, mirroring Marshmallow's behaviour of only
    including keys that were explicitly provided by the caller.
    """

    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None

    def _to_sparse_dict(self) -> dict[str, str]:
        """Return a sparse dict containing only non-None fields.

        Mirrors Marshmallow 3 ``UserSchema().load({})`` → ``{}`` behaviour:
        absent fields are omitted from the output, which lets
        ``GuestUser.from_token_payload`` fallbacks fire for ``username`` /
        ``first_name`` / ``last_name``.
        """
        result: dict[str, str] = {}
        if self.username is not None:
            result["username"] = self.username
        if self.first_name is not None:
            result["first_name"] = self.first_name
        if self.last_name is not None:
            result["last_name"] = self.last_name
        return result


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
    """POST body for ``/api/v1/security/guest_token/``.

    ``user`` is optional to match the original ``GuestTokenCreateSchema``
    (Marshmallow) where ``user = fields.Nested(UserSchema)`` has no
    ``required=True``.  When omitted, an empty dict is used downstream
    (same as Marshmallow's default deserialization of a missing Nested field).
    """

    resources: list[GuestTokenResource]
    rls: list[GuestTokenRlsRule]
    user: GuestTokenUser | None = None


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
        guards=[require_permission("can_read", "SecurityRestApi")],
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
        await event_logger.alog_with_context("security.csrf_token")
        return {"result": token}

    @post(
        "/guest_token/",
        guards=[require_permission("can_grant_guest_token", "SecurityRestApi")],
        # Upstream returns 200 (security/api.py:192:
        # ``return self.response(200, token=token)``), not Litestar's
        # default 201 — short-lived JWT mint, no resource created.
        status_code=200,
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
        ``SecurityRestApi`` resource.

        The request body must contain:
        - ``user``: dict with ``username`` (required), ``first_name``,
          ``last_name``.
        - ``resources``: list of ``{type, id}`` dicts (supported types:
          ``dashboard``, ``chart``).
        - ``rls``: list of Row Level Security rule dicts (``{clause}``).

        Returns ``{"token": "<jwt_string>"}`` on success.
        """
        from superset.exceptions import (
            QueryObjectValidationError,
            SupersetGenericErrorException,
            SupersetValidationException,
        )

        # Convert msgspec Structs to plain dicts for downstream functions
        resources_raw: list[dict[str, Any]] = [
            {"type": r.type, "id": r.id} for r in data.resources
        ]

        # Validate resource entries (schema + DB existence checks).
        # Original catches EmbeddedDashboardNotFoundError → response_400()
        # and marshmallow ValidationError → response_400(); mirror as 400.
        session = security_manager.dao.session  # type: ignore[attr-defined]
        errors = await validate_guest_token_resources(resources_raw, session)
        if errors:
            raise QueryObjectValidationError(
                f"Invalid guest token resources: {'; '.join(errors)}"
            )

        # Resolve user — data.user may be None when the field was omitted from
        # the POST body (GuestTokenCreateSchema.user is Optional). Use an empty
        # GuestTokenUser as the default so downstream accesses never crash.
        # Original Marshmallow Nested field without required=True deserialized
        # a missing user as {} → same empty-user semantics.
        _user = data.user if data.user is not None else GuestTokenUser()

        # Apply GUEST_TOKEN_VALIDATOR_HOOK if configured (mirrors Superset's
        # current_app.config["GUEST_TOKEN_VALIDATOR_HOOK"]).
        settings = state.settings
        guest_token_validator_hook = getattr(
            settings, "guest_token_validator_hook", None
        )
        if guest_token_validator_hook is not None:
            if not callable(guest_token_validator_hook):
                # Original raises SupersetGenericErrorException → HTTP 500:
                # the hook is a server-side misconfiguration, not a client error.
                raise SupersetGenericErrorException(
                    message="Guest token validator hook not callable"
                )
            token_payload: dict[str, Any] = {
                "user": _user._to_sparse_dict(),
                "resources": resources_raw,
                "rls": [msgspec.structs.asdict(r) for r in data.rls],
            }
            if not guest_token_validator_hook(token_payload):
                # Original raises marshmallow ValidationError → response_400().
                raise QueryObjectValidationError("Guest token validation failed")

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

        # 1:1 with superset_old config GUEST_TOKEN_JWT_EXP_SECONDS = 300 (5 min).
        exp_seconds: int = getattr(settings, "guest_token_jwt_exp_seconds", 300)

        # Resolve audience: GUEST_TOKEN_JWT_AUDIENCE config or WEBDRIVER_BASEURL.
        # Mirrors original _get_guest_token_jwt_audience():
        #   audience = get_conf()["GUEST_TOKEN_JWT_AUDIENCE"] or get_url_host()
        # get_url_host() returns app.config["WEBDRIVER_BASEURL"] — a configured
        # base URL, NOT the attacker-controllable Host request header.
        audience_setting = getattr(settings, "guest_token_jwt_audience", None)
        if audience_setting is not None:
            audience = (
                audience_setting() if callable(audience_setting) else audience_setting
            )
        else:
            # Fallback: WEBDRIVER_BASEURL (matches original get_url_host())
            audience = getattr(settings, "webdriver_baseurl", "")
        audience = str(audience) if audience else ""

        # Build a sparse user dict (only include keys provided by the caller).
        # Matches Marshmallow's UserSchema sparse output — absent fields are
        # omitted so GuestUser.from_token_payload fallbacks fire correctly.
        user_dict: dict[str, Any] = _user._to_sparse_dict()
        rls_raw: list[dict[str, Any]] = [msgspec.structs.asdict(r) for r in data.rls]

        algorithm: str = getattr(settings, "guest_token_jwt_algo", "HS256")
        token = security_manager.create_guest_access_token(
            secret_key=secret_key,
            user=user_dict,
            resources=resources_raw,
            rls=rls_raw,
            algorithm=algorithm,
            exp_seconds=exp_seconds,
            audience=audience,
        )
        await event_logger.alog_with_context(
            "security.guest_token", extra={"username": _user.username or ""}
        )
        return {"token": token}

    @get(
        "/roles/search/",
        guards=[require_permission("can_list_roles", "RoleRestAPI")],
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
        - ``filters`` with ``col`` one of: name, user_ids, permission_ids,
          group_ids
        """
        from superset.schemas.security import RoleResponse, RolesSearchResponse

        params = rison_params or {}
        # Original security/api.py:298 defaults page_size to 10 (not 25)
        page, page_size = extract_pagination(rison_params, default_page_size=10)
        order_column = params.get("order_column", "id")
        order_direction = params.get("order_direction", "asc")

        # Original returns 400 for invalid order_column (security/api.py:289-292)
        valid_columns = ("id", "name")
        if order_column not in valid_columns:
            from litestar.exceptions import HTTPException

            raise HTTPException(
                status_code=400,
                detail=f"Invalid order column: {order_column}",
            )

        # Extract filters — mirrors original filter_dict loop (security/api.py:305-319)
        name_filter: str | None = None
        user_ids_filter: str | None = None
        permission_ids_filter: str | None = None
        group_ids_filter: str | None = None
        for f in params.get("filters", []):
            col = f.get("col")
            value = f.get("value")
            if col == "name":
                name_filter = value
            elif col == "user_ids":
                user_ids_filter = value
            elif col == "permission_ids":
                permission_ids_filter = value
            elif col == "group_ids":
                group_ids_filter = value

        roles, total = await role_dao.search(
            name_filter=name_filter,
            user_ids_filter=user_ids_filter,
            permission_ids_filter=permission_ids_filter,
            group_ids_filter=group_ids_filter,
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

        await event_logger.alog_with_context("security.search_roles")

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
        opt={"exclude_from_auth": True, "exclude_from_csrf": True},
        status_code=200,
    )
    async def login(
        self,
        data: LoginRequest,
        state: State,
    ) -> LoginResponse | dict[str, str]:
        """POST /api/v1/security/login -- authenticate and return JWT tokens.

        Supports ``provider=db`` (database auth) and ``provider=ldap``
        (LDAP-bind auth via :class:`AsyncSecurityManager`).
        Returns access_token and optionally refresh_token.
        """
        from superset.i18n import gettext

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

        # Authenticate via DAO/Security manager
        from superset.security.dao import AsyncSecurityDAO
        from superset.security.manager import AsyncSecurityManager

        session_factory = state.session_factory
        async with session_factory() as session:
            dao = AsyncSecurityDAO(session)
            user: Any | None

            if data.provider == "ldap":
                # Build a request-local SecurityManager bound to this
                # session so registration / role-sync writes commit through
                # the same transaction.
                feature_flags = getattr(settings, "feature_flags", {}) or {}
                embedded_enabled = bool(
                    getattr(settings, "embedded_superset", False)
                ) or bool(feature_flags.get("EMBEDDED_SUPERSET", False))
                sm = AsyncSecurityManager(
                    dao=dao,
                    admin_role_name=getattr(settings, "auth_role_admin", "Admin"),
                    public_role_name=getattr(settings, "auth_role_public", "Public"),
                    guest_role_name=getattr(settings, "guest_role_name", "Guest"),
                    dashboard_rbac_enabled=getattr(settings, "dashboard_rbac", False),
                    embedded_superset_enabled=embedded_enabled,
                )
                user = await sm.auth_user_ldap(
                    data.username,
                    data.password,
                    settings=settings,
                )
                if user is None:
                    raise NotAuthorizedException(
                        detail=gettext("Invalid login. Please try again.")
                    )
            else:
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

            await event_logger.alog_with_context(
                "security.login",
                extra={"username": data.username, "provider": data.provider},
            )
            return result

    @post(
        "/refresh",
        opt={"exclude_from_auth": True, "exclude_from_csrf": True},
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

        await event_logger.alog_with_context(
            "security.refresh", extra={"user_id": user_id}
        )
        return {"access_token": access_token}

    @staticmethod
    def _check_password(stored_hash: str, password: str) -> bool:
        """Verify password against a werkzeug-compatible hash.

        Supports scrypt and pbkdf2 formats used by Flask-AppBuilder.
        """
        from superset.utils.password import check_password_hash

        return check_password_hash(stored_hash, password)
