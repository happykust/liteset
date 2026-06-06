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
        superset_old/views/users/schemas.py:30 — returns only
        ``email``/``first_name``/``last_name``/``is_active``/
        ``is_anonymous``/``username``/``login_count``. ``id``/``roles``
        are intentionally absent (use ``/me/roles/`` for the latter).
        """
        return {
            "result": {
                "username": current_user.username,
                "first_name": getattr(current_user, "first_name", ""),
                "last_name": getattr(current_user, "last_name", ""),
                "email": getattr(current_user, "email", ""),
                "is_active": getattr(current_user, "is_active", True),
                "is_anonymous": not current_user.is_authenticated,
                "login_count": int(getattr(current_user, "login_count", 0) or 0),
            }
        }

    @get("/roles/", guards=[require_authentication])
    async def get_my_roles(self, current_user: UserProtocol) -> dict[str, Any]:
        """GET /api/v1/me/roles/ — get current user roles and permissions.

        Returns bootstrap_user_data-compatible payload including roles
        (with their permissions) and a flat permissions dict.

        Handles both ORM User objects (roles have .permissions with
        PermissionView objects) and CachedUser objects (_CachedRole with
        only id/name — permissions are on the user object directly).
        """
        from superset.middleware.auth import CachedUser

        user_roles = getattr(current_user, "roles", [])

        roles: dict[str, list[tuple[str, str]]] = {}
        permissions: dict[str, list[str]] = {}

        # CachedUser: roles are _CachedRole (id + name only, no
        # .permissions attribute).  Use the flat user.permissions set
        # and assign to every role (same approach as _build_user_data).
        if isinstance(current_user, CachedUser):
            user_perms: set[tuple[str, str]] = getattr(
                current_user, "permissions", set()
            )
            sorted_perms = sorted(user_perms)
            for role in user_roles:
                role_name = getattr(role, "name", "")
                roles[role_name] = list(sorted_perms)
            for action, resource in sorted_perms:
                permissions.setdefault(action, [])
                if resource not in permissions[action]:
                    permissions[action].append(resource)
        else:
            # ORM User: roles have .permissions (PermissionView objects)
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
                        permissions.setdefault(perm_name, [])
                        if view_name not in permissions[perm_name]:
                            permissions[perm_name].append(view_name)
                roles[role_name] = role_perms

        return {
            "result": {
                "username": current_user.username,
                "firstName": getattr(current_user, "first_name", ""),
                "lastName": getattr(current_user, "last_name", ""),
                "userId": current_user.id,
                "isActive": getattr(current_user, "is_active", True),
                "isAnonymous": not current_user.is_authenticated,
                "email": getattr(current_user, "email", ""),
                "roles": roles,
                "permissions": permissions,
            }
        }

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

        has_password = data.password is not None

        if not updates and not has_password:
            # Upstream uses ``self.response_400`` here, not 422.
            raise HTTPException(
                status_code=400,
                detail="At least one field must be provided.",
            )

        hashed_password: str | None = None
        if has_password:
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
                        status_code=422,
                        detail={"password": [str(exc)]},
                    ) from exc

            hashed_password = generate_password_hash(data.password or "")

        await user_dao.update_profile(
            user_id=current_user.id,
            attributes=updates,
            hashed_password=hashed_password,
        )

        # Re-read so we serialise the persisted state (mirrors upstream's
        # ``user_response_schema.dump(g.user)`` after ``db.session.commit()``).
        fresh = await user_dao.get_by_id(current_user.id)
        await event_logger.alog_with_context("user.update_me", user_id=current_user.id)
        return {
            "result": {
                "id": fresh.id,
                "username": fresh.username,
                "email": getattr(fresh, "email", ""),
                "first_name": getattr(fresh, "first_name", ""),
                "last_name": getattr(fresh, "last_name", ""),
                "is_active": bool(getattr(fresh, "is_active", True)),
                "is_anonymous": False,
                "login_count": int(getattr(fresh, "login_count", 0) or 0),
            }
        }
