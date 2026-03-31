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
"""PermissionView controller — list permissions-resources for FAB security."""

from __future__ import annotations

import logging
from typing import Any

import msgspec
from litestar import Controller, get
from litestar.di import Provide

from superset.controllers.base import extract_pagination
from superset.events import event_logger
from superset.exceptions import ObjectNotFoundError
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


def _pv_to_response(pv: Any) -> PermissionViewResponse:
    """Convert a PermissionView model instance to response schema."""
    perm_ref = (
        PermissionRef(name=pv.permission.name) if pv.permission else None
    )
    vm_ref = ViewMenuRef(name=pv.view_menu.name) if pv.view_menu else None
    return PermissionViewResponse(
        id=pv.id,
        permission=perm_ref,
        view_menu=vm_ref,
    )


class PermissionViewController(Controller):
    """Read-only controller for FAB permission-view entries.

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
        guards=[require_permission("can_read", "PermissionViewMenu")],
    )
    async def get_list(
        self,
        pv_dao: Any,
        rison_params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """GET /api/v1/security/permissions-resources/ — list all permission-views."""
        params = rison_params or {}
        page, page_size = extract_pagination(rison_params)
        order_column = params.get("order_column", "id")
        order_direction = params.get("order_direction", "asc")

        if order_column not in ("id",):
            order_column = "id"

        pvs, total = await pv_dao.search(
            order_column=order_column,
            order_direction=order_direction,
            page=page,
            page_size=page_size,
        )

        result = [_pv_to_response(pv) for pv in pvs]
        event_logger.log("permission_view.list")
        return msgspec.to_builtins(
            PermissionViewsSearchResponse(result=result, count=total)
        )

    # ------------------------------------------------------------------
    # GET /{pk} — single permission-view
    # ------------------------------------------------------------------
    @get(
        "/{pk:int}",
        guards=[require_permission("can_read", "PermissionViewMenu")],
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
        event_logger.log("permission_view.show", object_ref=str(pk))
        return {"id": pk, "result": msgspec.to_builtins(result)}

    # ------------------------------------------------------------------
    # GET /_info — metadata
    # ------------------------------------------------------------------
    @get(
        "/_info",
        guards=[require_permission("can_read", "PermissionViewMenu")],
    )
    async def get_info(self) -> dict[str, Any]:
        """GET /api/v1/security/permissions-resources/_info — metadata."""
        return {
            "permissions": ["can_read"],
            "add_columns": ["permission_id", "view_menu_id"],
            "edit_columns": ["permission_id", "view_menu_id"],
        }
