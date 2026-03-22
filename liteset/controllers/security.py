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
"""Security controller — CSRF token, guest token, roles search and auth-related endpoints."""

from __future__ import annotations

import logging
from typing import Any

import msgspec
from litestar import Controller, get, post
from litestar.connection import Request
from litestar.datastructures import State
from litestar.di import Provide

from liteset.controllers.base import extract_pagination
from liteset.events import event_logger
from liteset.guards.rbac import require_permission
from liteset.params.rison import provide_rison_query
from liteset.providers import provide_role_dao
from liteset.security.guest import validate_guest_token_resources
from liteset.typing import SecurityManagerProtocol

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request / response schemas for guest_token endpoint
# ---------------------------------------------------------------------------


class GuestTokenUser(msgspec.Struct):
    """User info embedded in the guest token."""

    username: str
    first_name: str = ""
    last_name: str = ""


class GuestTokenResource(msgspec.Struct):
    """Resource access entry (type + id)."""

    type: str
    id: str | int


class GuestTokenRlsRule(msgspec.Struct):
    """Row Level Security rule for the guest token."""

    clause: str
    dataset: int | None = None


class GuestTokenCreateBody(msgspec.Struct):
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
        opt={"exclude_from_auth": True},
    )
    async def csrf_token(self, request: Request[Any, Any, Any]) -> dict[str, str]:
        """Get a CSRF token for state-changing requests.

        Returns the CSRF token from the cookie set by Litestar's
        CSRFConfig middleware. On the first request (when no CSRF cookie
        exists yet), this endpoint returns an empty string in ``result``.
        The CSRF middleware will set the token cookie in the *response*
        Set-Cookie header. The client should therefore:

        1. Call ``GET /api/v1/security/csrf_token/``.
        2. Read the CSRF token from the ``Set-Cookie`` header (or the
           browser cookie jar) — **not** from the JSON body on first call.
        3. On subsequent requests, the JSON ``result`` will contain the
           token value since the cookie is now present on the request.
        4. Include the token in the ``X-CSRFToken`` header for all
           state-changing (POST/PUT/DELETE) requests.

        Backward-compatible with Superset frontend's
        GET /api/v1/security/csrf_token/ endpoint.
        """
        # Read the CSRF cookie that Litestar's CSRFConfig middleware sets.
        # The cookie name is configurable but defaults to "csrf_access_token".
        # NOTE: On the very first request the cookie will not yet exist on the
        # incoming request — the middleware generates a new token and sets it
        # only on the *response*.  The client must read it from Set-Cookie.
        settings = getattr(request.app.state, "settings", None)
        cookie_name = getattr(settings, "csrf_cookie_name", "csrf_access_token")
        token = request.cookies.get(cookie_name, "")
        event_logger.log("security.csrf_token")
        return {"result": token}

    @post(
        "/guest_token/",
        guards=[require_permission("can_grant_guest_token", "Security")],
    )
    async def guest_token(
        self,
        data: GuestTokenCreateBody,
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
        from liteset.exceptions import LitesetValidationException

        # Convert msgspec Structs to plain dicts for downstream functions
        resources_raw: list[dict[str, Any]] = [
            {"type": r.type, "id": r.id} for r in data.resources
        ]

        # Validate resource entries
        errors = validate_guest_token_resources(resources_raw)
        if errors:
            raise LitesetValidationException(
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
                raise LitesetValidationException(
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
                raise LitesetValidationException(
                    "Guest token validation failed"
                )

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
            raise LitesetValidationException(
                "Guest token creation requires a configured secret key "
                "(guest_token_jwt_secret or secret_key)"
            )

        exp_seconds: int = getattr(
            settings, "guest_token_jwt_exp_seconds", 3600
        )

        user_dict: dict[str, Any] = {
            "username": data.user.username,
            "first_name": data.user.first_name,
            "last_name": data.user.last_name,
        }
        rls_raw: list[dict[str, Any]] = [
            msgspec.structs.asdict(r) for r in data.rls
        ]

        token = security_manager.create_guest_access_token(
            secret_key=secret_key,
            user=user_dict,
            resources=resources_raw,
            rls=rls_raw,
            exp_seconds=exp_seconds,
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
        from liteset.schemas.security import RoleResponse, RolesSearchResponse

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
