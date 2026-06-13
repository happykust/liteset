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
"""PermissionView controller — CRUD for upstream permission-view mappings.

Mirrors the upstream ``PermissionViewMenuApi`` which is a
``ModelRestApi`` with all default route methods enabled.

Original: the upstream permission-view-menu security API.
"""

from __future__ import annotations

import logging
from typing import Any

import msgspec
from litestar import Controller, delete, get, post, put
from litestar.di import Provide

from superset.controllers.base import build_rison_query_params, extract_pagination
from superset.events import event_logger
from superset.exceptions import ObjectNotFoundError, SupersetValidationException
from superset.guards.rbac import require_permission
from superset.params.rison import provide_rison_query
from superset.providers import provide_permission_view_dao
from superset.schemas.security import (
    PermissionRef,
    PermissionViewResponse,
    PermissionViewsSearchResponse,
    ViewMenuRef,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------


class PermissionViewPostBody(msgspec.Struct):
    """POST body for creating a permission-view mapping.

    Matches the upstream ``PermissionViewMenuApi.add_columns``.
    """

    permission_id: int
    view_menu_id: int


class PermissionViewPutBody(msgspec.Struct):
    """PUT body for updating a permission-view mapping.

    Matches the upstream ``PermissionViewMenuApi.edit_columns``.
    """

    permission_id: int
    view_menu_id: int


def _pv_to_response(pv: Any) -> PermissionViewResponse:
    """Convert a PermissionView model instance to response schema."""
    perm_ref = PermissionRef(name=pv.permission.name) if pv.permission else None
    vm_ref = ViewMenuRef(name=pv.view_menu.name) if pv.view_menu else None
    return PermissionViewResponse(
        id=pv.id,
        permission=perm_ref,
        view_menu=vm_ref,
    )


class PermissionViewController(Controller):
    """Full CRUD controller for upstream permission-view entries
    (ab_permission_view).

    Mirrors the original upstream ``PermissionViewMenuApi(ModelRestApi)`` which
    exposes all default CRUD methods.

    Used by the frontend to populate permission dropdowns in the
    role edit modal.
    """

    path = "/api/v1/security/permissions-resources"
    tags = ["Security Permissions Resources"]
    dependencies = {
        "pv_dao": Provide(provide_permission_view_dao, sync_to_thread=False),
        "rison_params": Provide(provide_rison_query),
    }

    # ------------------------------------------------------------------
    # GET / — list permission-views (paginated)
    # ------------------------------------------------------------------
    @get(
        "/",
        guards=[require_permission("can_get", "PermissionViewMenu")],
    )
    async def get_list(
        self,
        pv_dao: Any,
        rison_params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """GET /api/v1/security/permissions-resources/ — list all permission-views.

        Supports Rison query parameters for pagination, ordering, and filtering.
        Matches the upstream ``PermissionViewMenuApi.get_list`` with
        ``list_columns = ["id", "permission.name", "view_menu.name"]``.

        Searchable columns: ``id``, ``permission_id``, ``view_menu_id``
        (matches the FK columns used by the frontend to filter permission-views
        when editing roles).
        """
        from superset.models.security import PermissionView

        params = rison_params or {}
        page, page_size = extract_pagination(rison_params)
        order_column = params.get("order_column", "id")
        order_direction = params.get("order_direction", "asc")

        # Validate order_column -- only allow actual model columns
        if order_column not in ("id", "permission_id", "view_menu_id"):
            order_column = "id"

        # Build filters using the shared helper (supports all upstream operators)
        rison_filters, _order_by, _page, _page_size = build_rison_query_params(
            PermissionView,
            rison_params,
        )

        pvs, total = await pv_dao.search(
            filters=rison_filters if rison_filters else None,
            order_column=order_column,
            order_direction=order_direction,
            page=page,
            page_size=page_size,
        )

        result = [_pv_to_response(pv) for pv in pvs]
        await event_logger.alog_with_context("permission_view.list")
        return msgspec.to_builtins(
            PermissionViewsSearchResponse(
                result=result,
                count=total,
                ids=[r.id for r in result],
            )
        )

    # ------------------------------------------------------------------
    # GET /{pk} — single permission-view
    # ------------------------------------------------------------------
    @get(
        "/{pk:int}",
        guards=[require_permission("can_get", "PermissionViewMenu")],
    )
    async def get_single(
        self,
        pv_dao: Any,
        pk: int,
    ) -> dict[str, Any]:
        """GET /api/v1/security/permissions-resources/{pk} — get single entry."""
        pv = await pv_dao.find_by_id(pk)
        if pv is None:
            raise ObjectNotFoundError("PermissionView", pk)

        result = _pv_to_response(pv)
        await event_logger.alog_with_context("permission_view.show", object_ref=str(pk))
        return {"id": pk, "result": msgspec.to_builtins(result)}

    # ------------------------------------------------------------------
    # GET /_info — metadata
    # ------------------------------------------------------------------
    @get(
        "/_info",
        guards=[require_permission("can_info", "PermissionViewMenu")],
    )
    async def get_info(self) -> dict[str, Any]:
        """GET /api/v1/security/permissions-resources/_info — metadata."""
        return {
            "permissions": ["can_read", "can_write"],
            "add_columns": ["permission_id", "view_menu_id"],
            "edit_columns": ["permission_id", "view_menu_id"],
        }

    # ------------------------------------------------------------------
    # POST / — create permission-view mapping
    # ------------------------------------------------------------------
    @post(
        "/",
        guards=[require_permission("can_post", "PermissionViewMenu")],
        status_code=201,
    )
    async def create(
        self,
        pv_dao: Any,
        data: PermissionViewPostBody,
    ) -> dict[str, Any]:
        """POST /api/v1/security/permissions-resources/ — create mapping.

        Mirrors the upstream ``PermissionViewMenuApi.post`` with
        ``add_columns = ["permission_id", "view_menu_id"]``.

        Returns 201 with ``{id, result}`` on success.
        Returns 422 on database error (e.g., duplicate pair).
        """
        try:
            pv = await pv_dao.create(
                {
                    "permission_id": data.permission_id,
                    "view_menu_id": data.view_menu_id,
                }
            )
        except Exception as exc:
            # Unique constraint violation or FK violation
            raise SupersetValidationException(
                f"Database exception occurred: {exc}"
            ) from exc

        await event_logger.alog_with_context("permission_view.create")
        return {
            "id": pv.id,
            "result": {
                "permission_id": pv.permission_id,
                "view_menu_id": pv.view_menu_id,
            },
        }

    # ------------------------------------------------------------------
    # PUT /{pk} — update permission-view mapping
    # ------------------------------------------------------------------
    @put(
        "/{pk:int}",
        guards=[require_permission("can_put", "PermissionViewMenu")],
    )
    async def update(
        self,
        pv_dao: Any,
        pk: int,
        data: PermissionViewPutBody,
    ) -> dict[str, Any]:
        """PUT /api/v1/security/permissions-resources/{pk} — update mapping.

        Mirrors the upstream ``PermissionViewMenuApi.put`` with
        ``edit_columns = ["permission_id", "view_menu_id"]``.

        Returns 200 with ``{result}`` on success.
        Returns 404 if not found.
        Returns 422 on database error.
        """
        pv = await pv_dao.find_by_id(pk)
        if pv is None:
            raise ObjectNotFoundError("PermissionView", pk)

        try:
            updated = await pv_dao.update(
                pv,
                {
                    "permission_id": data.permission_id,
                    "view_menu_id": data.view_menu_id,
                },
            )
        except Exception as exc:
            raise SupersetValidationException(
                f"Database exception occurred: {exc}"
            ) from exc

        await event_logger.alog_with_context(
            "permission_view.update", object_ref=str(pk)
        )
        return {
            "result": {
                "permission_id": updated.permission_id,
                "view_menu_id": updated.view_menu_id,
            },
        }

    # ------------------------------------------------------------------
    # DELETE /{pk} — delete permission-view mapping
    # ------------------------------------------------------------------
    @delete(
        "/{pk:int}",
        guards=[require_permission("can_delete", "PermissionViewMenu")],
        status_code=200,
    )
    async def delete_single(
        self,
        pv_dao: Any,
        pk: int,
    ) -> dict[str, str]:
        """DELETE /api/v1/security/permissions-resources/{pk} — delete mapping.

        Mirrors the upstream ``PermissionViewMenuApi.delete``.

        Returns 200 with ``{message: "OK"}`` on success.
        Returns 404 if not found.
        Returns 422 on database error (e.g., FK constraint from roles).
        """
        pv = await pv_dao.find_by_id(pk)
        if pv is None:
            raise ObjectNotFoundError("PermissionView", pk)

        try:
            await pv_dao.delete(pv)
        except Exception as exc:
            raise SupersetValidationException(
                f"Database exception occurred: {exc}"
            ) from exc

        await event_logger.alog_with_context(
            "permission_view.delete", object_ref=str(pk)
        )
        return {"message": "OK"}
