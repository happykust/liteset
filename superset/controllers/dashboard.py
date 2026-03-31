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
"""Dashboard controller — 22 endpoints for dashboard CRUD, export/import, favorites,
embedded, screenshots, and related objects."""

from __future__ import annotations

import io
from typing import Any

from litestar import Controller, delete, get, post, put
from litestar.datastructures import State, UploadFile
from litestar.di import Provide
from litestar.enums import RequestEncodingType
from litestar.params import Body, Parameter
from litestar.response import Response, Stream

from superset.commands.dashboard import (
    BulkDeleteDashboardsCommand,
    CopyDashboardCommand,
    CreateDashboardCommand,
    DeleteDashboardCommand,
    DeleteEmbeddedDashboardCommand,
    ExportDashboardsCommand,
    ImportDashboardsCommand,
    UpdateDashboardColorsCommand,
    UpdateDashboardCommand,
    UpdateDashboardFiltersCommand,
    UpsertEmbeddedDashboardCommand,
)
from superset.commands.dashboard_permalink import (
    CreateDashboardPermalinkCommand,
    GetDashboardPermalinkCommand,
)

# DAO imports moved to provider functions (avoid Flask import chain)
from superset.controllers.base import (
    build_export_headers,
    build_rison_query_params,
    extract_ids,
    extract_ids_required,
    get_distinct_payload,
    get_info_payload,
    get_related_payload,
    serialize_list_response,
    stream_zip,
)
from superset.events import event_logger
from superset.exceptions import CommandInvalidError, ObjectNotFoundError
from superset.guards.rbac import require_permission
from superset.params.rison import provide_rison_query
from superset.providers import (
    provide_dashboard_dao,
    provide_embedded_dao,
    provide_kv_dao,
)
from superset.schemas.base import FavoriteStatusItem, FavoriteStatusResponse
from superset.schemas.dashboard import (
    DashboardColorsUpdateSchema,
    DashboardCopySchema,
    DashboardDataset,
    DashboardFiltersUpdateSchema,
    DashboardGetResponse,
    DashboardPermalinkSchema,
    DashboardPostSchema,
    DashboardPutSchema,
    DashboardScreenshotSchema,
    EmbeddedDashboardConfig,
    EmbeddedDashboardResponse,
    TabInfo,
)
from superset.typing import (
    DashboardDAOProtocol,
    EmbeddedDAOProtocol,
    KeyValueDAOProtocol,
    SecurityManagerProtocol,
    UserProtocol,
)
from superset.utils import filter_none, filter_unset


