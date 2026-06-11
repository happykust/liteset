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
"""Current user controller — /api/v1/me/ endpoints."""

from __future__ import annotations

from typing import Any

from litestar import Controller, get, put
from litestar.di import Provide

from superset.config import SupersetSettings
from superset.events import event_logger
from superset.guards.rbac import require_authenticated_user as require_authentication
from superset.schemas.user import (
    CurrentUserUpdateRequest,
)
from superset.typing import UserProtocol


def _provide_user_dao(session: Any) -> Any:
    """Lazy provider for AsyncUserDAO — avoids eager Flask imports."""
    from superset.db.daos.user import AsyncUserDAO

    return AsyncUserDAO(session)


def _user_is_active(user: Any) -> bool:
    """Resolve FAB's ``is_active`` for CachedUser/ORM users.

    FAB ``User.is_active`` is a property over the ``active`` column
    (flask_appbuilder/security/sqla/models.py:222-224); neither CachedUser
    nor the liteset ORM model defines ``is_active``, so fall back to
    ``active`` instead of a hardcoded True.
    """
    value = getattr(user, "is_active", None)
    if value is None:
        value = getattr(user, "active", True)
    return bool(value)


def _build_roles_map(
    current_user: Any,
) -> dict[str, list[tuple[str, str]]]:
    """Build role->permissions map from user roles.

    Handles both CachedUser (flat permissions set) and ORM User
    (roles with .permissions PermissionView objects).
    Mirrors security_manager.get_user_roles_permissions().
    """

    # 1:1 with FAB security_manager.get_user_roles() at
    # flask_appbuilder/security/manager.py:1828:
    # ``return user.roles + [role for group in user.groups for role in group.roles]``
    # Groups contribute additional roles that must be included.
    direct_roles = list(getattr(current_user, "roles", []) or [])
    group_roles = [
        role
        for group in (getattr(current_user, "groups", None) or [])
        for role in (getattr(group, "roles", None) or [])
    ]
    user_roles = direct_roles + group_roles
    roles: dict[str, list[tuple[str, str]]] = {}

    for role in user_roles:
        role_name = getattr(role, "name", "")
        role_perms: list[tuple[str, str]] = []
        for pvm in getattr(role, "permissions", []):
            perm_name = getattr(
                getattr(pvm, "permission", None),
                "name",
                "",
            )
            view_name = getattr(
                getattr(pvm, "view_menu", None),
                "name",
                "",
            )
            if perm_name and view_name:
                role_perms.append((perm_name, view_name))
        roles[role_name] = role_perms

    return roles


