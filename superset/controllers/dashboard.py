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
from typing import Any, cast, TYPE_CHECKING

from litestar import Controller, delete, get, post, put
from litestar.connection import Request
from litestar.datastructures import State, UploadFile
from litestar.di import Provide
from litestar.enums import RequestEncodingType
from litestar.params import Body, Parameter
from litestar.response import Response, Stream

from superset.commands.dashboard.copy import CopyDashboardCommand
from superset.commands.dashboard.create import CreateDashboardCommand
from superset.commands.dashboard.delete import (
    BulkDeleteDashboardsCommand,
    DeleteDashboardCommand,
    DeleteEmbeddedDashboardCommand,
)
from superset.commands.dashboard.embedded.upsert import UpsertEmbeddedDashboardCommand
from superset.commands.dashboard.export import ExportDashboardsCommand
from superset.commands.dashboard.importers.v1 import ImportDashboardsCommand
from superset.commands.dashboard.permalink.create import (
    CreateDashboardPermalinkCommand,
)
from superset.commands.dashboard.permalink.get import GetDashboardPermalinkCommand
from superset.commands.dashboard.update import (
    UpdateDashboardColorsCommand,
    UpdateDashboardCommand,
    UpdateDashboardFiltersCommand,
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
from superset.guards.rbac import (
    deny_anon_with_404,
    require_permission,
)
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
    DashboardDetailResult,
    DashboardFiltersUpdateSchema,
    DashboardGetResponse,
    DashboardPermalinkSchema,
    DashboardPostSchema,
    DashboardPutSchema,
    DashboardScreenshotSchema,
    EmbeddedDashboardConfig,
    EmbeddedDashboardResponse,
)
from superset.typing import (
    DashboardDAOProtocol,
    EmbeddedDAOProtocol,
    KeyValueDAOProtocol,
    SecurityManagerProtocol,
    UserProtocol,
)
from superset.utils import filter_none, filter_unset

if TYPE_CHECKING:
    from superset.db.daos.dashboard import AsyncDashboardDAO, AsyncEmbeddedDashboardDAO
    from superset.db.daos.key_value import AsyncKeyValueDAO


