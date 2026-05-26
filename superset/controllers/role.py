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
"""Role controller — full CRUD + sub-resource mutations for FAB roles."""

from __future__ import annotations

import logging
from typing import Any

import msgspec
from litestar import Controller, delete, get, post, put
from litestar.di import Provide

from superset.controllers.base import extract_pagination
from superset.events import event_logger
from superset.exceptions import ObjectNotFoundError, SupersetValidationException
from superset.guards.rbac import require_permission
from superset.params.rison import provide_rison_query
from superset.providers import provide_role_dao
from superset.schemas.security import RoleResponse, RolesSearchResponse

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------


class RolePostBody(msgspec.Struct):
    """POST body for creating a role."""

    name: str


class RolePutBody(msgspec.Struct):
    """PUT body for updating a role."""

    name: str


class RolePermissionsPostBody(msgspec.Struct):
    """POST body for setting role permissions."""

    permission_view_menu_ids: list[int]


class RoleUsersPutBody(msgspec.Struct):
    """PUT body for setting role users."""

    user_ids: list[int]


class RoleGroupsPutBody(msgspec.Struct):
    """PUT body for setting role groups."""

    group_ids: list[int]


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class PermissionViewResponse(msgspec.Struct):
    """Single permission-view entry in role permissions response."""

    id: int
    permission_name: str | None = None
    view_menu_name: str | None = None


class RolePermissionsResponse(msgspec.Struct):
    """Response for GET /api/v1/role/{pk}/permissions/."""

    result: list[PermissionViewResponse] = []
    count: int = 0