class DashboardController(Controller):
    path = "/api/v1/dashboard"
    tags = ["Dashboards"]
    dependencies = {
        "dao": Provide(provide_dashboard_dao, sync_to_thread=False),
        "embedded_dao": Provide(provide_embedded_dao, sync_to_thread=False),
        "kv_dao": Provide(provide_kv_dao, sync_to_thread=False),
        "rison_params": Provide(provide_rison_query),
    }

    # ------------------------------------------------------------------
    # GET — list dashboards
    # ------------------------------------------------------------------
    @get(
        "/",
        guards=[require_permission("can_read", "Dashboard")],
    )
    async def get_list(
        self,
        dao: DashboardDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
        rison_params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """GET /api/v1/dashboard/ — list dashboards with filtering/pagination."""
        from superset.db.filters import dashboard_access_filters

        rison_filters, order_by, page, page_size = build_rison_query_params(
            dao.model_cls, rison_params
        )
        base_filters = await dashboard_access_filters(security_manager, current_user)
        all_filters = (base_filters or []) + rison_filters
        dashboards = await dao.find_all(
            filters=all_filters or None,
            page=page,
            page_size=page_size,
            order_by=order_by,
        )
        total = await dao.count(filters=all_filters or None)
        event_logger.log("dashboard.list")
        return serialize_list_response(
            dashboards,
            total,
            ["id", "dashboard_title", "slug", "published"],
        )

    # ------------------------------------------------------------------
    # GET — API metadata
    # ------------------------------------------------------------------
    @get(
        "/_info",
        guards=[require_permission("can_read", "Dashboard")],
    )
    async def info(self, dao: DashboardDAOProtocol) -> dict[str, Any]:
        """GET /api/v1/dashboard/_info — API metadata for frontend."""
        return await get_info_payload(
            dao=dao,
            model_name="Dashboard",
            permissions=["can_read", "can_write"],
        )

    # ------------------------------------------------------------------
    # GET — related values for dropdowns
    # ------------------------------------------------------------------
    @get(
        "/related/{column_name:str}",
        guards=[require_permission("can_read", "Dashboard")],
    )
    async def related(
        self,
        column_name: str,
        dao: DashboardDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
        rison_params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """GET /api/v1/dashboard/related/{column_name}"""
        from superset.db.filters import dashboard_access_filters

        base_filters = await dashboard_access_filters(security_manager, current_user)
        return await get_related_payload(
            dao=dao,
            column_name=column_name,
            rison_params=rison_params,
            allowed_fields=frozenset({"owners", "roles", "created_by", "changed_by"}),
            base_filters=base_filters or None,
        )

    # ------------------------------------------------------------------
    # GET — distinct values for filters
    # ------------------------------------------------------------------
    @get(
        "/distinct/{column_name:str}",
        guards=[require_permission("can_read", "Dashboard")],
    )
    async def distinct(
        self,
        column_name: str,
        dao: DashboardDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
        rison_params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """GET /api/v1/dashboard/distinct/{column_name}"""
        from superset.db.filters import dashboard_access_filters

        base_filters = await dashboard_access_filters(security_manager, current_user)
        return await get_distinct_payload(
            dao=dao,
            column_name=column_name,
            rison_params=rison_params,
            base_filters=base_filters or None,
        )

    # ------------------------------------------------------------------
    # GET — single dashboard
    # ------------------------------------------------------------------
    @get(
        "/{id_or_slug:str}",
        guards=[require_permission("can_read", "Dashboard")],
    )
    async def get_dashboard(
        self,
        id_or_slug: str,
        dao: DashboardDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
    ) -> DashboardGetResponse:
        dashboard = await dao.get_by_id_or_slug(id_or_slug)
        if not dashboard:
            raise ObjectNotFoundError("Dashboard", id_or_slug)
        # Verify object-level access
        from superset.db.filters import dashboard_access_filters

        base_filters = await dashboard_access_filters(security_manager, current_user)
        if base_filters:
            from sqlalchemy import select as sa_select

            model_cls = getattr(dao, "model_cls", None)
            if model_cls is not None:
                stmt = sa_select(model_cls.id).where(
                    model_cls.id == dashboard.id, *base_filters
                )
                result = await dao.session.scalar(stmt)
                if result is None:
                    raise ObjectNotFoundError("Dashboard", id_or_slug)
        owners = getattr(dashboard, "owners", []) or []
        roles = getattr(dashboard, "roles", []) or []
        tags = getattr(dashboard, "tags", []) or []
        changed_by = getattr(dashboard, "changed_by", None)
        created_by = getattr(dashboard, "created_by", None)
        changed_on = getattr(dashboard, "changed_on", None)
        created_on = getattr(dashboard, "created_on", None)
        charts = getattr(dashboard, "slices", []) or []
        event_logger.log("dashboard.get", object_ref=f"dashboard:{id_or_slug}")
        return DashboardGetResponse(
            id=dashboard.id,
            result={
                "dashboard_title": dashboard.dashboard_title,
                "slug": dashboard.slug,
                "position_json": dashboard.position_json,
                "css": dashboard.css,
                "json_metadata": dashboard.json_metadata,
                "published": dashboard.published,
                "description": getattr(dashboard, "description", None),
                "uuid": str(dashboard.uuid)
                if getattr(dashboard, "uuid", None)
                else None,
                "changed_on": changed_on.isoformat() if changed_on else None,
                "created_on": created_on.isoformat() if created_on else None,
                "changed_by": {
                    "id": changed_by.id,
                    "first_name": getattr(changed_by, "first_name", ""),
                    "last_name": getattr(changed_by, "last_name", ""),
                }
                if changed_by
                else None,
                "created_by": {
                    "id": created_by.id,
                    "first_name": getattr(created_by, "first_name", ""),
                    "last_name": getattr(created_by, "last_name", ""),
                }
                if created_by
                else None,
                "owners": [{"id": o.id, "name": str(o)} for o in owners],
                "roles": [
                    {"id": r.id, "name": getattr(r, "name", str(r))} for r in roles
                ],
                "charts": [
                    {"id": c.id, "slice_name": getattr(c, "slice_name", str(c))}
                    for c in charts
                ],
                "certified_by": getattr(dashboard, "certified_by", None),
                "thumbnail_url": (
                    f"/api/v1/dashboard/{dashboard.id}/thumbnail/"
                    f"{getattr(dashboard, 'digest', '')}/"
                    if getattr(dashboard, "digest", None)
                    else None
                ),
                "is_managed_externally": getattr(
                    dashboard, "is_managed_externally", False
                ),
                "tags": [
                    {"id": t.id, "name": getattr(t, "name", str(t))} for t in tags
                ],
                "table_names": getattr(dashboard, "table_names", None),
                "changed_on_delta_humanized": getattr(
                    dashboard, "changed_on_delta_humanized", None
                ),
            },
        )

    # ------------------------------------------------------------------
    # GET — related datasets
    # ------------------------------------------------------------------
    @get(
        "/{id_or_slug:str}/datasets",
        guards=[require_permission("can_read", "Dashboard")],
    )
    async def get_datasets(
        self,
        id_or_slug: str,
        dao: DashboardDAOProtocol,
    ) -> dict[str, Any]:
        dashboard = await dao.get_by_id_or_slug(id_or_slug)
        if not dashboard:
            raise ObjectNotFoundError("Dashboard", id_or_slug)
        datasets = await dao.get_datasets_for_dashboard(dashboard)
        return {
            "result": [
                DashboardDataset(
                    id=ds.id,
                    uid=getattr(ds, "uid", None),
                    column_names=[
                        c.column_name
                        for c in getattr(ds, "columns", [])
                        if hasattr(c, "column_name")
                    ],
                    verbose_map=getattr(ds, "verbose_map", {}),
                )
                for ds in datasets
            ]
        }

    # ------------------------------------------------------------------
    # GET — tab structure
    # ------------------------------------------------------------------
    @get(
        "/{id_or_slug:str}/tabs",
        guards=[require_permission("can_read", "Dashboard")],
    )
    async def get_tabs(
        self,
        id_or_slug: str,
        dao: DashboardDAOProtocol,
    ) -> dict[str, Any]:
        from superset.commands.dashboard import parse_tab_structure

        dashboard = await dao.get_by_id_or_slug(id_or_slug)
        if not dashboard:
            raise ObjectNotFoundError("Dashboard", id_or_slug)
        if not dashboard.position_json:
            return {"tabs": []}
        raw_tabs = parse_tab_structure(dashboard.position_json)
        tabs = [TabInfo(**t) for t in raw_tabs]
        return {"result": tabs}

    # ------------------------------------------------------------------
    # GET — related charts
    # ------------------------------------------------------------------
    @get(
        "/{id_or_slug:str}/charts",
        guards=[require_permission("can_read", "Dashboard")],
    )
    async def get_charts(
        self,
        id_or_slug: str,
        dao: DashboardDAOProtocol,
    ) -> dict[str, Any]:
        dashboard = await dao.get_by_id_or_slug(id_or_slug)
        if not dashboard:
            raise ObjectNotFoundError("Dashboard", id_or_slug)
        charts = await dao.get_charts_for_dashboard(dashboard)
        return {
            "result": [
                {
                    "id": chart.id,
                    "slice_name": chart.slice_name,
                    "viz_type": chart.viz_type,
                }
                for chart in charts
            ]
        }

    # ------------------------------------------------------------------
    # POST — create
    # ------------------------------------------------------------------
    @post(
        "/",
        guards=[require_permission("can_write", "Dashboard")],
        status_code=201,
    )
    async def create(
        self,
        data: DashboardPostSchema,
        dao: DashboardDAOProtocol,
        current_user: UserProtocol,
    ) -> DashboardGetResponse:
        cmd = CreateDashboardCommand(
            dao=dao,
            data=filter_none(
                {
                    "dashboard_title": data.dashboard_title,
                    "slug": data.slug,
                    "position_json": data.position_json,
                    "css": data.css,
                    "json_metadata": data.json_metadata,
                    "published": data.published,
                    "certified_by": data.certified_by,
                    "certification_details": data.certification_details,
                    "is_managed_externally": data.is_managed_externally,
                    "external_url": data.external_url,
                }
            ),
            user_id=current_user.id,
        )
        dashboard = await cmd.execute()
        event_logger.log(
            "dashboard.create",
            object_ref=f"dashboard:{dashboard.id}",
            user_id=current_user.id,
        )
        return DashboardGetResponse(
            id=dashboard.id,
            result={
                "dashboard_title": dashboard.dashboard_title,
                "slug": dashboard.slug,
            },
        )

    # ------------------------------------------------------------------
    # PUT — update
    # ------------------------------------------------------------------
    @put(
        "/{pk:int}",
        guards=[require_permission("can_write", "Dashboard")],
    )
    async def update(
        self,
        pk: int,
        data: DashboardPutSchema,
        dao: DashboardDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
    ) -> DashboardGetResponse:
        update_data = filter_unset(
            {
                "dashboard_title": data.dashboard_title,
                "slug": data.slug,
                "position_json": data.position_json,
                "css": data.css,
                "json_metadata": data.json_metadata,
                "published": data.published,
                "certified_by": data.certified_by,
                "certification_details": data.certification_details,
                "is_managed_externally": data.is_managed_externally,
                "external_url": data.external_url,
            }
        )
        cmd = UpdateDashboardCommand(
            dao=dao,
            dashboard_id=pk,
            data=update_data,
            security_manager=security_manager,
            user_id=current_user.id,
        )
        dashboard = await cmd.execute()
        event_logger.log(
            "dashboard.update",
            object_ref=f"dashboard:{pk}",
            user_id=current_user.id,
        )
        changed_on = getattr(dashboard, "changed_on", None)
        created_on = getattr(dashboard, "created_on", None)
        changed_by = getattr(dashboard, "changed_by", None)
        created_by = getattr(dashboard, "created_by", None)
        owners = getattr(dashboard, "owners", []) or []
        roles = getattr(dashboard, "roles", []) or []
        tags = getattr(dashboard, "tags", []) or []
        last_modified_time = (
            changed_on.replace(microsecond=0).timestamp() if changed_on else None
        )
        return DashboardGetResponse(
            id=dashboard.id,
            result={
                "dashboard_title": dashboard.dashboard_title,
                "slug": dashboard.slug,
                "position_json": dashboard.position_json,
                "css": dashboard.css,
                "json_metadata": dashboard.json_metadata,
                "published": dashboard.published,
                "description": getattr(dashboard, "description", None),
                "uuid": str(dashboard.uuid)
                if getattr(dashboard, "uuid", None)
                else None,
                "changed_on": changed_on.isoformat() if changed_on else None,
                "created_on": created_on.isoformat() if created_on else None,
                "changed_by": {
                    "id": changed_by.id,
                    "first_name": getattr(changed_by, "first_name", ""),
                    "last_name": getattr(changed_by, "last_name", ""),
                }
                if changed_by
                else None,
                "created_by": {
                    "id": created_by.id,
                    "first_name": getattr(created_by, "first_name", ""),
                    "last_name": getattr(created_by, "last_name", ""),
                }
                if created_by
                else None,
                "owners": [{"id": o.id, "name": str(o)} for o in owners],
                "roles": [
                    {"id": r.id, "name": getattr(r, "name", str(r))} for r in roles
                ],
                "certified_by": getattr(dashboard, "certified_by", None),
                "is_managed_externally": getattr(
                    dashboard, "is_managed_externally", False
                ),
                "tags": [
                    {"id": t.id, "name": getattr(t, "name", str(t))} for t in tags
                ],
            },
            last_modified_time=last_modified_time,
        )

    # ------------------------------------------------------------------
    # PUT — update filters
    # ------------------------------------------------------------------
    @put(
        "/{pk:int}/filters",
        guards=[require_permission("can_write", "Dashboard")],
    )
    async def update_filters(
        self,
        pk: int,
        data: DashboardFiltersUpdateSchema,
        dao: DashboardDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
    ) -> dict[str, Any]:
        cmd = UpdateDashboardFiltersCommand(
            dao=dao,
            dashboard_id=pk,
            data={
                "deleted": data.deleted,
                "modified": data.modified,
                "reordered": data.reordered,
            },
            security_manager=security_manager,
            user_id=current_user.id,
        )
        dashboard = await cmd.execute()
        event_logger.log(
            "dashboard.update_filters",
            object_ref=f"dashboard:{pk}",
            user_id=current_user.id,
        )
        # Return parsed native_filter_configuration, not raw json_metadata string
        import json as _json

        nfc: list[dict[str, Any]] = []
        if dashboard.json_metadata:
            try:
                meta = _json.loads(dashboard.json_metadata)
                nfc = meta.get("native_filter_configuration", [])
            except (ValueError, TypeError):
                pass
        return {"result": nfc}

    # ------------------------------------------------------------------
    # PUT — update colors
    # ------------------------------------------------------------------
    @put(
        "/{pk:int}/colors",
        guards=[require_permission("can_write", "Dashboard")],
    )
    async def update_colors(
        self,
        pk: int,
        data: DashboardColorsUpdateSchema,
        dao: DashboardDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
    ) -> Response[None]:
        cmd = UpdateDashboardColorsCommand(
            dao=dao,
            dashboard_id=pk,
            data={
                "color_namespace": data.color_namespace,
                "color_scheme": data.color_scheme,
                "label_colors": data.label_colors,
                "map_label_colors": data.map_label_colors,
                "shared_label_colors": data.shared_label_colors,
                "color_scheme_domain": data.color_scheme_domain,
            },
            security_manager=security_manager,
            user_id=current_user.id,
        )
        await cmd.execute()
        event_logger.log(
            "dashboard.update_colors",
            object_ref=f"dashboard:{pk}",
            user_id=current_user.id,
        )
        return Response(content=None, status_code=200)

    # ------------------------------------------------------------------
    # DELETE — single
    # ------------------------------------------------------------------
    @delete(
        "/{pk:int}",
        guards=[require_permission("can_write", "Dashboard")],
        status_code=200,
    )
    async def delete_dashboard(
        self,
        pk: int,
        dao: DashboardDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
    ) -> dict[str, str]:
        cmd = DeleteDashboardCommand(
            dao=dao,
            dashboard_id=pk,
            security_manager=security_manager,
            user_id=current_user.id,
        )
        await cmd.execute()
        event_logger.log(
            "dashboard.delete",
            object_ref=f"dashboard:{pk}",
            user_id=current_user.id,
        )
        return {"message": "OK"}

    # ------------------------------------------------------------------
    # DELETE — bulk
    # ------------------------------------------------------------------
    @delete(
        "/",
        guards=[require_permission("can_write", "Dashboard")],
        status_code=200,
    )
    async def bulk_delete(
        self,
        dao: DashboardDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
        rison_params: dict[str, Any] | None,
    ) -> dict[str, str]:
        ids = extract_ids_required(rison_params)
        cmd = BulkDeleteDashboardsCommand(
            dao=dao,
            dashboard_ids=ids,
            security_manager=security_manager,
            user_id=current_user.id,
        )
        await cmd.execute()
        event_logger.log(
            "dashboard.bulk_delete",
            user_id=current_user.id,
            extra={"count": len(ids)},
        )
        return {"message": "OK"}

    # ------------------------------------------------------------------
    # GET — export (ZIP)
    # ------------------------------------------------------------------
    @get(
        "/export/",
        guards=[require_permission("can_read", "Dashboard")],
        media_type="application/zip",
    )
    async def export(
        self,
        dao: DashboardDAOProtocol,
        rison_params: dict[str, Any] | None,
        token: str | None = Parameter(query="token", default=None),
    ) -> Stream:
        ids = extract_ids(rison_params)
        if not ids:
            raise CommandInvalidError("At least one ID is required for export")
        cmd = ExportDashboardsCommand(model_ids=ids, dao=dao)
        buf = await cmd.execute()
        event_logger.log("dashboard.export", extra={"count": len(ids)})
        return Stream(
            stream_zip(buf),
            status_code=200,
            media_type="application/zip",
            headers=build_export_headers("dashboards_export.zip", token=token),
        )

    # ------------------------------------------------------------------
    # POST — trigger screenshot
    # ------------------------------------------------------------------
    @post(
        "/{pk:int}/cache_dashboard_screenshot/",
        guards=[require_permission("can_read", "Dashboard")],
    )
    async def cache_dashboard_screenshot(
        self,
        pk: int,
        data: DashboardScreenshotSchema,
        dao: DashboardDAOProtocol,
        state: State,
    ) -> dict[str, Any]:
        settings = getattr(state, "settings", None)
        flags = getattr(settings, "feature_flags", {}) or {}
        if not flags.get("THUMBNAILS", False) or not flags.get(
            "ENABLE_DASHBOARD_SCREENSHOT_ENDPOINTS", False
        ):
            raise ObjectNotFoundError("Dashboard screenshot", pk)
        dashboard = await dao.find_by_id(pk)
        if not dashboard:
            raise ObjectNotFoundError("Dashboard", pk)
        # Trigger Celery screenshot task (actual dispatch in thumbnails module)
        cache_key = f"dashboard_{pk}_screenshot"
        return {
            "cache_key": cache_key,
            "dashboard_url": f"/superset/dashboard/{pk}/",
            "image_url": f"/api/v1/dashboard/{pk}/screenshot/{cache_key}/",
        }

    # ------------------------------------------------------------------
    # GET — screenshot
    # ------------------------------------------------------------------
    @get(
        "/{pk:int}/screenshot/{digest:str}/",
        guards=[require_permission("can_read", "Dashboard")],
        media_type="image/png",
    )
    async def screenshot(
        self, pk: int, digest: str, dao: DashboardDAOProtocol, state: State
    ) -> Response[bytes]:
        settings = getattr(state, "settings", None)
        flags = getattr(settings, "feature_flags", {}) or {}
        if not flags.get("THUMBNAILS", False) or not flags.get(
            "ENABLE_DASHBOARD_SCREENSHOT_ENDPOINTS", False
        ):
            raise ObjectNotFoundError("Dashboard screenshot", pk)
        dashboard = await dao.find_by_id(pk)
        if not dashboard:
            raise ObjectNotFoundError("Dashboard", pk)
        # Return placeholder — actual screenshot retrieval from cache
        return Response(
            content=b"",
            status_code=202,
            media_type="image/png",
        )

    # ------------------------------------------------------------------
    # GET — thumbnail
    # ------------------------------------------------------------------
    @get(
        "/{pk:int}/thumbnail/{digest:str}/",
        guards=[require_permission("can_read", "Dashboard")],
        media_type="image/png",
    )
    async def thumbnail(
        self, pk: int, digest: str, dao: DashboardDAOProtocol, state: State
    ) -> Response[bytes]:
        settings = getattr(state, "settings", None)
        flags = getattr(settings, "feature_flags", {}) or {}
        if not flags.get("THUMBNAILS", False):
            raise ObjectNotFoundError("Dashboard thumbnail", pk)
        dashboard = await dao.find_by_id(pk)
        if not dashboard:
            raise ObjectNotFoundError("Dashboard", pk)
        return Response(
            content=b"",
            status_code=202,
            media_type="image/png",
        )

    # ------------------------------------------------------------------
    # GET — favorite status
    # ------------------------------------------------------------------
    @get(
        "/favorite_status/",
        guards=[require_permission("can_read", "Dashboard")],
    )
    async def favorite_status(
        self,
        dao: DashboardDAOProtocol,
        current_user: UserProtocol,
        rison_params: dict[str, Any] | None,
    ) -> FavoriteStatusResponse:
        ids = extract_ids(rison_params)
        if not ids:
            return FavoriteStatusResponse()
        fav_ids = set(await dao.favorited_ids(ids, current_user.id))
        return FavoriteStatusResponse(
            result=[FavoriteStatusItem(id=i, value=i in fav_ids) for i in ids],
        )

    # ------------------------------------------------------------------
    # POST — add favorite
    # ------------------------------------------------------------------
    @post(
        "/{pk:int}/favorites/",
        guards=[require_permission("can_read", "Dashboard")],
        status_code=200,
    )
    async def add_favorite(
        self, pk: int, dao: DashboardDAOProtocol, current_user: UserProtocol
    ) -> dict[str, str]:
        dashboard = await dao.find_by_id(pk)
        if not dashboard:
            raise ObjectNotFoundError("Dashboard", pk)
        await dao.add_favorite(pk, current_user.id)
        event_logger.log(
            "dashboard.add_favorite",
            object_ref=f"dashboard:{pk}",
            user_id=current_user.id,
        )
        return {"message": "OK"}

    # ------------------------------------------------------------------
    # DELETE — remove favorite
    # ------------------------------------------------------------------
    @delete(
        "/{pk:int}/favorites/",
        guards=[require_permission("can_read", "Dashboard")],
        status_code=200,
    )
    async def remove_favorite(
        self, pk: int, dao: DashboardDAOProtocol, current_user: UserProtocol
    ) -> dict[str, str]:
        dashboard = await dao.find_by_id(pk)
        if not dashboard:
            raise ObjectNotFoundError("Dashboard", pk)
        await dao.remove_favorite(pk, current_user.id)
        event_logger.log(
            "dashboard.remove_favorite",
            object_ref=f"dashboard:{pk}",
            user_id=current_user.id,
        )
        return {"message": "OK"}

    # ------------------------------------------------------------------
    # POST — import
    # ------------------------------------------------------------------
    @post(
        "/import/",
        guards=[require_permission("can_write", "Dashboard")],
        media_type="application/json",
    )
    async def import_dashboard(
        self,
        dao: DashboardDAOProtocol,
        data: UploadFile = Body(media_type=RequestEncodingType.MULTI_PART),  # noqa: B008
        overwrite: bool = False,
        passwords: str | None = None,
        ssh_tunnel_passwords: str | None = None,
    ) -> dict[str, str]:
        import json as _json

        contents = await data.read()
        buf = io.BytesIO(contents)
        try:
            passwords_dict: dict[str, str] = _json.loads(passwords) if passwords else {}
        except (ValueError, _json.JSONDecodeError):
            raise CommandInvalidError("Invalid JSON in 'passwords' field")
        try:
            ssh_dict: dict[str, str] = (
                _json.loads(ssh_tunnel_passwords) if ssh_tunnel_passwords else {}
            )
        except (ValueError, _json.JSONDecodeError):
            raise CommandInvalidError("Invalid JSON in 'ssh_tunnel_passwords' field")
        cmd = ImportDashboardsCommand(
            contents=buf,
            dao=dao,
            overwrite=overwrite,
            passwords=passwords_dict,
            ssh_tunnel_passwords=ssh_dict,
        )
        await cmd.execute()
        event_logger.log("dashboard.import")
        return {"message": "OK"}

    # ------------------------------------------------------------------
    # GET — embedded config
    # ------------------------------------------------------------------
    @get(
        "/{id_or_slug:str}/embedded",
        guards=[require_permission("can_read", "Dashboard")],
    )
    async def get_embedded(
        self,
        id_or_slug: str,
        dao: DashboardDAOProtocol,
        embedded_dao: EmbeddedDAOProtocol,
    ) -> dict[str, Any]:
        dashboard = await dao.get_by_id_or_slug(id_or_slug)
        if not dashboard:
            raise ObjectNotFoundError("Dashboard", id_or_slug)
        embedded = await embedded_dao.find_by_dashboard_id(dashboard.id)
        if not embedded:
            return {"result": None}
        return {
            "result": EmbeddedDashboardResponse(
                uuid=str(embedded.uuid),
                allowed_domains=embedded.allowed_domains or [],
                dashboard_id=str(dashboard.id),
                changed_on=str(embedded.changed_on) if embedded.changed_on else None,
            )
        }

    # ------------------------------------------------------------------
    # POST — create/update embedded
    # ------------------------------------------------------------------
    @post(
        "/{id_or_slug:str}/embedded",
        guards=[require_permission("can_write", "Dashboard")],
        status_code=200,
    )
    async def create_embedded(
        self,
        id_or_slug: str,
        data: EmbeddedDashboardConfig,
        dao: DashboardDAOProtocol,
        embedded_dao: EmbeddedDAOProtocol,
    ) -> dict[str, Any]:
        dashboard = await dao.get_by_id_or_slug(id_or_slug)
        if not dashboard:
            raise ObjectNotFoundError("Dashboard", id_or_slug)
        cmd = UpsertEmbeddedDashboardCommand(
            dao=dao,
            embedded_dao=embedded_dao,
            dashboard_id=dashboard.id,
            allowed_domains=data.allowed_domains,
        )
        embedded = await cmd.execute()
        event_logger.log(
            "dashboard.create_embedded",
            object_ref=f"dashboard:{id_or_slug}",
        )
        return {
            "result": EmbeddedDashboardResponse(
                uuid=str(embedded.uuid),
                allowed_domains=embedded.allowed_domains or [],
                dashboard_id=str(dashboard.id),
            )
        }

    # ------------------------------------------------------------------
    # PUT — update embedded (idempotent)
    # ------------------------------------------------------------------
    @put(
        "/{id_or_slug:str}/embedded",
        guards=[require_permission("can_write", "Dashboard")],
        status_code=200,
    )
    async def update_embedded(
        self,
        id_or_slug: str,
        data: EmbeddedDashboardConfig,
        dao: DashboardDAOProtocol,
        embedded_dao: EmbeddedDAOProtocol,
    ) -> dict[str, Any]:
        dashboard = await dao.get_by_id_or_slug(id_or_slug)
        if not dashboard:
            raise ObjectNotFoundError("Dashboard", id_or_slug)
        cmd = UpsertEmbeddedDashboardCommand(
            dao=dao,
            embedded_dao=embedded_dao,
            dashboard_id=dashboard.id,
            allowed_domains=data.allowed_domains,
        )
        embedded = await cmd.execute()
        event_logger.log(
            "dashboard.update_embedded",
            object_ref=f"dashboard:{id_or_slug}",
        )
        return {
            "result": EmbeddedDashboardResponse(
                uuid=str(embedded.uuid),
                allowed_domains=embedded.allowed_domains or [],
                dashboard_id=str(dashboard.id),
            )
        }

    # ------------------------------------------------------------------
    # DELETE — embedded
    # ------------------------------------------------------------------
    @delete(
        "/{id_or_slug:str}/embedded",
        guards=[require_permission("can_write", "Dashboard")],
        status_code=200,
    )
    async def delete_embedded(
        self,
        id_or_slug: str,
        dao: DashboardDAOProtocol,
        embedded_dao: EmbeddedDAOProtocol,
    ) -> dict[str, str]:
        dashboard = await dao.get_by_id_or_slug(id_or_slug)
        if not dashboard:
            raise ObjectNotFoundError("Dashboard", id_or_slug)
        cmd = DeleteEmbeddedDashboardCommand(
            dao=dao,
            embedded_dao=embedded_dao,
            dashboard_id=dashboard.id,
        )
        await cmd.execute()
        event_logger.log(
            "dashboard.delete_embedded",
            object_ref=f"dashboard:{id_or_slug}",
        )
        return {"message": "OK"}

    # ------------------------------------------------------------------
    # POST — deep copy
    # ------------------------------------------------------------------
    @post(
        "/{id_or_slug:str}/copy/",
        guards=[require_permission("can_write", "Dashboard")],
        status_code=200,
    )
    async def copy_dashboard(
        self,
        id_or_slug: str,
        data: DashboardCopySchema,
        dao: DashboardDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
    ) -> dict[str, Any]:
        dashboard = await dao.get_by_id_or_slug(id_or_slug)
        if not dashboard:
            raise ObjectNotFoundError("Dashboard", id_or_slug)
        cmd = CopyDashboardCommand(
            dao=dao,
            dashboard_id=dashboard.id,
            data={
                "dashboard_title": data.dashboard_title,
                "css": data.css,
                "json_metadata": data.json_metadata,
                "duplicate_slices": data.duplicate_slices,
            },
            security_manager=security_manager,
            current_user=current_user,
        )
        new_dash = await cmd.execute()
        event_logger.log(
            "dashboard.copy",
            object_ref=f"dashboard:{id_or_slug}",
            user_id=current_user.id,
        )
        return {
            "result": {
                "id": new_dash.id,
                "last_modified_time": getattr(new_dash, "changed_on", None),
            }
        }

    # ------------------------------------------------------------------
    # Permalink endpoints (merged from DashboardPermalinkController)
    # ------------------------------------------------------------------

    @post(
        "/{pk:int}/permalink",
        guards=[require_permission("can_write", "DashboardPermalinkRestApi")],
        status_code=201,
    )
    async def create_permalink(
        self,
        pk: int,
        data: DashboardPermalinkSchema,
        dao: DashboardDAOProtocol,
        kv_dao: KeyValueDAOProtocol,
    ) -> dict[str, str]:
        dashboard = await dao.find_by_id(pk)
        if not dashboard:
            raise ObjectNotFoundError("Dashboard", pk)
        dashboard_uuid = str(getattr(dashboard, "uuid", "")) or None
        state: dict[str, Any] = {
            "dataMask": data.dataMask,
            "activeTabs": data.activeTabs,
            "anchor": data.anchor,
            "urlParams": data.urlParams,
        }
        cmd = CreateDashboardPermalinkCommand(
            dao=kv_dao,
            dashboard_id=pk,
            state=state,
            dashboard_uuid=dashboard_uuid,
        )
        key = await cmd.execute()
        event_logger.log("dashboard.create_permalink", object_ref=f"dashboard:{pk}")
        return {"key": key, "url": f"/api/v1/dashboard/permalink/{key}"}

    @get(
        "/permalink/{key:str}",
        guards=[require_permission("can_read", "DashboardPermalinkRestApi")],
    )
    async def get_permalink(
        self, key: str, kv_dao: KeyValueDAOProtocol
    ) -> dict[str, Any]:
        cmd = GetDashboardPermalinkCommand(dao=kv_dao, key=key)
        state = await cmd.execute()
        event_logger.log("dashboard.get_permalink", object_ref=f"permalink:{key}")
        return state