# ---------------------------------------------------------------------------
# Custom RISON filters for dashboards
# ---------------------------------------------------------------------------
def _dashboard_custom_filters(current_user: Any) -> dict[str, Any]:
    def _dashboard_is_favorite(model_cls: Any, value: Any) -> Any:
        from sqlalchemy import select as sa_select

        from superset.models.core import FavStar

        user_id = getattr(current_user, "id", None)
        if user_id is None:
            return None
        fav_subq = sa_select(FavStar.obj_id).where(
            FavStar.class_name == "Dashboard",
            FavStar.user_id == user_id,
        )
        if value:
            return model_cls.id.in_(fav_subq)
        return ~model_cls.id.in_(fav_subq)

    def _dashboard_is_certified(model_cls: Any, value: Any) -> Any:
        if value:
            return model_cls.certified_by.isnot(None)
        return model_cls.certified_by.is_(None)

    return {
        "dashboard_is_favorite": _dashboard_is_favorite,
        "dashboard_is_certified": _dashboard_is_certified,
    }


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
        from sqlalchemy.orm import selectinload

        from superset.db.filters import dashboard_access_filters
        from superset.models.dashboard import Dashboard

        rison_filters, order_by, page, page_size = build_rison_query_params(
            Dashboard,
            rison_params,
            custom_filters=_dashboard_custom_filters(current_user),
        )
        # Default ordering: changed_on desc (matches original base_order)
        if order_by is None:
            order_by = [Dashboard.changed_on.desc()]

        base_filters = await dashboard_access_filters(security_manager, current_user)
        all_filters = (base_filters or []) + rison_filters

        dashboards = await dao.find_all(
            filters=all_filters or None,
            page=page,
            page_size=page_size,
            order_by=order_by,
            options=[
                selectinload(Dashboard.owners),
                selectinload(Dashboard.roles),
                selectinload(Dashboard.tags),
                selectinload(Dashboard.changed_by),
                selectinload(Dashboard.created_by),
            ],
        )
        total = await dao.count(filters=all_filters or None)
        await event_logger.alog_with_context("dashboard.list")
        return serialize_list_response(
            dashboards,
            total,
            [
                "id",
                "uuid",
                "published",
                "status",
                "slug",
                "url",
                "dashboard_title",
                "thumbnail_url",
                "certified_by",
                "certification_details",
                "changed_by_name",
                "changed_by.first_name",
                "changed_by.last_name",
                "changed_by.id",
                "changed_on_utc",
                "changed_on_delta_humanized",
                "created_on_delta_humanized",
                "created_by.first_name",
                "created_by.id",
                "created_by.last_name",
                "owners.id",
                "owners.first_name",
                "owners.last_name",
                "roles.id",
                "roles.name",
                "tags.id",
                "tags.name",
                "tags.type",
                "is_managed_externally",
            ],
            list_title="List Dashboard",
            order_columns=[
                "changed_by.first_name",
                "changed_on_delta_humanized",
                "created_by.first_name",
                "dashboard_title",
                "published",
                "changed_on",
            ],
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
            # Mirrors the original FAB-generated permission list exposed
            # by ``/api/v1/dashboard/_info``; the list order is preserved to
            # match what Cypress snapshots assume.
            permissions=[
                "can_read",
                "can_get_embedded",
                "can_delete_embedded",
                "can_export",
                "can_cache_dashboard_screenshot",
                "can_set_embedded",
                "can_write",
            ],
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
        from sqlalchemy.orm import selectinload

        from superset.db.filters import dashboard_access_filters
        from superset.models.dashboard import Dashboard

        # Build query with eager loading for all relationships
        id_filter = (
            Dashboard.id == int(id_or_slug)
            if id_or_slug.isdigit()
            else Dashboard.slug == id_or_slug
        )
        base_filters = await dashboard_access_filters(security_manager, current_user)
        all_filters = [id_filter] + (base_filters or [])

        dashboard = await dao.find_with_filters_and_options(
            filters=all_filters,
            options=[
                selectinload(Dashboard.owners),
                selectinload(Dashboard.roles),
                selectinload(Dashboard.tags),
                selectinload(Dashboard.changed_by),
                selectinload(Dashboard.created_by),
                selectinload(Dashboard.slices),
                selectinload(Dashboard.theme),
            ],
        )
        if not dashboard:
            raise ObjectNotFoundError("Dashboard", id_or_slug)

        await event_logger.alog_with_context(
            "dashboard.get", object_ref=f"dashboard:{id_or_slug}"
        )
        return DashboardGetResponse(
            id=dashboard.id,
            result=DashboardDetailResult.from_model(dashboard),
        )

    # ------------------------------------------------------------------
    # GET — related datasets
    # ------------------------------------------------------------------
    @get(
        "/{id_or_slug:str}/datasets",
        guards=[require_permission("can_read", "Dashboard")],
    )
    async def get_datasets(  # noqa: C901  # complex business logic
        self,
        id_or_slug: str,
        dao: DashboardDAOProtocol,
    ) -> dict[str, Any]:
        dashboard = await dao.get_by_id_or_slug(id_or_slug)
        if not dashboard:
            raise ObjectNotFoundError("Dashboard", id_or_slug)
        datasets = await dao.get_datasets_for_dashboard(dashboard)

        def _resolve_generic_type(sqla_type: Any, is_dttm: bool) -> int | None:
            """Map SQLAlchemy column type → utils.GenericDataType int.
            0=NUMERIC, 1=STRING, 2=TEMPORAL, 3=BOOLEAN.  Matches
            ``TableColumn.type_generic`` in superset_old without
            requiring a live db_engine_spec lookup (which needs a
            database connection in async context).
            """
            if is_dttm:
                return 2  # TEMPORAL
            if not sqla_type:
                return None
            t = str(sqla_type).upper()
            if "BOOL" in t:
                return 3
            if any(k in t for k in ("DATE", "TIME", "TIMESTAMP")):
                return 2
            if any(k in t for k in ("CHAR", "TEXT", "STRING", "JSON", "UUID")):
                return 1
            if any(
                k in t
                for k in (
                    "INT",
                    "NUMERIC",
                    "DECIMAL",
                    "FLOAT",
                    "DOUBLE",
                    "REAL",
                    "BIGINT",
                    "SMALLINT",
                )
            ):
                return 0
            return None

        def _build_dataset_dict(ds: Any) -> dict[str, Any]:
            columns = getattr(ds, "columns", []) or []
            metrics = getattr(ds, "metrics", []) or []
            owners = getattr(ds, "owners", []) or []
            database = getattr(ds, "database", None)
            table_name = getattr(ds, "table_name", None) or ""
            ds_id: int = ds.id
            main_dttm_col = getattr(ds, "main_dttm_col", None)

            column_names = [
                c.column_name for c in columns if getattr(c, "column_name", None)
            ]
            # Unique set of generic column types (matches
            # ``superset_old/connectors/sqla/models.py:482``)
            column_types: list[int] = []
            seen_types: set[int] = set()
            for c in columns:
                gt = _resolve_generic_type(
                    getattr(c, "type", None),
                    getattr(c, "is_dttm", False),
                )
                if gt is not None and gt not in seen_types:
                    seen_types.add(gt)
                    column_types.append(gt)
            verbose_map = {
                c.column_name: (getattr(c, "verbose_name", None) or c.column_name)
                for c in columns
                if getattr(c, "column_name", None)
            }
            cols_dicts = [
                {
                    "column_name": c.column_name,
                    "verbose_name": getattr(c, "verbose_name", None),
                    "is_dttm": getattr(c, "is_dttm", False),
                    "type": getattr(c, "type", None),
                    "groupby": getattr(c, "groupby", True),
                    "filterable": getattr(c, "filterable", True),
                    "expression": getattr(c, "expression", None),
                }
                for c in columns
                if getattr(c, "column_name", None)
            ]
            metrics_dicts = [
                {
                    "metric_name": m.metric_name,
                    "verbose_name": getattr(m, "verbose_name", None),
                    "expression": getattr(m, "expression", None),
                }
                for m in metrics
                if getattr(m, "metric_name", None)
            ]
            granularity_sqla = [
                c.column_name
                for c in columns
                if getattr(c, "is_dttm", False) and getattr(c, "column_name", None)
            ]
            # Build ``order_by_choices`` in the shape expected by the
            # frontend (json-encoded string + human label). See
            # ``superset/schemas/dataset.py._resolve_order_by_choices``
            # for the full rationale — raw [col, bool] pairs break the
            # table viz ``order_by_cols`` SelectControl hydration.
            import json as _json

            order_by_choices: list[list[Any]] = []
            for c in columns:
                col_name = getattr(c, "column_name", None)
                if col_name:
                    order_by_choices.append(
                        [_json.dumps([col_name, True]), f"{col_name} [asc]"]
                    )
                    order_by_choices.append(
                        [_json.dumps([col_name, False]), f"{col_name} [desc]"]
                    )
            owners_list = [
                {
                    "id": o.id,
                    "first_name": getattr(o, "first_name", ""),
                    "last_name": getattr(o, "last_name", ""),
                }
                for o in owners
            ]
            db_dict = (
                {
                    "id": database.id,
                    "database_name": getattr(database, "database_name", ""),
                    "backend": getattr(database, "backend", None),
                }
                if database
                else None
            )
            return {
                "id": ds_id,
                "uid": f"{ds_id}__table",
                "table_name": table_name,
                "name": table_name,
                "datasource_name": table_name,
                "type": "table",
                "schema": getattr(ds, "schema", None),
                "catalog": getattr(ds, "catalog", None),
                "database": db_dict,
                "filter_select_enabled": getattr(ds, "filter_select_enabled", False),
                "is_sqllab_view": getattr(ds, "is_sqllab_view", False),
                "main_dttm_col": main_dttm_col,
                "offset": getattr(ds, "offset", 0),
                "cache_timeout": getattr(ds, "cache_timeout", None),
                "default_endpoint": getattr(ds, "default_endpoint", None),
                "fetch_values_predicate": getattr(ds, "fetch_values_predicate", None),
                "template_params": getattr(ds, "template_params", None),
                "params": getattr(ds, "params", None),
                "perm": getattr(ds, "perm", None),
                "sql": getattr(ds, "sql", None),
                "extra": getattr(ds, "extra", None),
                "normalize_columns": getattr(ds, "normalize_columns", False),
                "always_filter_main_dttm": getattr(
                    ds, "always_filter_main_dttm", False
                ),
                "edit_url": f"/tablemodelview/edit/{ds_id}",
                "column_names": column_names,
                "column_formats": {},
                "column_types": column_types,
                "verbose_map": verbose_map,
                "columns": cols_dicts,
                "metrics": metrics_dicts,
                "granularity_sqla": granularity_sqla,
                "time_grain_sqla": [],
                "order_by_choices": order_by_choices,
                "owners": owners_list,
                "select_star": None,
                # ``filter_select`` is a legacy alias kept for frontend
                # compatibility (TODO deprecate — see superset_old
                # connectors/sqla/models.py:375).
                "filter_select": getattr(ds, "filter_select_enabled", False),
                "health_check_message": getattr(ds, "health_check_message", None),
            }

        return {"result": [_build_dataset_dict(ds) for ds in datasets]}

    # ------------------------------------------------------------------
    # GET — tab structure
    # ------------------------------------------------------------------
    @get(
        "/{id_or_slug:str}/tabs",
        guards=[require_permission("can_read", "Dashboard")],
    )
    async def get_tabs(  # noqa: C901
        self,
        id_or_slug: str,
        dao: DashboardDAOProtocol,
    ) -> dict[str, Any]:
        import json as _json
        from collections import deque

        dashboard = await dao.get_by_id_or_slug(id_or_slug)
        if not dashboard:
            raise ObjectNotFoundError("Dashboard", id_or_slug)

        if not dashboard.position_json:
            return {"all_tabs": {}, "tab_tree": []}

        try:
            position = _json.loads(dashboard.position_json)
        except (ValueError, TypeError):
            return {"all_tabs": {}, "tab_tree": []}

        all_tabs: dict[str, str] = {}
        tab_tree: list[dict[str, Any]] = []

        def get_node(node_id: str) -> dict[str, Any]:
            return position.get(node_id, {})

        def build_tab_tree(
            node: dict[str, Any], children: list[dict[str, Any]]
        ) -> None:
            new_children: list[dict[str, Any]] = []
            for child_id in node.get("children", []):
                child = get_node(child_id)
                if not child:
                    continue
                node_type = node.get("type", "")
                if node_type == "TABS":
                    children.append(child)
                    queue.append((child, new_children))
                elif node_type in ("GRID", "ROOT"):
                    queue.append((child, children))
                elif node_type == "TAB":
                    queue.append((child, new_children))
            if node.get("type") == "TAB":
                meta = node.get("meta", {})
                title = meta.get("text") or meta.get("defaultText") or ""
                node_id = node.get("id", "")
                node["children"] = new_children
                node["title"] = title
                node["value"] = node_id
                node["parents"] = node.get("parents", [])
                all_tabs[node_id] = title

        root = get_node("ROOT_ID")
        if not root:
            return {"all_tabs": {}, "tab_tree": []}

        queue: deque[tuple[dict[str, Any], list[dict[str, Any]]]] = deque()
        queue.append((root, tab_tree))
        while queue:
            node, children = queue.popleft()
            build_tab_tree(node, children)

        return {"all_tabs": all_tabs, "tab_tree": tab_tree}

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

        result = []
        for chart in charts:
            # Use Slice.form_data property — applies update_time_range()
            # which migrates since/until → time_range.
            fd = chart.form_data

            desc = getattr(chart, "description", None) or ""
            result.append(
                {
                    "id": chart.id,
                    "slice_name": chart.slice_name,
                    "cache_timeout": getattr(chart, "cache_timeout", None),
                    "changed_on": (
                        chart.changed_on.isoformat()
                        if getattr(chart, "changed_on", None)
                        else None
                    ),
                    "description": desc or None,
                    "description_markeddown": (f"<p>{desc}</p>" if desc else ""),
                    "form_data": fd,
                    "slice_url": getattr(chart, "slice_url", None),
                    "certified_by": getattr(chart, "certified_by", None),
                    "certification_details": getattr(
                        chart, "certification_details", None
                    ),
                }
            )
        return {"result": result}

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
        security_manager: SecurityManagerProtocol,
    ) -> DashboardGetResponse:
        cmd = CreateDashboardCommand(
            dao=cast("AsyncDashboardDAO", dao),
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
                    "owners": data.owners,
                    "roles": data.roles,
                    "tags": data.tags,
                    "theme_id": data.theme_id,
                    "uuid": data.uuid,
                }
            ),
            user_id=current_user.id,
            security_manager=security_manager,
        )
        dashboard = await cmd.execute()
        dashboard_id = int(dashboard.id)
        await event_logger.alog_with_context(
            "dashboard.create",
            object_ref=f"dashboard:{dashboard_id}",
            user_id=current_user.id,
        )
        return DashboardGetResponse(
            id=dashboard_id,
            result=DashboardDetailResult.from_model_brief(dashboard),
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
                "owners": data.owners,
                "roles": data.roles,
                "tags": data.tags,
                "theme_id": data.theme_id,
                "uuid": data.uuid,
            }
        )
        cmd = UpdateDashboardCommand(
            dao=cast("AsyncDashboardDAO", dao),
            dashboard_id=pk,
            data=update_data,
            security_manager=security_manager,
            user_id=current_user.id,
        )
        dashboard = await cmd.execute()

        # Eager-load relationships for serialization (avoids lazy load on async)
        from sqlalchemy.orm import selectinload

        from superset.models.dashboard import Dashboard

        refreshed = await dao.find_by_id_with_options(
            dashboard_id=dashboard.id,
            options=[
                selectinload(Dashboard.owners),
                selectinload(Dashboard.roles),
                selectinload(Dashboard.tags),
                selectinload(Dashboard.changed_by),
                selectinload(Dashboard.created_by),
                selectinload(Dashboard.slices),
                selectinload(Dashboard.theme),
            ],
        )
        assert refreshed is not None  # just updated, must exist
        dashboard = refreshed

        await event_logger.alog_with_context(
            "dashboard.update",
            object_ref=f"dashboard:{pk}",
            user_id=current_user.id,
        )
        changed_on = getattr(dashboard, "changed_on", None)
        last_modified_time = (
            changed_on.replace(microsecond=0).timestamp() if changed_on else None
        )
        return DashboardGetResponse(
            id=int(dashboard.id),
            result=DashboardDetailResult.from_model(dashboard),
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
            dao=cast("AsyncDashboardDAO", dao),
            dashboard_id=pk,
            data={
                "deleted": data.deleted,
                "modified": data.modified,
                "reordered": data.reordered,
            },
            security_manager=security_manager,
            user_id=current_user.id,
        )
        updated_configuration = await cmd.execute()
        await event_logger.alog_with_context(
            "dashboard.update_filters",
            object_ref=f"dashboard:{pk}",
            user_id=current_user.id,
        )
        return {"result": updated_configuration}

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
        mark_updated: bool = Parameter(query="mark_updated", default=True),  # noqa: B008
    ) -> Response[None]:
        cmd = UpdateDashboardColorsCommand(
            dao=cast("AsyncDashboardDAO", dao),
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
            mark_updated=mark_updated,
        )
        await cmd.execute()
        await event_logger.alog_with_context(
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
            dao=cast("AsyncDashboardDAO", dao),
            dashboard_id=pk,
            security_manager=security_manager,
            user_id=current_user.id,
        )
        await cmd.execute()
        await event_logger.alog_with_context(
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
        rison_params: list[int] | dict[str, Any] | None,
    ) -> dict[str, str]:
        ids = extract_ids_required(rison_params)
        cmd = BulkDeleteDashboardsCommand(
            dao=cast("AsyncDashboardDAO", dao),
            dashboard_ids=ids,
            security_manager=security_manager,
            user_id=current_user.id,
        )
        await cmd.execute()
        await event_logger.alog_with_context(
            "dashboard.bulk_delete",
            user_id=current_user.id,
            extra={"count": len(ids)},
        )
        num = len(ids)
        msg = f"Deleted {num} dashboard" if num == 1 else f"Deleted {num} dashboards"
        return {"message": msg}

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
        rison_params: list[int] | dict[str, Any] | None,
        token: str | None = Parameter(query="token", default=None),
    ) -> Stream:
        ids = extract_ids(rison_params)
        if not ids:
            raise CommandInvalidError("At least one ID is required for export")
        cmd = ExportDashboardsCommand(model_ids=ids, dao=cast("AsyncDashboardDAO", dao))
        buf = await cmd.execute()
        await event_logger.alog_with_context(
            "dashboard.export", extra={"count": len(ids)}
        )
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
    ) -> Response[dict[str, Any]]:
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
        return Response(
            content={
                "cache_key": cache_key,
                "dashboard_url": f"/superset/dashboard/{pk}/",
                "image_url": f"/api/v1/dashboard/{pk}/screenshot/{cache_key}/",
                "task_status": "not_available",
                "task_updated_at": None,
            },
            status_code=202,
            media_type="application/json",
        )

    # ------------------------------------------------------------------
    # GET — screenshot
    # ------------------------------------------------------------------
    @get(
        "/{pk:int}/screenshot/{digest:str}/",
        guards=[require_permission("can_read", "Dashboard")],
        media_type="image/png",
    )
    async def screenshot(
        self,
        pk: int,
        digest: str,
        dao: DashboardDAOProtocol,
        state: State,
        request: Request,  # type: ignore[type-arg]
    ) -> Response[bytes]:
        """Get a computed dashboard screenshot from cache.

        The *digest* path parameter is the cache key.  If the cache
        contains an image we serve it (as PNG or converted to PDF
        depending on the ``download_format`` query parameter);
        otherwise we return 404.
        """
        import asyncio

        # Check feature flag BEFORE importing screenshots — that module's
        # import chain pulls in webdriver/playwright/selenium which can
        # blow up in deployments where the dependencies are not present.
        # When THUMBNAILS is disabled we should respond 404 without ever
        # touching the optional code path.
        settings = getattr(state, "settings", None)
        flags = getattr(settings, "feature_flags", {}) or {}
        if not flags.get("THUMBNAILS", False) or not flags.get(
            "ENABLE_DASHBOARD_SCREENSHOT_ENDPOINTS", False
        ):
            raise ObjectNotFoundError("Dashboard screenshot", pk)

        from superset.utils.screenshots import (
            DashboardScreenshot,
            ScreenshotImageNotAvailableException,
        )

        dashboard = await dao.find_by_id(pk)
        if not dashboard:
            raise ObjectNotFoundError("Dashboard", pk)

        download_format = request.query_params.get("download_format", "png")

        cache_payload = await asyncio.to_thread(
            DashboardScreenshot.get_from_cache_key, digest
        )
        if cache_payload is not None:
            try:
                image = cache_payload.get_image()
            except ScreenshotImageNotAvailableException:
                return Response(content=b"", status_code=404, media_type="image/png")

            if download_format == "pdf":
                from superset.utils.pdf import build_pdf_from_screenshots

                pdf_data = build_pdf_from_screenshots([image.getvalue()])
                return Response(
                    content=pdf_data,
                    status_code=200,
                    media_type="application/pdf",
                    headers={
                        "Content-Disposition": "inline; filename=dashboard.pdf",
                    },
                )
            # Default: PNG
            return Response(
                content=image.getvalue(),
                status_code=200,
                media_type="image/png",
            )
        return Response(content=b"", status_code=404, media_type="image/png")

    # ------------------------------------------------------------------
    # GET — thumbnail
    # ------------------------------------------------------------------
    @get(
        "/{pk:int}/thumbnail/{digest:str}/",
        guards=[require_permission("can_read", "Dashboard")],
        media_type="image/png",
    )
    async def thumbnail(
        self,
        pk: int,
        digest: str,
        dao: DashboardDAOProtocol,
        state: State,
        current_user: UserProtocol,
    ) -> Response[bytes]:
        """Compute or get already computed dashboard thumbnail from cache.

        If the dashboard's current digest differs from *digest* we redirect
        to the canonical URL.  Otherwise we check the thumbnail cache: if
        the image exists we serve it directly; if not we queue a Celery task
        and return 202.
        """
        import asyncio

        from litestar.response import Redirect

        # Gate on the feature flag *before* importing screenshots so the
        # optional webdriver dependency chain is never touched when
        # thumbnails are disabled (see screenshot endpoint above).
        settings = getattr(state, "settings", None)
        flags = getattr(settings, "feature_flags", {}) or {}
        if not flags.get("THUMBNAILS", False):
            raise ObjectNotFoundError("Dashboard thumbnail", pk)

        from superset.utils.screenshots import (
            DashboardScreenshot,
            ScreenshotCachePayload,
            ScreenshotImageNotAvailableException,
        )

        dashboard = await dao.find_by_id(pk)
        if not dashboard:
            raise ObjectNotFoundError("Dashboard", pk)

        # Redirect to canonical digest URL if stale
        dashboard_digest = getattr(dashboard, "digest", None)
        if dashboard_digest and dashboard_digest != digest:
            return Redirect(
                path=f"/api/v1/dashboard/{pk}/thumbnail/{dashboard_digest}/",
            )

        # Build screenshot object and compute cache key
        dashboard_url = f"/superset/dashboard/{pk}/"
        screenshot_obj = DashboardScreenshot(dashboard_url, dashboard_digest or digest)
        cache_key = await asyncio.to_thread(screenshot_obj.get_cache_key)
        cache_payload = (
            await asyncio.to_thread(DashboardScreenshot.get_from_cache_key, cache_key)
            or ScreenshotCachePayload()
        )

        if cache_payload.should_trigger_task():
            # Mark as pending in cache and dispatch Celery task
            await asyncio.to_thread(
                screenshot_obj.cache.set,
                cache_key,
                ScreenshotCachePayload().to_dict(),
            )
            from superset.tasks.thumbnails import cache_dashboard_thumbnail

            cache_dashboard_thumbnail.delay(
                current_user=getattr(current_user, "username", None),
                dashboard_id=dashboard.id,
                force=False,
                cache_key=cache_key,
            )
            return Response(
                content=b"",
                status_code=202,
                media_type="image/png",
            )

        # Serve from cache
        try:
            image = cache_payload.get_image()
            # Validate the BytesIO object
            if not image or not hasattr(image, "read"):
                return Response(content=b"", status_code=404, media_type="image/png")
            if image.getbuffer().nbytes == 0:
                return Response(content=b"", status_code=404, media_type="image/png")
            image.seek(0)
        except ScreenshotImageNotAvailableException:
            return Response(content=b"", status_code=404, media_type="image/png")
        return Response(
            content=image.read(),
            status_code=200,
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
        rison_params: list[int] | dict[str, Any] | None,
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
        await event_logger.alog_with_context(
            "dashboard.add_favorite",
            object_ref=f"dashboard:{pk}",
            user_id=current_user.id,
        )
        return {"result": "OK"}

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
        await event_logger.alog_with_context(
            "dashboard.remove_favorite",
            object_ref=f"dashboard:{pk}",
            user_id=current_user.id,
        )
        return {"result": "OK"}

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
        ssh_tunnel_private_keys: str | None = None,
        ssh_tunnel_private_key_passwords: str | None = None,
    ) -> dict[str, str]:
        import json as _json

        contents = await data.read()
        buf = io.BytesIO(contents)
        try:
            passwords_dict: dict[str, str] = _json.loads(passwords) if passwords else {}
        except (ValueError, _json.JSONDecodeError) as exc:
            raise CommandInvalidError("Invalid JSON in 'passwords' field") from exc
        try:
            ssh_dict: dict[str, str] = (
                _json.loads(ssh_tunnel_passwords) if ssh_tunnel_passwords else {}
            )
        except (ValueError, _json.JSONDecodeError) as exc:
            raise CommandInvalidError(
                "Invalid JSON in 'ssh_tunnel_passwords' field"
            ) from exc
        try:
            ssh_private_keys_dict: dict[str, str] = (
                _json.loads(ssh_tunnel_private_keys) if ssh_tunnel_private_keys else {}
            )
        except (ValueError, _json.JSONDecodeError) as exc:
            raise CommandInvalidError(
                "Invalid JSON in 'ssh_tunnel_private_keys' field"
            ) from exc
        try:
            ssh_private_key_passwords_dict: dict[str, str] = (
                _json.loads(ssh_tunnel_private_key_passwords)
                if ssh_tunnel_private_key_passwords
                else {}
            )
        except (ValueError, _json.JSONDecodeError) as exc:
            raise CommandInvalidError(
                "Invalid JSON in 'ssh_tunnel_private_key_passwords' field"
            ) from exc
        cmd = ImportDashboardsCommand(
            contents=buf,
            dao=cast("AsyncDashboardDAO", dao),
            overwrite=overwrite,
            passwords=passwords_dict,
            ssh_tunnel_passwords=ssh_dict,
            ssh_tunnel_private_keys=ssh_private_keys_dict,
            ssh_tunnel_private_key_passwords=ssh_private_key_passwords_dict,
        )
        await cmd.execute()
        await event_logger.alog_with_context("dashboard.import")
        return {"message": "OK"}

    # ------------------------------------------------------------------
    # GET — embedded config
    # ------------------------------------------------------------------
    @get(
        "/{id_or_slug:str}/embedded",
        guards=[
            deny_anon_with_404,
            require_permission("can_read", "Dashboard"),
        ],
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
            dao=cast("AsyncDashboardDAO", dao),
            embedded_dao=cast("AsyncEmbeddedDashboardDAO", embedded_dao),
            dashboard_id=dashboard.id,
            allowed_domains=data.allowed_domains,
        )
        embedded = await cmd.execute()
        await event_logger.alog_with_context(
            "dashboard.create_embedded",
            object_ref=f"dashboard:{id_or_slug}",
        )
        _raw = embedded.allow_domain_list
        _domains: list[str] = [d for d in (_raw or "").split(",") if d]
        return {
            "result": EmbeddedDashboardResponse(
                uuid=str(embedded.uuid),
                allowed_domains=_domains,
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
            dao=cast("AsyncDashboardDAO", dao),
            embedded_dao=cast("AsyncEmbeddedDashboardDAO", embedded_dao),
            dashboard_id=dashboard.id,
            allowed_domains=data.allowed_domains,
        )
        embedded = await cmd.execute()
        await event_logger.alog_with_context(
            "dashboard.update_embedded",
            object_ref=f"dashboard:{id_or_slug}",
        )
        _raw2 = embedded.allow_domain_list
        _domains2: list[str] = [d for d in (_raw2 or "").split(",") if d]
        return {
            "result": EmbeddedDashboardResponse(
                uuid=str(embedded.uuid),
                allowed_domains=_domains2,
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
            dao=cast("AsyncDashboardDAO", dao),
            embedded_dao=cast("AsyncEmbeddedDashboardDAO", embedded_dao),
            dashboard_id=dashboard.id,
        )
        await cmd.execute()
        await event_logger.alog_with_context(
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
            dao=cast("AsyncDashboardDAO", dao),
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
        await event_logger.alog_with_context(
            "dashboard.copy",
            object_ref=f"dashboard:{id_or_slug}",
            user_id=current_user.id,
        )
        changed_on = getattr(new_dash, "changed_on", None)
        return {
            "result": {
                "id": new_dash.id,
                "last_modified_time": (
                    changed_on.replace(microsecond=0).timestamp()
                    if changed_on
                    else None
                ),
            }
        }

    # ------------------------------------------------------------------
    # Permalink endpoints (merged from DashboardPermalinkController)
    # ------------------------------------------------------------------

    @post(
        "/{pk:int}/permalink",
        guards=[
            deny_anon_with_404,
            require_permission("can_write", "DashboardPermalinkRestApi"),
        ],
        status_code=201,
    )
    async def create_permalink(
        self,
        pk: int,
        data: DashboardPermalinkSchema,
        dao: DashboardDAOProtocol,
        kv_dao: KeyValueDAOProtocol,
        current_user: UserProtocol,
    ) -> dict[str, str]:
        dashboard = await dao.find_by_id(pk)
        if not dashboard:
            raise ObjectNotFoundError("Dashboard", pk)
        dashboard_uuid = str(getattr(dashboard, "uuid", "")) or None
        state: dict[str, Any] = {
            "dataMask": data.data_mask,
            "activeTabs": data.active_tabs,
            "anchor": data.anchor,
            "urlParams": data.url_params,
        }
        cmd = CreateDashboardPermalinkCommand(
            dao=cast("AsyncKeyValueDAO", kv_dao),
            dashboard_id=pk,
            state=state,
            dashboard_uuid=dashboard_uuid,
            user_id=current_user.id,
        )
        key = await cmd.execute()
        await event_logger.alog_with_context(
            "dashboard.create_permalink", object_ref=f"dashboard:{pk}"
        )
        return {"key": key, "url": f"/api/v1/dashboard/permalink/{key}"}

    @get(
        "/permalink/{key:str}",
        guards=[require_permission("can_read", "DashboardPermalinkRestApi")],
    )
    async def get_permalink(
        self, key: str, kv_dao: KeyValueDAOProtocol
    ) -> dict[str, Any]:
        cmd = GetDashboardPermalinkCommand(
            dao=cast("AsyncKeyValueDAO", kv_dao), key=key
        )
        state = await cmd.execute()
        await event_logger.alog_with_context(
            "dashboard.get_permalink", object_ref=f"permalink:{key}"
        )
        return state