class RoleController(Controller):
    """Full CRUD controller for FAB roles with sub-resource mutations."""

    path = "/api/v1/security/roles"
    tags = ["Security Roles"]
    dependencies = {
        "role_dao": Provide(provide_role_dao, sync_to_thread=False),
        "rison_params": Provide(provide_rison_query),
    }

    # ------------------------------------------------------------------
    # GET — list roles (paginated)
    # ------------------------------------------------------------------
    @get(
        "/",
        guards=[require_permission("can_get", "Role")],
    )
    async def get_list(
        self,
        role_dao: Any,
        rison_params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """GET /api/v1/security/roles/ — list roles with optional filtering/pagination.

        Supports Rison query parameters:
        - ``page``, ``page_size`` for pagination
        - ``order_column`` (id | name), ``order_direction`` (asc | desc)
        - ``filters`` with ``col=name`` for name substring matching
        """
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
                group_ids=[g.id for g in (getattr(role, "groups", None) or [])],
            )
            for role in roles
        ]

        await event_logger.alog_with_context("role.list")

        return msgspec.to_builtins(
            RolesSearchResponse(
                result=result,
                count=total,
                ids=[r.id for r in result],
            )
        )

    # ------------------------------------------------------------------
    # GET — single role
    # ------------------------------------------------------------------
    @get(
        "/{pk:int}",
        guards=[require_permission("can_get", "Role")],
    )
    async def get_single(
        self,
        role_dao: Any,
        pk: int,
    ) -> dict[str, Any]:
        """GET /api/v1/security/roles/{pk} — get a single role by ID."""
        role = await role_dao.find_by_id(pk)
        if role is None:
            raise ObjectNotFoundError("Role", pk)

        result = RoleResponse(
            id=role.id,
            name=role.name,
            user_ids=[u.id for u in (role.user or [])],
            permission_ids=[p.id for p in (role.permissions or [])],
            group_ids=[g.id for g in (getattr(role, "groups", None) or [])],
        )
        await event_logger.alog_with_context("role.show", object_ref=str(pk))
        return {"id": pk, "result": msgspec.to_builtins(result)}

    # ------------------------------------------------------------------
    # POST — create role (admin only)
    # ------------------------------------------------------------------
    @post(
        "/",
        guards=[require_permission("can_post", "Role")],
    )
    async def create(
        self,
        role_dao: Any,
        data: RolePostBody,
    ) -> dict[str, Any]:
        """POST /api/v1/security/roles/ — create a new role.

        Requires admin or ``can_write_Role`` permission.
        """
        if not data.name or not data.name.strip():
            raise SupersetValidationException("Role name is required")

        role = await role_dao.create({"name": data.name.strip()})
        await event_logger.alog_with_context(
            "role.create", extra={"role_name": data.name}
        )
        return {"id": role.id, "result": {"id": role.id, "name": role.name}}

    # ------------------------------------------------------------------
    # PUT — update role (admin only)
    # ------------------------------------------------------------------
    @put(
        "/{pk:int}",
        guards=[require_permission("can_put", "Role")],
    )
    async def update(
        self,
        role_dao: Any,
        pk: int,
        data: RolePutBody,
    ) -> dict[str, Any]:
        """PUT /api/v1/security/roles/{pk} — update a role.

        Requires admin or ``can_write_Role`` permission.
        """
        role = await role_dao.find_by_id(pk)
        if role is None:
            raise ObjectNotFoundError("Role", pk)

        if not data.name or not data.name.strip():
            raise SupersetValidationException("Role name is required")

        updated = await role_dao.update(role, {"name": data.name.strip()})
        await event_logger.alog_with_context("role.update", object_ref=str(pk))
        return {"id": pk, "result": {"id": updated.id, "name": updated.name}}

    # ------------------------------------------------------------------
    # DELETE — delete role (admin only)
    # ------------------------------------------------------------------
    @delete(
        "/{pk:int}",
        guards=[require_permission("can_delete", "Role")],
        status_code=200,
    )
    async def delete_single(
        self,
        role_dao: Any,
        pk: int,
    ) -> dict[str, str]:
        """DELETE /api/v1/security/roles/{pk} — delete a role.

        Requires admin or ``can_write_Role`` permission.
        """
        role = await role_dao.find_by_id(pk)
        if role is None:
            raise ObjectNotFoundError("Role", pk)

        await role_dao.delete(role)
        await event_logger.alog_with_context("role.delete", object_ref=str(pk))
        return {"message": "OK"}

    # ------------------------------------------------------------------
    # GET — role permissions
    # ------------------------------------------------------------------
    @get(
        "/{role_id:int}/permissions/",
        guards=[require_permission("can_list_role_permissions", "Role")],
    )
    async def get_permissions(
        self,
        role_dao: Any,
        role_id: int,
    ) -> dict[str, Any]:
        """GET /api/v1/security/roles/{role_id}/permissions/ — list role permissions."""
        role = await role_dao.find_by_id(role_id)
        if role is None:
            raise ObjectNotFoundError("Role", role_id)

        permission_views = await role_dao.get_permissions(role_id)

        result = [
            PermissionViewResponse(
                id=pv.id,
                permission_name=getattr(pv.permission, "name", None)
                if pv.permission
                else None,
                view_menu_name=getattr(pv.view_menu, "name", None)
                if pv.view_menu
                else None,
            )
            for pv in permission_views
        ]

        await event_logger.alog_with_context(
            "role.permissions", object_ref=str(role_id)
        )
        return msgspec.to_builtins(
            RolePermissionsResponse(
                result=result,
                count=len(result),
            )
        )

    # ------------------------------------------------------------------
    # POST /{role_id}/permissions — set role permissions
    # ------------------------------------------------------------------
    @post(
        "/{role_id:int}/permissions",
        guards=[require_permission("can_add_role_permissions", "Role")],
        status_code=200,
    )
    async def set_permissions(
        self,
        role_dao: Any,
        role_id: int,
        data: RolePermissionsPostBody,
    ) -> dict[str, Any]:
        """POST /api/v1/security/roles/{role_id}/permissions — replace role permissions.

        Body: {permission_view_menu_ids: [int, ...]}
        """
        result = await role_dao.set_permissions(role_id, data.permission_view_menu_ids)
        if result is None:
            raise ObjectNotFoundError("Role", role_id)

        await event_logger.alog_with_context(
            "role.set_permissions", object_ref=str(role_id)
        )
        return {
            "result": {
                "permission_view_menu_ids": data.permission_view_menu_ids,
            }
        }

    # ------------------------------------------------------------------
    # PUT /{role_id}/users — set role users
    # ------------------------------------------------------------------
    @put(
        "/{role_id:int}/users",
        guards=[require_permission("can_update_role_users", "Role")],
    )
    async def set_users(
        self,
        role_dao: Any,
        role_id: int,
        data: RoleUsersPutBody,
    ) -> dict[str, Any]:
        """PUT /api/v1/security/roles/{role_id}/users — replace role users.

        Body: {user_ids: [int, ...]}
        """
        result = await role_dao.set_users(role_id, data.user_ids)
        if result is None:
            raise ObjectNotFoundError("Role", role_id)
        if result == "not_found":
            raise ObjectNotFoundError("User", "some user_ids not found")

        await event_logger.alog_with_context("role.set_users", object_ref=str(role_id))
        return {"result": {"user_ids": data.user_ids}}

    # ------------------------------------------------------------------
    # PUT /{role_id}/groups — set role groups
    # ------------------------------------------------------------------
    @put(
        "/{role_id:int}/groups",
        guards=[require_permission("can_update_role_groups", "Role")],
    )
    async def set_groups(
        self,
        role_dao: Any,
        role_id: int,
        data: RoleGroupsPutBody,
    ) -> dict[str, Any]:
        """PUT /api/v1/security/roles/{role_id}/groups — replace role groups.

        Body: {group_ids: [int, ...]}
        """
        result = await role_dao.set_groups(role_id, data.group_ids)
        if result is None:
            raise ObjectNotFoundError("Role", role_id)
        if result == "not_found":
            raise ObjectNotFoundError("Group", "some group_ids not found")

        await event_logger.alog_with_context("role.set_groups", object_ref=str(role_id))
        return {"result": {"group_ids": data.group_ids}}

    # ------------------------------------------------------------------
    # GET /_info — metadata for frontend forms
    # ------------------------------------------------------------------
    @get(
        "/_info",
        guards=[require_permission("can_info", "Role")],
    )
    async def get_info(self) -> dict[str, Any]:
        """GET /api/v1/security/roles/_info — permissions, columns metadata."""
        return {
            "permissions": ["can_read", "can_write"],
            "add_columns": ["name"],
            "edit_columns": ["name"],
        }
