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
from litestar.connection import Request
from litestar.datastructures import State, UploadFile
from litestar.di import Provide
from litestar.params import Parameter
from litestar.response import Stream

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
    build_export_headers,
    build_rison_query_params,
    extract_ids_required,
    get_info_payload,
    get_related_payload,
    serialize_list_response,
    stream_zip,
)
from superset.events import event_logger
from superset.exceptions import ObjectNotFoundError
from superset.guards.rbac import require_permission
from superset.i18n import gettext as _
from superset.params.rison import provide_rison_query
from superset.providers import provide_theme_dao
from superset.schemas.theme import ThemePostSchema, ThemePutSchema
from superset.typing import CRUDDAOProtocol, SecurityManagerProtocol, UserProtocol
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
    is_admin = any(getattr(role, "name", "") == admin_role_name for role in user_roles)
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
        """GET /api/v1/theme/ — list themes with rison filters + pagination."""
        from sqlalchemy import or_
        from sqlalchemy.orm import selectinload

        from superset.models.core import Theme

        def _theme_all_text(model: Any, value: Any) -> Any:
            """``ThemeAllTextFilter`` — free-text search over theme_name +
            json_data (1:1 upstream)."""
            if not value:
                return None
            ilike = f"%{value}%"
            return or_(model.theme_name.ilike(ilike), model.json_data.ilike(ilike))

        # Apply the request's rison filters/ordering — the theme list previously
        # IGNORED them (only paginated), so ``?q=(filters:...)`` was a silent
        # no-op (it always returned every theme). Now supports the standard
        # operators + the ``theme_all_text`` custom filter.
        rison_filters, order_by, page, page_size = build_rison_query_params(
            Theme,
            rison_params,
            custom_filters={"theme_all_text": _theme_all_text},
        )
        # Eager-load the audit-user relationships so the dotted ``changed_by.*``
        # / ``created_by.*`` columns serialize without a lazy-load MissingGreenlet.
        themes = await dao.find_all(
            filters=rison_filters or None,
            page=page,
            page_size=page_size,
            order_by=order_by,
            options=[
                selectinload(Theme.changed_by),
                selectinload(Theme.created_by),
            ],
        )
        total = await dao.count(filters=rison_filters or None)
        await event_logger.alog_with_context("theme.list", user_id=current_user.id)
        # 1:1 with upstream ``ThemeRestApi.list_columns`` — includes
        # ``changed_by_name`` (AuditMixinNullable @property returning the
        # changer's full name) and ``created_on`` (SA datetime column) which
        # the frontend list view uses for display/sorting.
        return serialize_list_response(
            themes,
            total,
            [
                "id",
                "theme_name",
                "json_data",
                "uuid",
                "is_system",
                "is_system_default",
                "is_system_dark",
                "changed_on_delta_humanized",
                "changed_by.first_name",
                "changed_by.id",
                "changed_by.last_name",
                "changed_by_name",
                "created_on",
                "created_by.first_name",
                "created_by.id",
                "created_by.last_name",
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

        Returns the real ``Theme`` columns 1:1 with upstream
        ``ThemeRestApi.show_columns`` (id, theme_name, json_data, uuid,
        is_system*, audit). There is NO css/json_metadata/description on the
        model — those phantom keys are dropped.
        """
        from sqlalchemy.orm import selectinload

        from superset.models.core import Theme

        themes = await dao.find_all(
            filters=[Theme.id == pk],
            page_size=1,
            options=[
                selectinload(Theme.changed_by),
                selectinload(Theme.created_by),
            ],
        )
        if not themes:
            raise ObjectNotFoundError("Theme", pk)
        theme = themes[0]
        await event_logger.alog_with_context(
            "theme.get", object_ref=str(pk), user_id=current_user.id
        )
        changed_by = getattr(theme, "changed_by", None)
        created_by = getattr(theme, "created_by", None)

        def _user_ref(u: Any) -> dict[str, Any] | None:
            if u is None:
                return None
            return {
                "first_name": getattr(u, "first_name", ""),
                "id": u.id,
                "last_name": getattr(u, "last_name", ""),
            }

        return {
            "id": theme.id,
            "result": {
                "id": theme.id,
                "theme_name": theme.theme_name,
                "json_data": getattr(theme, "json_data", "") or "",
                "uuid": str(theme.uuid) if getattr(theme, "uuid", None) else None,
                "is_system": getattr(theme, "is_system", False),
                "is_system_default": getattr(theme, "is_system_default", False),
                "is_system_dark": getattr(theme, "is_system_dark", False),
                "changed_on_delta_humanized": getattr(
                    theme, "changed_on_delta_humanized", None
                ),
                "changed_by": _user_ref(changed_by),
                "created_by": _user_ref(created_by),
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
        # Mirror FAB ``post_headless``: ``{"id": <pk>, "result": <add_columns
        # dump of submitted fields>}`` (add_columns = ["json_data", "theme_name"]).
        return {
            "id": theme.id,
            "result": {
                "theme_name": data.theme_name,
                "json_data": data.json_data,
            },
        }

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
        from litestar.response import Response
        from msgspec import structs as _structs

        payload = filter_unset(_structs.asdict(data))
        # Mirror superset_old/themes/api.py ``put``:
        # ``if not request.json: return self.response_400(...)`` — reject an
        # empty request body (no set fields) with HTTP 400.
        if not payload:
            return Response(  # type: ignore[return-value]
                content={"message": "Request body is required"},
                status_code=400,
            )
        cmd = UpdateThemeCommand(dao=dao, pk=pk, data=payload)  # type: ignore[arg-type]
        theme = await cmd.execute()
        await event_logger.alog_with_context(
            "theme.update", object_ref=str(pk), user_id=current_user.id
        )
        # Mirror superset_old/themes/api.py ``put``:
        # ``self.response(200, id=changed_model.id, result=item)`` which
        # produces ``{"id": <pk>, "result": {...}}``.  edit_columns =
        # ["json_data", "theme_name"]; the values reflect the submitted
        # payload (``item``), not the persisted record.
        return {
            "id": theme.id,
            "result": {
                "theme_name": theme.theme_name,
                "json_data": getattr(theme, "json_data", "") or "",
            },
        }

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
        # Mirror superset_old/themes/api.py ``delete``: ngettext with num=1.
        return {"message": _("Deleted %(num)d theme", num=1)}

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
        # Mirror superset_old/themes/api.py: response(200, id=..., result="success").
        return {"id": theme.id, "result": "success"}

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
        # Mirror superset_old/themes/api.py: response(200, result="success").
        return {"result": "success"}

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
        # Mirror superset_old/themes/api.py ``bulk_delete``: ngettext keyed
        # on len(item_ids) (singular form for N == 1).
        num = len(ids)
        message = (
            _("Deleted %(num)d theme", num=num)
            if num == 1
            else _("Deleted %(num)d themes", num=num)
        )
        return {"message": message}

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
        # Mirror superset_old/themes/api.py: response(200, id=..., result="success").
        return {"id": theme.id, "result": "success"}

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
        # Mirror superset_old/themes/api.py: response(200, result="success").
        return {"result": "success"}

    @get(
        "/export/",
        guards=[require_permission("can_export", "Theme")],
    )
    async def export_themes(
        self,
        dao: Any,
        rison_params: list[int] | dict[str, Any] | None,
        current_user: UserProtocol,
        token: str | None = Parameter(query="token", default=None),
    ) -> Stream:
        """GET /api/v1/theme/export/?q=!(id1,id2) — download the requested
        themes as a binary ZIP (1:1 upstream). The port previously dumped ALL
        themes (ignoring the ``q`` ids) and returned a JSON dict instead of a
        ZIP — the frontend "Export" download got an unusable response.
        """
        from datetime import datetime
        from io import BytesIO
        from zipfile import ZipFile

        ids = extract_ids_required(rison_params)
        cmd = ExportThemesCommand(dao=dao, model_ids=ids)
        files = await cmd.execute()
        await event_logger.alog_with_context("theme.export", extra={"count": len(ids)})

        timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        root = f"theme_export_{timestamp}"

        metadata = (
            f"version: 1.0.0\ntype: Theme\ntimestamp: '{datetime.now().isoformat()}'\n"
        )
        buf = BytesIO()
        with ZipFile(buf, "w") as bundle:
            with bundle.open(f"{root}/metadata.yaml", "w") as fp:
                fp.write(metadata.encode())
            for file_name, file_content in files:
                with bundle.open(f"{root}/{file_name}", "w") as fp:
                    fp.write(file_content.encode())
        return Stream(
            stream_zip(buf),
            status_code=200,
            media_type="application/zip",
            headers=build_export_headers(f"{root}.zip", token=token),
        )

    @post(
        "/import/",
        guards=[require_permission("can_write", "Theme")],
        status_code=200,
    )
    async def import_themes(  # noqa: C901
        self,
        request: Request[Any, Any, Any],
        dao: Any,
        current_user: UserProtocol,
    ) -> dict[str, Any]:
        """POST /api/v1/theme/import/ -- import themes from a ZIP file.

        Ported from superset_old/themes/api.py ``ThemeRestApi.import_``.
        Accepts a multipart/form-data upload containing a ZIP file with
        YAML theme definitions.  Each YAML file under ``themes/`` in the
        archive is parsed and imported via ``ImportThemesCommand``.

        The original reads from ``request.files.get('formData')``.
        We read the raw form and pick the first UploadFile regardless
        of field name (matching the pattern in parse_import_request),
        which is compatible with both 'formData' and 'data' field names.
        """
        from io import BytesIO
        from zipfile import ZipFile

        import yaml
        from litestar.response import Response

        # Read multipart form manually to be field-name agnostic — 1:1
        # with superset_old/themes/api.py:537 which reads 'formData'.
        form = await request.form()
        upload = next((v for v in form.values() if isinstance(v, UploadFile)), None)
        if upload is None:
            return Response(  # type: ignore[return-value]
                content={"message": "Arguments are not correct"},
                status_code=400,
            )
        file_bytes = await upload.read()
        overwrite = form.get("overwrite") == "true"
        if not file_bytes:
            return Response(  # type: ignore[return-value]
                content={"message": "Arguments are not correct"},
                status_code=400,
            )

        from superset.commands.importers.exceptions import IncorrectVersionError
        from superset.commands.importers.v1.utils import (
            _check_is_safe_zip,
            load_metadata,
            validate_metadata_type,
        )
        from superset.exceptions import SupersetException

        # Parse ZIP contents into a dict of filename -> parsed YAML config
        contents: dict[str, Any] = {}
        # Also collect raw YAML strings for metadata validation — mirrors the
        # original's get_contents_from_bundle which passes ALL file contents
        # (including metadata.yaml) to ImportThemesCommand.validate().
        raw_metadata_contents: dict[str, str] = {}
        try:
            with ZipFile(BytesIO(file_bytes)) as bundle:
                # Zip-bomb / path-traversal guard — mirrors the original's
                # get_contents_from_bundle which calls check_is_safe_zip
                # (superset_old/commands/importers/v1/utils.py:229-230).
                # Raises SupersetException when uncompressed size exceeds
                # ZIPPED_FILE_MAX_SIZE or compression ratio exceeds
                # ZIP_FILE_MAX_COMPRESS_RATIO.
                _check_is_safe_zip(bundle)
                for zip_entry in bundle.namelist():
                    parts = zip_entry.split("/")
                    # Capture metadata.yaml at the export-bundle root level
                    # (e.g. "theme_export_20240101T000000/metadata.yaml").
                    # The original get_contents_from_bundle reads ALL files and
                    # ImportModelsCommand.validate() checks metadata.yaml first
                    # (superset_old/commands/importers/v1/__init__.py:98).
                    if parts[-1] == "metadata.yaml":
                        raw_metadata_contents["metadata.yaml"] = bundle.read(
                            zip_entry
                        ).decode("utf-8", errors="replace")
                        continue

                    # Only process YAML files under themes/ paths
                    # Strip the root export directory prefix if present
                    # (e.g. "theme_export_20240101T000000/themes/My Theme.yaml")
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
        except SupersetException:
            raise
        except Exception:
            return {"message": "Invalid ZIP file", "errors": ["Could not read ZIP"]}

        # Validate metadata.yaml version and type — mirrors the original
        # ImportModelsCommand.validate() → load_metadata() + validate_metadata_type()
        # flow (superset_old/commands/importers/v1/__init__.py:93-133).
        # IncorrectVersionError → CommandInvalidError (mirrors the dispatcher
        # which catches IncorrectVersionError and raises CommandInvalidError).
        from superset.exceptions import CommandInvalidError

        try:
            metadata = load_metadata(raw_metadata_contents)
        except IncorrectVersionError as ex:
            raise CommandInvalidError(str(ex)) from ex
        exceptions: list[Exception] = []
        validate_metadata_type(metadata, "Theme", exceptions)
        if exceptions:
            raise CommandInvalidError(str(exceptions[0])) from exceptions[0]

        # NO early-return for "no themes/*.yaml" — the original
        # ``get_contents_from_bundle`` returns ALL YAML files (including
        # metadata.yaml), so a bundle with only a valid metadata.yaml still
        # reaches ImportThemesCommand and yields HTTP 200
        # "Theme imported successfully" (a no-op import).

        # Expose the current user so any audit-stamp path resolves the
        # importing user (mirrors the database-upload controller).
        from superset.utils.core import set_current_user

        set_current_user(current_user)
        # ``overwrite`` is read from the multipart form (was hardcoded True →
        # an import silently clobbered an existing same-uuid theme); imported
        # themes get the current user as owner.
        cmd = ImportThemesCommand(
            dao=dao,
            contents=contents,
            overwrite=overwrite,
            current_user=current_user,
        )
        await cmd.execute()
        await event_logger.alog_with_context("theme.import", user_id=current_user.id)
        return {"message": "Theme imported successfully"}

    @get(
        "/_info",
        guards=[require_permission("can_read", "Theme")],
    )
    async def info(
        self,
        dao: CRUDDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
    ) -> dict[str, Any]:
        """GET /api/v1/theme/_info -- API metadata for frontend."""
        return await get_info_payload(
            dao=dao,
            model_name="Theme",
            permissions=["can_read", "can_write", "can_export"],
            security_manager=security_manager,
            current_user=current_user,
            class_permission_name="Theme",
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
        query_hook: Any | None = None
        try:
            from superset.models.security import User

            settings = getattr(state, "settings", None)

            # Step 1: Apply EXTRA_RELATED_QUERY_FILTERS["user"] hook.
            # Original contract (superset_old/views/filters.py:72-76):
            #   query = extra_filters(query)  — Callable[[Query], Query]
            # We pass the hook through to get_related_payload as query_hook
            # so it receives the real Select statement and returns the
            # modified Select, matching the original calling convention.
            #
            # IMPORTANT: The original only registers BaseFilterRelatedUsers
            # (which applies this hook) for ``changed_by`` via
            # ``base_related_field_filters``
            # (superset_old/themes/api.py:150-152).  ``created_by`` has NO
            # entry there, so the hook MUST NOT be applied to created_by.
            extra_related_filters: dict[str, Any] = (
                getattr(settings, "extra_related_query_filters", {}) if settings else {}
            )
            user_extra_filter = extra_related_filters.get("user")
            if callable(user_extra_filter) and column_name == "changed_by":
                query_hook = user_extra_filter

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

            # Step 3: Exclude matched usernames — original
            # ``base_related_field_filters`` maps ONLY ``changed_by`` to
            # ``BaseFilterRelatedUsers`` (superset_old/themes/api.py:150-152);
            # ``created_by`` has no entry so the exclusion is NOT applied for it.
            if exclude_users and column_name == "changed_by":
                base_filters.append(User.username.not_in(exclude_users))
        except Exception:  # noqa: BLE001, S110
            pass

        return await get_related_payload(
            dao=dao,
            column_name=column_name,
            rison_params=rison_params,
            allowed_fields=allowed_rel_fields,
            base_filters=base_filters if base_filters else None,
            query_hook=query_hook,
        )
