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
"""Group controller — full CRUD for FAB groups (ab_group table)."""

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
from superset.providers import provide_group_dao
from superset.schemas.security import (
    GroupResponse,
    GroupRoleRef,
    GroupsSearchResponse,
    GroupUserRef,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------


class GroupPostBody(msgspec.Struct):
    """POST body for creating a group."""

    name: str
    label: str | None = None
    description: str | None = None
    roles: list[int] = []
    users: list[int] = []


class GroupPutBody(msgspec.Struct):
    """PUT body for updating a group."""

    name: str | None = None
    label: str | None = None
    description: str | None = None
    roles: list[int] | None = None
    users: list[int] | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _group_to_response(group: Any) -> GroupResponse:
    """Convert a Group model instance to a GroupResponse schema."""
    roles = [
        GroupRoleRef(id=r.id, name=r.name)
        for r in (getattr(group, "roles_", None) or [])
    ]
    users = [
        GroupUserRef(id=u.id, username=u.username)
        for u in (getattr(group, "users", None) or [])
    ]
    return GroupResponse(
        id=group.id,
        name=group.name,
        label=group.label,
        description=group.description,
        roles=roles,
        users=users,
    )


def _build_group_filters(rison_params: dict[str, Any] | None) -> list[Any]:
    """Build SQLAlchemy filter expressions from Rison filter list."""
    from superset.models.security import Group
    from superset.utils import escape_like

    filters: list[Any] = []
    if not rison_params:
        return filters

    allowed = {
        "name": Group.name,
        "label": Group.label,
        "description": Group.description,
    }

    for f in rison_params.get("filters", []):
        col_name = f.get("col")
        opr = f.get("opr", "eq")
        value = f.get("value")
        col = allowed.get(col_name)
        if col is None:
            continue

        if opr == "eq":
            filters.append(col == value)
        elif opr == "neq":
            filters.append(col != value)
        elif opr == "sw":
            filters.append(col.ilike(f"{escape_like(str(value))}%"))
        elif opr == "ct":
            filters.append(col.ilike(f"%{escape_like(str(value))}%"))

    return filters


# ---------------------------------------------------------------------------
# Group CRUD Controller
# ---------------------------------------------------------------------------


class GroupController(Controller):
    """Full CRUD controller for FAB groups."""

    path = "/api/v1/security/groups"
    tags = ["Security Groups"]
    dependencies = {
        "group_dao": Provide(provide_group_dao, sync_to_thread=False),
        "rison_params": Provide(provide_rison_query),
    }

    # ------------------------------------------------------------------
    # GET / — list groups (paginated)
    # ------------------------------------------------------------------
    @get(
        "/",
        guards=[require_permission("can_get", "Group")],
    )
    async def get_list(
        self,
        group_dao: Any,
        rison_params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """GET /api/v1/security/groups/ — list groups with Rison pagination."""
        params = rison_params or {}
        page, page_size = extract_pagination(rison_params)
        order_column = params.get("order_column", "id")
        order_direction = params.get("order_direction", "asc")

        if order_column not in ("id", "name", "label"):
            order_column = "id"

        filters = _build_group_filters(rison_params)

        groups, total = await group_dao.search(
            filters=filters,
            order_column=order_column,
            order_direction=order_direction,
            page=page,
            page_size=page_size,
        )

        result = [_group_to_response(g) for g in groups]
        await event_logger.alog_with_context("group.list")
        return msgspec.to_builtins(GroupsSearchResponse(result=result, count=total))

    # ------------------------------------------------------------------
    # GET /{pk} — single group
    # ------------------------------------------------------------------
    @get(
        "/{pk:int}",
        guards=[require_permission("can_get", "Group")],
    )
    async def get_single(
        self,
        group_dao: Any,
        pk: int,
    ) -> dict[str, Any]:
        """GET /api/v1/security/groups/{pk} — get a single group by ID."""
        group = await group_dao.find_by_id(pk)
        if group is None:
            raise ObjectNotFoundError("Group", pk)

        result = _group_to_response(group)
        await event_logger.alog_with_context("group.show", object_ref=str(pk))
        return {"id": pk, "result": msgspec.to_builtins(result)}

    # ------------------------------------------------------------------
    # POST / — create group
    # ------------------------------------------------------------------
    @post(
        "/",
        guards=[require_permission("can_post", "Group")],
        status_code=201,
    )
    async def create(
        self,
        group_dao: Any,
        data: GroupPostBody,
    ) -> dict[str, Any]:
        """POST /api/v1/security/groups/ — create a new group."""
        if not data.name or not data.name.strip():
            raise SupersetValidationException("Group name is required")

        attrs: dict[str, Any] = {
            "name": data.name.strip(),
            "role_ids": data.roles,
            "user_ids": data.users,
        }
        if data.label is not None:
            attrs["label"] = data.label.strip()
        if data.description is not None:
            attrs["description"] = data.description.strip()

        group = await group_dao.create(attrs)
        await event_logger.alog_with_context(
            "group.create", extra={"group_name": data.name}
        )
        return {
            "id": group.id,
            "result": msgspec.to_builtins(_group_to_response(group)),
        }

    # ------------------------------------------------------------------
    # PUT /{pk} — update group
    # ------------------------------------------------------------------
    @put(
        "/{pk:int}",
        guards=[require_permission("can_put", "Group")],
    )
    async def update(
        self,
        group_dao: Any,
        pk: int,
        data: GroupPutBody,
    ) -> dict[str, Any]:
        """PUT /api/v1/security/groups/{pk} — update a group."""
        group = await group_dao.find_by_id(pk)
        if group is None:
            raise ObjectNotFoundError("Group", pk)

        attrs: dict[str, Any] = {}
        if data.name is not None:
            if not data.name.strip():
                raise SupersetValidationException("Group name cannot be empty")
            attrs["name"] = data.name.strip()
        if data.label is not None:
            attrs["label"] = data.label.strip()
        if data.description is not None:
            attrs["description"] = data.description.strip()
        if data.roles is not None:
            attrs["role_ids"] = data.roles
        if data.users is not None:
            attrs["user_ids"] = data.users

        updated = await group_dao.update(group, attrs)
        await event_logger.alog_with_context("group.update", object_ref=str(pk))
        return {
            "id": pk,
            "result": msgspec.to_builtins(_group_to_response(updated)),
        }

    # ------------------------------------------------------------------
    # DELETE /{pk} — delete group
    # ------------------------------------------------------------------
    @delete(
        "/{pk:int}",
        guards=[require_permission("can_delete", "Group")],
        status_code=200,
    )
    async def delete_single(
        self,
        group_dao: Any,
        pk: int,
    ) -> dict[str, str]:
        """DELETE /api/v1/security/groups/{pk} — delete a group."""
        group = await group_dao.find_by_id(pk)
        if group is None:
            raise ObjectNotFoundError("Group", pk)

        await group_dao.delete(group)
        await event_logger.alog_with_context("group.delete", object_ref=str(pk))
        return {"message": "OK"}

    # ------------------------------------------------------------------
    # GET /_info — metadata for frontend forms
    # ------------------------------------------------------------------
    @get(
        "/_info",
        guards=[require_permission("can_info", "Group")],
    )
    async def get_info(self) -> dict[str, Any]:
        """GET /api/v1/security/groups/_info — permissions, columns metadata."""
        add_columns = ["name", "label", "description", "roles", "users"]
        return {
            "permissions": ["can_read", "can_write"],
            "add_columns": add_columns,
            "edit_columns": add_columns,
        }
