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
"""Theme controller — CRUD + system default management."""

from __future__ import annotations

from typing import Any

from litestar import Controller, delete, get, post, put
from litestar.di import Provide

from superset.commands.theme import (
    CreateThemeCommand,
    DeleteThemeCommand,
    SetSystemDefaultCommand,
    UnsetSystemDefaultCommand,
    UpdateThemeCommand,
)
from superset.controllers.base import (
    extract_pagination,
    serialize_list_response,
)
from superset.events import event_logger
from superset.exceptions import ObjectNotFoundError
from superset.guards.rbac import require_permission
from superset.params.rison import provide_rison_query
from superset.providers import provide_theme_dao
from superset.schemas.theme import ThemePostSchema, ThemePutSchema
from superset.typing import CRUDDAOProtocol, UserProtocol
from superset.utils import filter_unset


class ThemeController(Controller):
    path = "/api/v1/theme"
    tags = ["Themes"]
    dependencies = {
        "dao": Provide(provide_theme_dao, sync_to_thread=False),
        "rison_params": Provide(provide_rison_query),
    }

    # ------------------------------------------------------------------
    # GET — list themes
    # ------------------------------------------------------------------
    @get(
        "/",
        guards=[require_permission("can_read", "Theme")],
    )
    async def get_list(
        self,
        dao: CRUDDAOProtocol,
        rison_params: dict[str, Any] | None,
        current_user: UserProtocol,
    ) -> dict[str, Any]:
        """GET /api/v1/theme/ — list themes with optional pagination."""
        page, page_size = extract_pagination(rison_params)
        themes = await dao.find_all(page=page, page_size=page_size)
        total = await dao.count()
        event_logger.log("theme.list", user_id=current_user.id)
        return serialize_list_response(
            themes,
            total,
            ["id", "theme_name", "css", "json_metadata", "description",
             "is_system_default"],
        )

    # ------------------------------------------------------------------
    # GET — single theme
    # ------------------------------------------------------------------
    @get(
        "/{pk:int}",
        guards=[require_permission("can_read", "Theme")],
    )
    async def get_single(
        self,
        pk: int,
        dao: CRUDDAOProtocol,
        current_user: UserProtocol,
    ) -> dict[str, Any]:
        """GET /api/v1/theme/{pk} — get a single theme."""
        theme = await dao.find_by_id(pk)
        if not theme:
            raise ObjectNotFoundError("Theme", pk)
        event_logger.log("theme.get", object_ref=str(pk), user_id=current_user.id)
        return {
            "id": theme.id,
            "result": {
                "id": theme.id,
                "theme_name": theme.theme_name,
                "css": getattr(theme, "css", ""),
                "json_metadata": getattr(theme, "json_metadata", ""),
                "description": getattr(theme, "description", ""),
                "is_system_default": getattr(theme, "is_system_default", False),
            },
        }

    # ------------------------------------------------------------------
    # POST — create theme
    # ------------------------------------------------------------------
    @post(
        "/",
        guards=[require_permission("can_write", "Theme")],
    )
    async def create(
        self,
        dao: CRUDDAOProtocol,
        data: ThemePostSchema,
        current_user: UserProtocol,
    ) -> dict[str, Any]:
        """POST /api/v1/theme/ — create a new theme."""
        from msgspec import structs as _structs

        payload = _structs.asdict(data)
        cmd = CreateThemeCommand(dao=dao, data=payload)
        theme = await cmd.execute()
        event_logger.log("theme.create", object_ref=str(theme.id),
                         user_id=current_user.id)
        return {"id": theme.id, "result": {"id": theme.id}}

    # ------------------------------------------------------------------
    # PUT — update theme
    # ------------------------------------------------------------------
    @put(
        "/{pk:int}",
        guards=[require_permission("can_write", "Theme")],
    )
    async def update(
        self,
        pk: int,
        dao: CRUDDAOProtocol,
        data: ThemePutSchema,
        current_user: UserProtocol,
    ) -> dict[str, Any]:
        """PUT /api/v1/theme/{pk} — update a theme."""
        from msgspec import structs as _structs

        payload = filter_unset(_structs.asdict(data))
        cmd = UpdateThemeCommand(dao=dao, pk=pk, data=payload)
        theme = await cmd.execute()
        event_logger.log("theme.update", object_ref=str(pk),
                         user_id=current_user.id)
        return {"id": theme.id, "result": {"id": theme.id}}

    # ------------------------------------------------------------------
    # DELETE — delete theme
    # ------------------------------------------------------------------
    @delete(
        "/{pk:int}",
        guards=[require_permission("can_write", "Theme")],
        status_code=200,
    )
    async def delete_theme(
        self,
        pk: int,
        dao: CRUDDAOProtocol,
        current_user: UserProtocol,
    ) -> dict[str, Any]:
        """DELETE /api/v1/theme/{pk} — delete a theme."""
        cmd = DeleteThemeCommand(dao=dao, pk=pk)
        await cmd.execute()
        event_logger.log("theme.delete", object_ref=str(pk),
                         user_id=current_user.id)
        return {"message": "OK"}

    # ------------------------------------------------------------------
    # GET — system default theme
    # ------------------------------------------------------------------
    @get(
        "/system_default/",
        guards=[require_permission("can_read", "Theme")],
    )
    async def get_system_default(
        self,
        dao: Any,
        current_user: UserProtocol,
    ) -> dict[str, Any]:
        """GET /api/v1/theme/system_default/ — get the system default theme."""
        theme = await dao.find_system_default()
        if not theme:
            raise ObjectNotFoundError("Theme", "system_default")
        event_logger.log("theme.get_system_default", user_id=current_user.id)
        return {
            "id": theme.id,
            "result": {
                "id": theme.id,
                "theme_name": theme.theme_name,
                "css": getattr(theme, "css", ""),
                "json_metadata": getattr(theme, "json_metadata", ""),
                "description": getattr(theme, "description", ""),
                "is_system_default": getattr(theme, "is_system_default", False),
            },
        }

    # ------------------------------------------------------------------
    # PUT — set system default
    # ------------------------------------------------------------------
    @put(
        "/{pk:int}/set_system_default",
        guards=[require_permission("can_write", "Theme")],
    )
    async def set_system_default(
        self,
        pk: int,
        dao: Any,
        current_user: UserProtocol,
    ) -> dict[str, Any]:
        """PUT /api/v1/theme/{pk}/set_system_default — set as system default."""
        cmd = SetSystemDefaultCommand(dao=dao, pk=pk)
        theme = await cmd.execute()
        event_logger.log("theme.set_system_default", object_ref=str(pk),
                         user_id=current_user.id)
        return {"id": theme.id, "result": {"id": theme.id}}

    # ------------------------------------------------------------------
    # DELETE — unset system default
    # ------------------------------------------------------------------
    @delete(
        "/unset_system_default",
        guards=[require_permission("can_write", "Theme")],
        status_code=200,
    )
    async def unset_system_default(
        self,
        dao: Any,
        current_user: UserProtocol,
    ) -> dict[str, Any]:
        """DELETE /api/v1/theme/unset_system_default — remove system default."""
        cmd = UnsetSystemDefaultCommand(dao=dao)
        await cmd.execute()
        event_logger.log("theme.unset_system_default", user_id=current_user.id)
        return {"message": "OK"}
