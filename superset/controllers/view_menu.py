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
"""ViewMenu (Resources) controller — full CRUD for FAB view menus (ab_view_menu).

Mirrors Flask-AppBuilder's ``ViewMenuApi`` which is a ``ModelRestApi``
with all default route methods enabled (get_list, get, info, post, put, delete).

Original: flask_appbuilder/security/sqla/apis/view_menu/api.py
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
from superset.providers import provide_view_menu_dao
from superset.schemas.security import (
    ViewMenuResponse,
    ViewMenusSearchResponse,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------


class ViewMenuPostBody(msgspec.Struct):
    """POST body for creating a view menu (resource).

    Matches FAB ``ViewMenuApi.add_columns = ["name"]``.
    """

    name: str


class ViewMenuPutBody(msgspec.Struct):
    """PUT body for updating a view menu (resource).

    Matches FAB ``ViewMenuApi.edit_columns = ["name"]``.
    """

    name: str


class ViewMenuController(Controller):
    """Full CRUD controller for FAB view menus / resources (ab_view_menu).

    Mirrors the original FAB ``ViewMenuApi(ModelRestApi)`` which exposes
    all default CRUD methods.

    Used by the frontend to manage resource names in the security
    permission model.
    """

    path = "/api/v1/security/resources"
    tags = ["Security Resources (View Menus)"]
    dependencies = {
        "vm_dao": Provide(provide_view_menu_dao, sync_to_thread=False),
        "rison_params": Provide(provide_rison_query),
    }

    # ------------------------------------------------------------------
    # GET / -- list view menus (paginated)
    # ------------------------------------------------------------------
    @get(
        "/",
        guards=[require_permission("can_read", "ViewMenu")],
    )
    async def get_list(
        self,
        vm_dao: Any,
        rison_params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """GET /api/v1/security/resources/ -- list all view menus.

        Supports Rison query parameters for pagination and ordering.
        Matches FAB ``ViewMenuApi.get_list`` with
        ``list_columns = ["id", "name"]`` and
        ``search_columns = ["id", "name"]``.
        """
        from superset.models.security import ViewMenu

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
            ViewMenu,
            rison_params,
        )

        vms, total = await vm_dao.search(
            filters=rison_filters if rison_filters else None,
            order_column=order_column,
            order_direction=order_direction,
            page=page,
            page_size=page_size,
        )

        result = [ViewMenuResponse(id=vm.id, name=vm.name) for vm in vms]

        event_logger.log("view_menu.list")

        return msgspec.to_builtins(
            ViewMenusSearchResponse(
                result=result,
                count=total,
                ids=[r.id for r in result],
            )
        )

    # ------------------------------------------------------------------
    # GET /{pk} -- single view menu
    # ------------------------------------------------------------------
    @get(
        "/{pk:int}",
        guards=[require_permission("can_read", "ViewMenu")],
    )
    async def get_single(
        self,
        vm_dao: Any,
        pk: int,
    ) -> dict[str, Any]:
        """GET /api/v1/security/resources/{pk} -- get single view menu.

        Matches FAB ``ViewMenuApi.get`` with
        ``show_columns = ["id", "name"]``.
        """
        vm = await vm_dao.find_by_id(pk)
        if vm is None:
            raise ObjectNotFoundError("ViewMenu", pk)

        result = ViewMenuResponse(id=vm.id, name=vm.name)
        event_logger.log("view_menu.show", object_ref=str(pk))
        return {"id": pk, "result": msgspec.to_builtins(result)}

    # ------------------------------------------------------------------
    # GET /_info -- metadata
    # ------------------------------------------------------------------
    @get(
        "/_info",
        guards=[require_permission("can_read", "ViewMenu")],
    )
    async def get_info(self) -> dict[str, Any]:
        """GET /api/v1/security/resources/_info -- metadata.

        Matches FAB ``ViewMenuApi.info`` response shape with
        ``add_columns = ["name"]``, ``edit_columns = ["name"]``.
        """
        return {
            "permissions": ["can_read", "can_write"],
            "add_columns": ["name"],
            "edit_columns": ["name"],
        }

    # ------------------------------------------------------------------
    # POST / -- create view menu
    # ------------------------------------------------------------------
    @post(
        "/",
        guards=[require_permission("can_write", "ViewMenu")],
        status_code=201,
    )
    async def create(
        self,
        vm_dao: Any,
        data: ViewMenuPostBody,
    ) -> dict[str, Any]:
        """POST /api/v1/security/resources/ -- create a new view menu.

        Mirrors FAB ``ViewMenuApi.post`` with
        ``add_columns = ["name"]``.

        Returns 201 with ``{id, result}`` on success.
        Raises database error (422) if name already exists (unique constraint).
        """
        try:
            vm = await vm_dao.create({"name": data.name})
        except Exception as exc:
            # Unique constraint violation -- matches FAB's DatabaseException handling
            raise SupersetValidationException(
                f"Database exception occurred: {exc}"
            ) from exc

        event_logger.log("view_menu.create", extra={"name": data.name})
        return {
            "id": vm.id,
            "result": {"name": vm.name},
        }

    # ------------------------------------------------------------------
    # PUT /{pk} -- update view menu
    # ------------------------------------------------------------------
    @put(
        "/{pk:int}",
        guards=[require_permission("can_write", "ViewMenu")],
    )
    async def update(
        self,
        vm_dao: Any,
        pk: int,
        data: ViewMenuPutBody,
    ) -> dict[str, Any]:
        """PUT /api/v1/security/resources/{pk} -- update a view menu.

        Mirrors FAB ``ViewMenuApi.put`` with
        ``edit_columns = ["name"]``.

        Returns 200 with ``{result}`` on success.
        Returns 404 if not found.
        Returns 422 on database error (e.g., duplicate name).
        """
        vm = await vm_dao.find_by_id(pk)
        if vm is None:
            raise ObjectNotFoundError("ViewMenu", pk)

        try:
            updated = await vm_dao.update(vm, {"name": data.name})
        except Exception as exc:
            raise SupersetValidationException(
                f"Database exception occurred: {exc}"
            ) from exc

        event_logger.log("view_menu.update", object_ref=str(pk))
        return {"result": {"name": updated.name}}

    # ------------------------------------------------------------------
    # DELETE /{pk} -- delete view menu
    # ------------------------------------------------------------------
    @delete(
        "/{pk:int}",
        guards=[require_permission("can_write", "ViewMenu")],
        status_code=200,
    )
    async def delete_single(
        self,
        vm_dao: Any,
        pk: int,
    ) -> dict[str, str]:
        """DELETE /api/v1/security/resources/{pk} -- delete a view menu.

        Mirrors FAB ``ViewMenuApi.delete``.

        Returns 200 with ``{message: "OK"}`` on success.
        Returns 404 if not found.
        Returns 422 on database error (e.g., FK constraint).
        """
        vm = await vm_dao.find_by_id(pk)
        if vm is None:
            raise ObjectNotFoundError("ViewMenu", pk)

        try:
            await vm_dao.delete(vm)
        except Exception as exc:
            raise SupersetValidationException(
                f"Database exception occurred: {exc}"
            ) from exc

        event_logger.log("view_menu.delete", object_ref=str(pk))
        return {"message": "OK"}