class CurrentUserController(Controller):
    path = "/api/v1/me"
    tags = ["Current User"]
    dependencies = {
        "user_dao": Provide(_provide_user_dao, sync_to_thread=False),
    }

    @get("/", guards=[require_authentication])
    async def get_me(self, current_user: UserProtocol) -> dict[str, Any]:
        """GET /api/v1/me/ — get current user info.

        Mirrors original ``UserResponseSchema`` at
        superset_old/views/users/schemas.py:30-38 — returns
        ``id``/``username``/``email``/``first_name``/``last_name``/
        ``is_active``/``is_anonymous``/``login_count``.
        """
        return {
            "result": {
                "id": current_user.id,
                "username": current_user.username,
                "first_name": getattr(current_user, "first_name", ""),
                "last_name": getattr(current_user, "last_name", ""),
                "email": getattr(current_user, "email", ""),
                "is_active": _user_is_active(current_user),
                "is_anonymous": not current_user.is_authenticated,
                "login_count": int(getattr(current_user, "login_count", 0) or 0),
            }
        }

    @get("/roles/", guards=[require_authentication])
    async def get_my_roles(
        self, current_user: UserProtocol, user_dao: Any
    ) -> dict[str, Any]:
        """GET /api/v1/me/roles/ — get current user roles and permissions.

        Returns bootstrap_user_data-compatible payload. Ported 1:1 from
        superset_old/views/utils.py::bootstrap_user_data (lines 99-129)
        and superset_old/views/utils.py::get_permissions (lines 137-152).

        Key behaviors:
        - For regular (non-anonymous, non-guest) users includes
          ``createdOn`` and ``loginCount`` (bootstrap_user_data:119-122).
        - The ``permissions`` dict is filtered to only
          ``datasource_access`` and ``database_access`` entries
          (get_permissions:146-148).
        - ``roles`` dict contains ALL role permissions (unfiltered).
        """
        from collections import defaultdict

        from superset.middleware.auth import CachedUser

        is_anonymous = not current_user.is_authenticated
        is_guest = getattr(current_user, "is_guest", False)

        # Load full ORM user to get proper per-role permissions mapping.
        # The eager-load chain (roles/groups -> permissions -> permission/
        # view_menu) is required: _build_roles_map traverses all of it, and
        # async lazy loads raise MissingGreenlet.
        if isinstance(current_user, CachedUser):
            current_user = await user_dao.get_by_id_with_role_permissions(
                current_user.id
            )
            if current_user is None:
                # The user was deleted while their session token is still
                # valid — treat as unauthenticated (401) instead of an
                # AttributeError 500 in the payload builder below.
                from litestar.exceptions import NotAuthorizedException

                raise NotAuthorizedException(detail="User no longer exists")
            roles = _build_roles_map(current_user)
        elif is_guest:
            # GuestUser carries lightweight ``_CachedRole`` stubs without
            # ``.permissions``; the original GuestUser.__init__ receives REAL
            # ORM Roles (superset_old/security/guest_token.py:81), so
            # get_user_roles_permissions returns the Guest role's actual DB
            # permissions. Reload the Role rows with the perm chain.
            from types import SimpleNamespace

            role_ids = [
                rid
                for rid in (
                    getattr(r, "id", None)
                    for r in (getattr(current_user, "roles", None) or [])
                )
                if rid
            ]
            orm_roles = (
                await user_dao.get_roles_with_permissions(role_ids) if role_ids else []
            )
            roles = _build_roles_map(SimpleNamespace(roles=orm_roles, groups=[]))
        else:
            roles = _build_roles_map(current_user)

        # Filter permissions to only datasource_access / database_access
        # mirrors get_permissions (superset_old/views/utils.py:143-152):
        #   for _, permissions in roles_permissions.items():
        #       for permission in permissions:
        #           if permission[0] in ("datasource_access", "database_access"):
        #               data_permissions[permission[0]].add(permission[1])
        _data_access = {"datasource_access", "database_access"}
        data_permissions: dict[str, set[str]] = defaultdict(set)
        for _role_perms in roles.values():
            for perm_name, view_name in _role_perms:
                if perm_name in _data_access:
                    data_permissions[perm_name].add(view_name)
        permissions: dict[str, list[str]] = {
            k: list(v) for k, v in data_permissions.items()
        }

        # Build payload — mirrors bootstrap_user_data structure
        if is_anonymous:
            payload: dict[str, Any] = {}
        elif is_guest:
            payload = {
                "username": current_user.username,
                "firstName": getattr(current_user, "first_name", ""),
                "lastName": getattr(current_user, "last_name", ""),
                "isActive": _user_is_active(current_user),
                "isAnonymous": False,
            }
        else:
            # Regular authenticated user — includes createdOn + loginCount
            # (superset_old/views/utils.py:111-122)
            created_on_raw = getattr(current_user, "created_on", None)
            # CachedUser stores created_on as an already-serialised ISO string
            # (middleware/auth.py:517-524); ORM Users carry a datetime. Handle both.
            if isinstance(created_on_raw, str):
                created_on_str: str | None = created_on_raw or None
            else:
                created_on_str = created_on_raw.isoformat() if created_on_raw else None
            payload = {
                "username": current_user.username,
                "firstName": getattr(current_user, "first_name", ""),
                "lastName": getattr(current_user, "last_name", ""),
                "userId": current_user.id,
                "isActive": _user_is_active(current_user),
                "isAnonymous": False,
                "createdOn": created_on_str,
                "email": getattr(current_user, "email", ""),
                "loginCount": int(getattr(current_user, "login_count", 0) or 0),
            }

        payload["roles"] = roles
        payload["permissions"] = permissions

        return {"result": payload}

    @put("/", guards=[require_authentication])
    async def update_me(
        self,
        data: CurrentUserUpdateRequest,
        current_user: UserProtocol,
        user_dao: Any,
    ) -> dict[str, Any]:
        """PUT /api/v1/me/ — update current user (first_name, last_name, password).

        1:1 with ``superset_old/views/users/api.py::update_me``:
        - empty payload → 400 ``At least one field must be provided.``;
        - response body is ``{"result": user_response_schema.dump(user)}``
          (full ``UserResponseSchema``, not a diff of just the patched keys).
        """
        from litestar.exceptions import HTTPException

        updates: dict[str, str] = {}
        if data.first_name is not None:
            updates["first_name"] = data.first_name
        if data.last_name is not None:
            updates["last_name"] = data.last_name

        # KEY presence counts: ``{"password": ""}`` passes the "at least one
        # field" guard in the original (the Marshmallow-loaded dict is truthy)
        # — pre_update then skips hashing for the falsy value but still
        # touches changed_on/changed_by_fk (superset_old/views/users/api.py).
        password_provided = data.password is not None

        if not updates and not password_provided:
            # Upstream uses ``self.response_400`` here, not 422.
            raise HTTPException(
                status_code=400,
                detail="At least one field must be provided.",
            )

        hashed_password: str | None = None
        if data.password:
            from superset.utils.password import (
                default_password_complexity,
                generate_password_hash,
            )

            settings = SupersetSettings()  # type: ignore[call-arg]
            if settings.fab_password_complexity_enabled:
                # Mirror upstream PasswordComplexityValidator.__call__:
                # use custom callable if configured, else default rules.
                # Raises HTTPException(422) with per-field detail on failure.
                validator = settings.fab_password_complexity_validator
                try:
                    if validator is not None:
                        validator(data.password)
                    else:
                        default_password_complexity(data.password or "")
                except Exception as exc:
                    raise HTTPException(
                        status_code=400,
                        detail={"password": [str(exc)]},  # type: ignore[arg-type]
                    ) from exc

            # Read FAB_PASSWORD_HASH_METHOD / FAB_PASSWORD_HASH_SALT_LENGTH from
            # config — mirrors pre_update (superset_old/views/users/api.py:52-56).
            hash_method = (
                getattr(settings, "fab_password_hash_method", "scrypt") or "scrypt"
            )
            salt_length = getattr(settings, "fab_password_hash_salt_length", 16) or 16
            hashed_password = generate_password_hash(
                data.password or "",
                method=hash_method,
                salt_length=salt_length,
            )

        # Pass changed_by_fk — mirrors pre_update
        # (superset_old/views/users/api.py:49-50)
        updated = await user_dao.update_profile(
            user_id=current_user.id,
            attributes=updates,
            hashed_password=hashed_password,
            changed_by_fk=current_user.id,
        )

        # Re-read so we serialise the persisted state (mirrors upstream's
        # ``user_response_schema.dump(g.user)`` after ``db.session.commit()``).
        fresh = await user_dao.get_by_id(current_user.id)
        if not updated or fresh is None:
            # The user row vanished while the session token was still valid
            # (upstream works on the in-memory ``g.user`` so this path can't
            # happen there) — 401, not an AttributeError 500.
            from litestar.exceptions import NotAuthorizedException

            raise NotAuthorizedException(detail="User no longer exists")
        await event_logger.alog_with_context("user.update_me", user_id=current_user.id)
        return {
            "result": {
                "id": fresh.id,
                "username": fresh.username,
                "email": getattr(fresh, "email", ""),
                "first_name": getattr(fresh, "first_name", ""),
                "last_name": getattr(fresh, "last_name", ""),
                "is_active": _user_is_active(fresh),
                "is_anonymous": False,
                "login_count": int(getattr(fresh, "login_count", 0) or 0),
            }
        }
