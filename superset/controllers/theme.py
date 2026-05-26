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
# mypy: ignore-errors
"""Theme controller — CRUD + system default management."""

from __future__ import annotations

from typing import Any

from litestar import Controller, delete, get, post, put
from litestar.datastructures import State, UploadFile
from litestar.di import Provide
from litestar.enums import RequestEncodingType
from litestar.params import Body

from superset.commands.theme import (
    BulkDeleteThemeCommand,
    CreateThemeCommand,
    DeleteThemeCommand,
    ExportThemesCommand,
    ImportThemesCommand,
    SetSystemDarkCommand,
    SetSystemDefaultCommand,
    UnsetSystemDarkCommand,
    UnsetSystemDefaultCommand,
    UpdateThemeCommand,
)
from superset.controllers.base import (
    extract_ids_required,
    extract_pagination,
    get_info_payload,
    get_related_payload,
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


def _guard_system_theme_admin(user: UserProtocol, state: State) -> None:
    """Enforce the system-theme admin + feature-flag gate.

    Mirrors ``superset_old/themes/api.py`` system-theme handlers
    (``set_system_default``/``set_system_dark``/``unset_system_default``/
    ``unset_system_dark``) which first require ``security_manager.is_admin()``
    (else 403 "Only administrators can set system themes") **and** the
    ``ENABLE_UI_THEME_ADMINISTRATION`` config flag (else 403 "UI theme
    administration is not enabled").
    """
    from superset.exceptions import ForbiddenError

    settings = getattr(state, "settings", None)
    admin_role_name = getattr(settings, "auth_role_admin", "Admin")
    user_roles = getattr(user, "roles", [])
    is_admin = any(
        getattr(role, "name", "") == admin_role_name for role in user_roles
    )
    if not is_admin:
        raise ForbiddenError(message="Only administrators can set system themes")
    if not getattr(settings, "enable_ui_theme_administration", False):
        raise ForbiddenError(message="UI theme administration is not enabled")


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
        await event_logger.alog_with_context("theme.list", user_id=current_user.id)
        return serialize_list_response(
            themes,
            total,
            [
                "id",
                "theme_name",
                "css",
                "json_metadata",
                "description",
                "is_system_default",
            ],
            list_title="List Theme",
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
        """GET /api/v1/theme/{pk} — get a single theme.

        Returns the actual ``Theme`` columns (``theme_name`` +
        ``json_data`` + the system flags). The legacy ``css`` /
        ``json_metadata`` / ``description`` keys are kept for frontend
        backwards-compat but read empty.
        """
        theme = await dao.find_by_id(pk)
        if not theme:
            raise ObjectNotFoundError("Theme", pk)
        await event_logger.alog_with_context(
            "theme.get", object_ref=str(pk), user_id=current_user.id
        )
        return {
            "id": theme.id,
            "result": {
                "id": theme.id,
                "theme_name": theme.theme_name,
                "json_data": getattr(theme, "json_data", "") or "",
                "css": getattr(theme, "css", ""),
                "json_metadata": getattr(theme, "json_metadata", ""),
                "description": getattr(theme, "description", ""),
                "is_system": getattr(theme, "is_system", False),
                "is_system_default": getattr(theme, "is_system_default", False),
                "is_system_dark": getattr(theme, "is_system_dark", False),
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
        cmd = CreateThemeCommand(dao=dao, data=payload)  # type: ignore[arg-type]
        theme = await cmd.execute()
        await event_logger.alog_with_context(
            "theme.create", object_ref=str(theme.id), user_id=current_user.id
        )
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
        cmd = UpdateThemeCommand(dao=dao, pk=pk, data=payload)  # type: ignore[arg-type]
        theme = await cmd.execute()
        await event_logger.alog_with_context(
            "theme.update", object_ref=str(pk), user_id=current_user.id
        )
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
        cmd = DeleteThemeCommand(dao=dao, pk=pk)  # type: ignore[arg-type]
        await cmd.execute()
        await event_logger.alog_with_context(
            "theme.delete", object_ref=str(pk), user_id=current_user.id
        )
        return {"message": "OK"}

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
        state: State,
    ) -> dict[str, Any]:
        """PUT /api/v1/theme/{pk}/set_system_default — set as system default."""
        _guard_system_theme_admin(current_user, state)
        cmd = SetSystemDefaultCommand(dao=dao, pk=pk)
        theme = await cmd.execute()
        await event_logger.alog_with_context(
            "theme.set_system_default", object_ref=str(pk), user_id=current_user.id
        )
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
        state: State,
    ) -> dict[str, Any]:
        """DELETE /api/v1/theme/unset_system_default — remove system default."""
        _guard_system_theme_admin(current_user, state)
        cmd = UnsetSystemDefaultCommand(dao=dao)
        await cmd.execute()
        await event_logger.alog_with_context(
            "theme.unset_system_default", user_id=current_user.id
        )
        return {"message": "OK"}

    @delete(
        "/",
        guards=[require_permission("can_write", "Theme")],
        status_code=200,
    )
    async def bulk_delete(
        self,
        dao: CRUDDAOProtocol,
        rison_params: list[int] | dict[str, Any] | None,
        current_user: UserProtocol,
    ) -> dict[str, str]:
        """DELETE /api/v1/theme/?q=(ids:!(...)) -- bulk delete themes."""
        ids = extract_ids_required(rison_params)
        cmd = BulkDeleteThemeCommand(dao=dao, ids=ids)  # type: ignore[arg-type]
        count = await cmd.execute()
        await event_logger.alog_with_context(
            "theme.bulk_delete",
            extra={"count": count},
            user_id=current_user.id,
        )
        return {"message": f"Deleted {count} themes"}

    @put(
        "/{pk:int}/set_system_dark",
        guards=[require_permission("can_write", "Theme")],
    )
    async def set_system_dark(
        self,
        pk: int,
        dao: Any,
        current_user: UserProtocol,
        state: State,
    ) -> dict[str, Any]:
        """PUT /api/v1/theme/{pk}/set_system_dark -- set as system dark theme."""
        _guard_system_theme_admin(current_user, state)
        cmd = SetSystemDarkCommand(dao=dao, pk=pk)
        theme = await cmd.execute()
        await event_logger.alog_with_context(
            "theme.set_system_dark",
            object_ref=str(pk),
            user_id=current_user.id,
        )
        return {"id": theme.id, "result": {"id": theme.id}}

    @delete(
        "/unset_system_dark",
        guards=[require_permission("can_write", "Theme")],
        status_code=200,
    )
    async def unset_system_dark(
        self,
        dao: Any,
        current_user: UserProtocol,
        state: State,
    ) -> dict[str, Any]:
        """DELETE /api/v1/theme/unset_system_dark -- clear system dark theme."""
        _guard_system_theme_admin(current_user, state)
        cmd = UnsetSystemDarkCommand(dao=dao)
        await cmd.execute()
        await event_logger.alog_with_context(
            "theme.unset_system_dark", user_id=current_user.id
        )
        return {"message": "OK"}

    @get(
        "/export/",
        guards=[require_permission("can_export", "Theme")],
    )
    async def export_themes(
        self,
        dao: Any,
        current_user: UserProtocol,
    ) -> dict[str, Any]:
        """GET /api/v1/theme/export/ -- export themes as YAML in a ZIP bundle.

        Ported from superset_old/themes/api.py ``ThemeRestApi.export``.
        Queries all themes, serializes each to YAML, packages them into
        a ZIP archive, and returns the file list as a dict.  The original
        returns a binary ZIP via ``send_file``; here we return the dict
        of filename -> YAML content for JSON transport.  Callers that
        need a real ZIP download can wrap this response.
        """
        from datetime import datetime
        from io import BytesIO
        from zipfile import ZipFile

        cmd = ExportThemesCommand(dao=dao)
        files = await cmd.execute()
        await event_logger.alog_with_context("theme.export", user_id=current_user.id)

        timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        root = f"theme_export_{timestamp}"

        # Build ZIP in-memory (matching original format)
        buf = BytesIO()
        with ZipFile(buf, "w") as bundle:
            for file_name, file_content in files:
                with bundle.open(f"{root}/{file_name}", "w") as fp:
                    fp.write(file_content.encode())
        buf.seek(0)

        # Return as dict for JSON response; binary ZIP download would
        # be handled by a dedicated streaming endpoint if needed.
        result = dict(files)
        return {"result": result}

    @post(
        "/import/",
        guards=[require_permission("can_write", "Theme")],
    )
    async def import_themes(  # noqa: C901
        self,
        dao: Any,
        current_user: UserProtocol,
        data: UploadFile = Body(media_type=RequestEncodingType.MULTI_PART),  # noqa: B008
    ) -> dict[str, Any]:
        """POST /api/v1/theme/import/ -- import themes from a ZIP file.

        Ported from superset_old/themes/api.py ``ThemeRestApi.import_``.
        Accepts a multipart/form-data upload containing a ZIP file with
        YAML theme definitions.  Each YAML file under ``themes/`` in the
        archive is parsed and imported via ``ImportThemesCommand``.
        """
        from io import BytesIO
        from zipfile import ZipFile

        import yaml

        file_bytes = await data.read()
        if not file_bytes:
            return {"message": "No file uploaded", "errors": ["Empty upload"]}

        # Parse ZIP contents into a dict of filename -> parsed YAML config
        contents: dict[str, Any] = {}
        try:
            with ZipFile(BytesIO(file_bytes)) as bundle:
                for zip_entry in bundle.namelist():
                    # Only process YAML files under themes/ paths
                    # Strip the root export directory prefix if present
                    # (e.g. "theme_export_20240101T000000/themes/My Theme.yaml")
                    parts = zip_entry.split("/")
                    # Find the "themes" segment and reconstruct relative path
                    relative_path: str | None = None
                    for i, part in enumerate(parts):
                        if part == "themes" and i + 1 < len(parts):
                            relative_path = "/".join(parts[i:])
                            break

                    if relative_path is None:
                        continue
                    if not relative_path.endswith((".yaml", ".yml")):
                        continue

                    raw = bundle.read(zip_entry)
                    try:
                        config = yaml.safe_load(raw)
                    except yaml.YAMLError:
                        continue

                    if isinstance(config, dict):
                        contents[relative_path] = config
        except Exception:
            return {"message": "Invalid ZIP file", "errors": ["Could not read ZIP"]}

        if not contents:
            return {
                "message": "No valid theme files found",
                "errors": ["No YAML files under themes/ in the uploaded ZIP"],
            }

        cmd = ImportThemesCommand(dao=dao, contents=contents, overwrite=True)
        count = await cmd.execute()
        await event_logger.alog_with_context("theme.import", user_id=current_user.id)
        return {"message": f"Imported {count} themes"}

    @get(
        "/_info",
        guards=[require_permission("can_read", "Theme")],
    )
    async def info(self, dao: CRUDDAOProtocol) -> dict[str, Any]:
        """GET /api/v1/theme/_info -- API metadata for frontend."""
        return await get_info_payload(
            dao=dao,
            model_name="Theme",
            permissions=["can_read", "can_write", "can_export"],
        )

    # ------------------------------------------------------------------
    # GET /related/{column_name} — related values for dropdowns
    # ------------------------------------------------------------------
    @get(
        "/related/{column_name:str}",
        guards=[require_permission("can_read", "Theme")],
    )
    async def related(
        self,
        column_name: str,
        dao: CRUDDAOProtocol,
        rison_params: dict[str, Any] | None,
        state: State,
        security_manager: Any,
    ) -> dict[str, Any]:
        """GET /api/v1/theme/related/{column_name} — related values.

        Returns distinct values for a relationship column (e.g. created_by,
        changed_by) for use in select dropdown filters.

        Ported from superset_old/themes/api.py ``ThemeRestApi`` which
        included ``RouteMethod.RELATED`` with
        ``allowed_rel_fields = {"created_by", "changed_by"}``.

        The original FAB ``related`` method returns 404 when column_name
        is not in ``allowed_rel_fields``. The original also applies:
        - ``related_field_filters``: combined first_name + last_name search
          via ``FilterRelatedOwners`` on ``changed_by``
        - ``base_related_field_filters``: excludes users in
          ``EXCLUDE_USERS_FROM_LISTS`` via ``BaseFilterRelatedUsers``
        - ``EXTRA_RELATED_QUERY_FILTERS["user"]`` hook for additional filtering
        """
        allowed_rel_fields = frozenset({"created_by", "changed_by"})

        # FAB returns 404 when the column is not in allowed_rel_fields
        if column_name not in allowed_rel_fields:
            raise ObjectNotFoundError("related", column_name)

        # Build base_filters matching the original BaseFilterRelatedUsers
        # (superset_old/views/filters.py lines 72-87):
        # 1. Apply EXTRA_RELATED_QUERY_FILTERS["user"] hook
        # 2. If EXCLUDE_USERS_FROM_LISTS is None, fall back to
        #    security_manager.get_exclude_users_from_lists()
        # 3. Exclude matched usernames
        base_filters: list[Any] = []
        try:
            from superset.models.security import User

            settings = getattr(state, "settings", None)

            # Step 1: Apply EXTRA_RELATED_QUERY_FILTERS["user"] hook
            extra_related_filters: dict[str, Any] = (
                getattr(settings, "extra_related_query_filters", {}) if settings else {}
            )
            user_extra_filter = extra_related_filters.get("user")
            if callable(user_extra_filter):
                # The hook is a callable that receives and returns a query;
                # we store it for get_related_payload to apply as a stmt filter.
                # Since our get_related_payload applies base_filters as WHERE
                # clauses, and the original hook transforms a query, we need
                # to capture the filter clause. For simple callable filters
                # that return a clause, we pass it through.
                result = user_extra_filter(None)
                if result is not None:
                    base_filters.append(result)

            # Step 2: Determine exclude_users list with fallback
            # Original: EXCLUDE_USERS_FROM_LISTS is None -> call
            # security_manager.get_exclude_users_from_lists()
            exclude_users: list[str] | None = (
                getattr(settings, "exclude_users_from_lists", None)
                if settings
                else None
            )
            if exclude_users is None:
                # Fallback: security_manager.get_exclude_users_from_lists()
                get_exclude = getattr(
                    security_manager, "get_exclude_users_from_lists", None
                )
                if callable(get_exclude):
                    exclude_users = get_exclude()

            # Step 3: Exclude matched usernames
            if exclude_users:
                base_filters.append(User.username.not_in(exclude_users))
        except Exception:  # noqa: BLE001, S110
            pass

        return await get_related_payload(
            dao=dao,
            column_name=column_name,
            rison_params=rison_params,
            allowed_fields=allowed_rel_fields,
            base_filters=base_filters if base_filters else None,
        )
