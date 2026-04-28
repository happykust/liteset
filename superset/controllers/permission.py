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
"""Permission controller — read-only endpoints for FAB permissions (ab_permission).

Mirrors Flask-AppBuilder's ``PermissionApi`` which is a ``ModelRestApi``
with ``include_route_methods = {"info", "get", "get_list"}``.

Original: flask_appbuilder/security/sqla/apis/permission/api.py
"""

from __future__ import annotations

import logging
from typing import Any

import msgspec
from litestar import Controller, get
from litestar.di import Provide

from superset.controllers.base import build_rison_query_params, extract_pagination
from superset.events import event_logger
from superset.exceptions import ObjectNotFoundError
from superset.guards.rbac import require_permission
from superset.params.rison import provide_rison_query
from superset.providers import provide_permission_dao
from superset.schemas.security import (
    PermissionResponse,
    PermissionsSearchResponse,
)

logger = logging.getLogger(__name__)


class PermissionController(Controller):
    """Read-only controller for FAB permissions (ab_permission table).

    Provides list, show, and info endpoints matching the original FAB
    ``PermissionApi(ModelRestApi)`` which restricts to
    ``include_route_methods = {"info", "get", "get_list"}``.

    Used by the frontend to populate permission dropdowns in the
    role edit modal.
    """

    path = "/api/v1/security/permissions"
    tags = ["Security Permissions"]
    dependencies = {
        "perm_dao": Provide(provide_permission_dao, sync_to_thread=False),
        "rison_params": Provide(provide_rison_query),
    }

    # ------------------------------------------------------------------
    # GET / -- list permissions (paginated)
    # ------------------------------------------------------------------
    @get(
        "/",
        guards=[require_permission("can_read", "Permission")],
    )
    async def get_list(
        self,
        perm_dao: Any,
        rison_params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """GET /api/v1/security/permissions/ -- list all permissions.

        Supports Rison query parameters for pagination and ordering.
        Matches FAB ``PermissionApi.get_list`` with
        ``list_columns = ["id", "name"]`` and
        ``search_columns = ["id", "name"]``.
        """
        from superset.models.security import Permission

        params = rison_params or {}
        page, page_size = extract_pagination(rison_params)
        order_column = params.get("order_column", "id")
        order_direction = params.get("order_direction", "asc")

        # Validate order_column -- only allow columns from list_columns
        if order_column not in ("id", "name"):
            order_column = "id"

        # Build filters using the shared helper (supports all FAB operators
        # on search_columns = ["id", "name"])
        rison_filters, _order_by, _page, _page_size = build_rison_query_params(
            Permission,
            rison_params,
        )

        perms, total = await perm_dao.search(
            filters=rison_filters if rison_filters else None,
            order_column=order_column,
            order_direction=order_direction,
            page=page,
            page_size=page_size,
        )

        result = [PermissionResponse(id=p.id, name=p.name) for p in perms]

        await event_logger.alog_with_context("permission.list")

        return msgspec.to_builtins(
            PermissionsSearchResponse(
                result=result,
                count=total,
                ids=[r.id for r in result],
            )
        )

    # ------------------------------------------------------------------
    # GET /{pk} -- single permission
    # ------------------------------------------------------------------
    @get(
        "/{pk:int}",
        guards=[require_permission("can_read", "Permission")],
    )
    async def get_single(
        self,
        perm_dao: Any,
        pk: int,
    ) -> dict[str, Any]:
        """GET /api/v1/security/permissions/{pk} -- get single permission.

        Matches FAB ``PermissionApi.get`` with
        ``show_columns = ["id", "name"]``.
        """
        perm = await perm_dao.find_by_id(pk)
        if perm is None:
            raise ObjectNotFoundError("Permission", pk)

        result = PermissionResponse(id=perm.id, name=perm.name)
        await event_logger.alog_with_context("permission.show", object_ref=str(pk))
        return {"id": pk, "result": msgspec.to_builtins(result)}

    # ------------------------------------------------------------------
    # GET /_info -- metadata
    # ------------------------------------------------------------------
    @get(
        "/_info",
        guards=[require_permission("can_read", "Permission")],
    )
    async def get_info(self) -> dict[str, Any]:
        """GET /api/v1/security/permissions/_info -- metadata.

        Matches FAB ``PermissionApi.info`` — read-only
        (``include_route_methods = {"info", "get", "get_list"}``).
        No add/edit columns since POST/PUT/DELETE are not exposed.
        """
        return {
            "permissions": ["can_read"],
            "add_columns": [],
            "edit_columns": [],
        }
