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
"""Chart controller — 18 endpoints for chart CRUD,
export/import, favorites, screenshots, chart data queries, cached results,
and viz_types listing."""

from __future__ import annotations

import io
import json as _json
from typing import Any

from litestar import Controller, delete, get, post, put
from litestar.connection import Request
from litestar.datastructures import State, UploadFile
from litestar.di import Provide
from litestar.enums import RequestEncodingType
from litestar.params import Body
from litestar.response import Response, Stream

from liteset.commands.chart import (
    BulkDeleteChartsCommand,
    CreateChartCommand,
    DeleteChartCommand,
    ExportChartsCommand,
    ImportChartsCommand,
    UpdateChartCommand,
    WarmUpChartCacheCommand,
)
from liteset.commands.chart_data import ChartDataCommand, GetCachedChartDataCommand
from liteset.common.query_context import AsyncQueryContext
from liteset.common.query_context_processor import AsyncQueryContextProcessor
from liteset.common.query_object import AsyncQueryObject
from liteset.controllers.base import (
    extract_ids,
    extract_ids_required,
    extract_pagination,
    get_distinct_payload,
    get_info_payload,
    get_related_payload,
    serialize_list_response,
    stream_zip,
)
from liteset.exceptions import CommandInvalidError, ObjectNotFoundError
from liteset.guards.rbac import require_permission
from liteset.params.rison import provide_rison_query
from liteset.providers import provide_chart_dao, provide_datasource_dao
from liteset.schemas.base import FavoriteStatusItem, FavoriteStatusResponse
from liteset.schemas.chart import (
    ChartCacheScreenshotResponse,
    ChartCacheWarmUpRequest,
    ChartDataQueryContext,
    ChartGetResponse,
    ChartPostBody,
    ChartPutBody,
)
from liteset.typing import (
    ChartDAOProtocol,
    DatasourceDAOProtocol,
    SecurityManagerProtocol,
    UserProtocol,
)
from liteset.events import event_logger
from liteset.utils import filter_none, filter_unset


class ChartController(Controller):
    path = "/api/v1/chart"
    tags = ["Charts"]
    dependencies = {
        "dao": Provide(provide_chart_dao, sync_to_thread=False),
        "ds_dao": Provide(provide_datasource_dao, sync_to_thread=False),
        "rison_params": Provide(provide_rison_query),
    }

    @get(
        "/",
        guards=[require_permission("can_read", "Chart")],
    )
    async def get_list(
        self,
        dao: ChartDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
        rison_params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """GET /api/v1/chart/ — list charts with optional filtering/pagination."""
        from liteset.db.filters import chart_access_filters

        # TODO(liteset/remaining-api): Implement Rison-based
        # filtering, sorting, pagination
        page, page_size = extract_pagination(rison_params)
        base_filters = await chart_access_filters(security_manager, current_user)
        charts = await dao.find_all(
            filters=base_filters or None, page=page, page_size=page_size
        )
        total = await dao.count(filters=base_filters or None)
        event_logger.log("chart.list")
        return serialize_list_response(charts, total, ["id", "slice_name", "viz_type"])

    @get(
        "/_info",
        guards=[require_permission("can_read", "Chart")],
    )
    async def info(self, dao: ChartDAOProtocol) -> dict[str, Any]:
        """GET /api/v1/chart/_info — API metadata for frontend."""
        return await get_info_payload(
            dao=dao,
            model_name="Chart",
            permissions=["can_read", "can_write"],
        )

    @get(
        "/related/{column_name:str}",
        guards=[require_permission("can_read", "Chart")],
    )
    async def related(
        self,
        column_name: str,
        dao: ChartDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
        rison_params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """GET /api/v1/chart/related/{column_name} — related values for dropdowns."""
        from liteset.db.filters import chart_access_filters

        base_filters = await chart_access_filters(security_manager, current_user)
        return await get_related_payload(
            dao=dao,
            column_name=column_name,
            rison_params=rison_params,
            allowed_fields=frozenset({"owners", "created_by", "changed_by"}),
            base_filters=base_filters or None,
        )

    @get(
        "/distinct/{column_name:str}",
        guards=[require_permission("can_read", "Chart")],
    )
    async def distinct(
        self,
        column_name: str,
        dao: ChartDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
        rison_params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """GET /api/v1/chart/distinct/{column_name} — distinct values for filters."""
        from liteset.db.filters import chart_access_filters

        base_filters = await chart_access_filters(security_manager, current_user)
        return await get_distinct_payload(
            dao=dao,
            column_name=column_name,
            rison_params=rison_params,
            base_filters=base_filters or None,
        )

    @get(
        "/{id_or_uuid:str}",
        guards=[require_permission("can_read", "Chart")],
    )
    async def get_chart(
        self,
        id_or_uuid: str,
        dao: ChartDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
    ) -> ChartGetResponse:
        chart = await dao.get_by_id_or_uuid(id_or_uuid)
        if not chart:
            raise ObjectNotFoundError("Chart", id_or_uuid)
        # Verify object-level access
        from liteset.db.filters import chart_access_filters

        base_filters = await chart_access_filters(security_manager, current_user)
        if base_filters:
            from sqlalchemy import select as sa_select

            model_cls = getattr(dao, "model_cls", None)
            if model_cls is not None:
                stmt = sa_select(model_cls.id).where(
                    model_cls.id == chart.id, *base_filters
                )
                result = await dao.session.scalar(stmt)
                if result is None:
                    raise ObjectNotFoundError("Chart", id_or_uuid)
        owners = getattr(chart, "owners", []) or []
        dashboards = getattr(chart, "dashboards", []) or []
        tags = getattr(chart, "tags", []) or []
        changed_by = getattr(chart, "changed_by", None)
        created_by = getattr(chart, "created_by", None)
        changed_on = getattr(chart, "changed_on", None)
        created_on = getattr(chart, "created_on", None)
        event_logger.log("chart.get", object_ref=f"chart:{id_or_uuid}")
        return ChartGetResponse(
            id=chart.id,
            result={
                "slice_name": chart.slice_name,
                "viz_type": chart.viz_type,
                "params": chart.params,
                "cache_timeout": chart.cache_timeout,
                "description": getattr(chart, "description", None),
                "datasource_id": getattr(chart, "datasource_id", None),
                "datasource_type": getattr(chart, "datasource_type", None),
                "query_context": getattr(chart, "query_context", None),
                "uuid": str(chart.uuid) if getattr(chart, "uuid", None) else None,
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
                "dashboards": [
                    {"id": d.id, "name": getattr(d, "dashboard_title", str(d))}
                    for d in dashboards
                ],
                "certified_by": getattr(chart, "certified_by", None),
                "thumbnail_url": (
                    f"/api/v1/chart/{chart.id}/thumbnail/{getattr(chart, 'digest', '')}/"
                    if getattr(chart, "digest", None)
                    else None
                ),
                "is_managed_externally": getattr(chart, "is_managed_externally", False),
                "tags": [
                    {"id": t.id, "name": getattr(t, "name", str(t))} for t in tags
                ],
                "datasource_name_text": getattr(chart, "datasource_name_text", None),
                "datasource_url": getattr(chart, "datasource_url", None),
                "datasource_uuid": (
                    str(getattr(chart, "datasource_uuid", None))
                    if getattr(chart, "datasource_uuid", None)
                    else None
                ),
            },
        )

    @post(
        "/",
        guards=[require_permission("can_write", "Chart")],
        status_code=201,
    )
    async def create(
        self,
        data: ChartPostBody,
        dao: ChartDAOProtocol,
        current_user: UserProtocol,
    ) -> ChartGetResponse:
        cmd = CreateChartCommand(
            dao=dao,
            data=filter_none(
                {
                    "slice_name": data.slice_name,
                    "viz_type": data.viz_type,
                    "datasource_id": data.datasource_id,
                    "datasource_type": data.datasource_type,
                    "params": data.params,
                    "query_context": data.query_context,
                    "cache_timeout": data.cache_timeout,
                    "description": data.description,
                }
            ),
            user_id=current_user.id,
        )
        chart = await cmd.execute()
        event_logger.log("chart.create", object_ref=f"chart:{chart.id}", user_id=current_user.id)
        # BL-M7: Return full response including all important fields
        return ChartGetResponse(
            id=chart.id,
            result={
                "slice_name": chart.slice_name,
                "viz_type": chart.viz_type,
                "uuid": str(chart.uuid) if getattr(chart, "uuid", None) else None,
                "datasource_id": getattr(chart, "datasource_id", None),
                "datasource_type": getattr(chart, "datasource_type", None),
                "params": getattr(chart, "params", None),
                "query_context": getattr(chart, "query_context", None),
                "cache_timeout": getattr(chart, "cache_timeout", None),
                "description": getattr(chart, "description", None),
                "certified_by": getattr(chart, "certified_by", None),
                "is_managed_externally": getattr(
                    chart, "is_managed_externally", False
                ),
                "owners": [
                    {"id": o.id, "name": str(o)}
                    for o in (getattr(chart, "owners", []) or [])
                ],
            },
        )

    @put(
        "/{pk:int}",
        guards=[require_permission("can_write", "Chart")],
    )
    async def update(
        self,
        pk: int,
        data: ChartPutBody,
        dao: ChartDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
    ) -> ChartGetResponse:
        update_data = filter_unset(
            {
                "slice_name": data.slice_name,
                "viz_type": data.viz_type,
                "datasource_id": data.datasource_id,
                "datasource_type": data.datasource_type,
                "params": data.params,
                "query_context": data.query_context,
                "cache_timeout": data.cache_timeout,
                "description": data.description,
            }
        )
        cmd = UpdateChartCommand(
            dao=dao,
            chart_id=pk,
            data=update_data,
            user_id=current_user.id,
            security_manager=security_manager,
        )
        chart = await cmd.execute()
        event_logger.log("chart.update", object_ref=f"chart:{pk}", user_id=current_user.id)
        owners = getattr(chart, "owners", []) or []
        dashboards = getattr(chart, "dashboards", []) or []
        tags = getattr(chart, "tags", []) or []
        changed_by = getattr(chart, "changed_by", None)
        created_by = getattr(chart, "created_by", None)
        changed_on = getattr(chart, "changed_on", None)
        created_on = getattr(chart, "created_on", None)
        return ChartGetResponse(
            id=chart.id,
            result={
                "slice_name": chart.slice_name,
                "viz_type": chart.viz_type,
                "params": chart.params,
                "cache_timeout": chart.cache_timeout,
                "description": getattr(chart, "description", None),
                "datasource_id": getattr(chart, "datasource_id", None),
                "datasource_type": getattr(chart, "datasource_type", None),
                "query_context": getattr(chart, "query_context", None),
                "uuid": str(chart.uuid) if getattr(chart, "uuid", None) else None,
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
                "dashboards": [
                    {"id": d.id, "name": getattr(d, "dashboard_title", str(d))}
                    for d in dashboards
                ],
                "certified_by": getattr(chart, "certified_by", None),
                "thumbnail_url": (
                    f"/api/v1/chart/{chart.id}/thumbnail/{getattr(chart, 'digest', '')}/"
                    if getattr(chart, "digest", None)
                    else None
                ),
                "is_managed_externally": getattr(chart, "is_managed_externally", False),
                "tags": [
                    {"id": t.id, "name": getattr(t, "name", str(t))} for t in tags
                ],
                "datasource_name_text": getattr(chart, "datasource_name_text", None),
                "datasource_url": getattr(chart, "datasource_url", None),
                "datasource_uuid": (
                    str(getattr(chart, "datasource_uuid", None))
                    if getattr(chart, "datasource_uuid", None)
                    else None
                ),
            },
        )

    @delete(
        "/{pk:int}",
        guards=[require_permission("can_write", "Chart")],
        status_code=200,
    )
    async def delete_chart(
        self,
        pk: int,
        dao: ChartDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
    ) -> dict[str, str]:
        cmd = DeleteChartCommand(
            dao=dao,
            chart_id=pk,
            security_manager=security_manager,
            user_id=current_user.id,
        )
        await cmd.execute()
        event_logger.log("chart.delete", object_ref=f"chart:{pk}", user_id=current_user.id)
        return {"message": "OK"}

    @delete(
        "/",
        guards=[require_permission("can_write", "Chart")],
        status_code=200,
    )
    async def bulk_delete(
        self,
        dao: ChartDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
        rison_params: dict[str, Any] | None,
    ) -> dict[str, str]:
        ids = extract_ids_required(rison_params)
        cmd = BulkDeleteChartsCommand(
            dao=dao,
            chart_ids=ids,
            security_manager=security_manager,
            user_id=current_user.id,
        )
        await cmd.execute()
        event_logger.log("chart.bulk_delete", user_id=current_user.id, extra={"count": len(ids)})
        return {"message": "OK"}

    @get(
        "/{pk:int}/cache_screenshot/",
        guards=[require_permission("can_read", "Chart")],
    )
    async def cache_screenshot(
        self, pk: int, dao: ChartDAOProtocol, state: State
    ) -> ChartCacheScreenshotResponse | Response[Any]:
        # BL-C1: Gate on THUMBNAILS feature flag
        feature_flags = getattr(state.settings, "feature_flags", {})
        if not feature_flags.get("THUMBNAILS", False):
            return Response(content={"message": "Not found"}, status_code=404)
        chart = await dao.find_by_id(pk)
        if not chart:
            raise ObjectNotFoundError("Chart", pk)
        # Trigger Celery screenshot task (actual dispatch happens in thumbnails module)
        cache_key = f"chart_{pk}_screenshot"
        return ChartCacheScreenshotResponse(
            cache_key=cache_key,
            chart_url=f"/explore/?slice_id={pk}",
            image_url=f"/api/v1/chart/{pk}/screenshot/{cache_key}/",
        )

    @get(
        "/{pk:int}/screenshot/{digest:str}/",
        guards=[require_permission("can_read", "Chart")],
        media_type="image/png",
    )
    async def screenshot(
        self, pk: int, digest: str, dao: ChartDAOProtocol, state: State
    ) -> Response[bytes]:
        # BL-C1: Gate on THUMBNAILS feature flag
        feature_flags = getattr(state.settings, "feature_flags", {})
        if not feature_flags.get("THUMBNAILS", False):
            return Response(content=b"", status_code=404, media_type="image/png")
        chart = await dao.find_by_id(pk)
        if not chart:
            raise ObjectNotFoundError("Chart", pk)
        # Return placeholder — actual screenshot retrieval from cache
        return Response(
            content=b"",
            status_code=202,
            media_type="image/png",
        )

    @get(
        "/{pk:int}/thumbnail/{digest:str}/",
        guards=[require_permission("can_read", "Chart")],
        media_type="image/png",
    )
    async def thumbnail(
        self, pk: int, digest: str, dao: ChartDAOProtocol, state: State
    ) -> Response[bytes]:
        # BL-C1: Gate on THUMBNAILS feature flag
        feature_flags = getattr(state.settings, "feature_flags", {})
        if not feature_flags.get("THUMBNAILS", False):
            return Response(content=b"", status_code=404, media_type="image/png")
        chart = await dao.find_by_id(pk)
        if not chart:
            raise ObjectNotFoundError("Chart", pk)
        return Response(
            content=b"",
            status_code=202,
            media_type="image/png",
        )

    @get(
        "/export/",
        guards=[require_permission("can_read", "Chart")],
        media_type="application/zip",
    )
    async def export(
        self, dao: ChartDAOProtocol, rison_params: dict[str, Any] | None
    ) -> Stream:
        ids = extract_ids(rison_params)
        if not ids:
            raise CommandInvalidError("At least one ID is required for export")
        cmd = ExportChartsCommand(model_ids=ids, dao=dao)
        buf = await cmd.execute()
        event_logger.log("chart.export", extra={"count": len(ids)})
        return Stream(
            stream_zip(buf),
            status_code=200,
            media_type="application/zip",
            headers={"Content-Disposition": "attachment; filename=charts_export.zip"},
        )

    @get(
        "/favorite_status/",
        guards=[require_permission("can_read", "Chart")],
    )
    async def favorite_status(
        self,
        dao: ChartDAOProtocol,
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

    @post(
        "/{pk:int}/favorites/",
        guards=[require_permission("can_read", "Chart")],
        status_code=200,
    )
    async def add_favorite(
        self, pk: int, dao: ChartDAOProtocol, current_user: UserProtocol
    ) -> dict[str, str]:
        chart = await dao.find_by_id(pk)
        if not chart:
            raise ObjectNotFoundError("Chart", pk)
        await dao.add_favorite(pk, current_user.id)
        event_logger.log("chart.add_favorite", object_ref=f"chart:{pk}", user_id=current_user.id)
        return {"result": "OK"}

    @delete(
        "/{pk:int}/favorites/",
        guards=[require_permission("can_read", "Chart")],
        status_code=200,
    )
    async def remove_favorite(
        self, pk: int, dao: ChartDAOProtocol, current_user: UserProtocol
    ) -> dict[str, str]:
        chart = await dao.find_by_id(pk)
        if not chart:
            raise ObjectNotFoundError("Chart", pk)
        await dao.remove_favorite(pk, current_user.id)
        event_logger.log("chart.remove_favorite", object_ref=f"chart:{pk}", user_id=current_user.id)
        return {"result": "OK"}

    @put(
        "/warm_up_cache",
        guards=[require_permission("can_write", "Chart")],
    )
    async def warm_up_cache(
        self, data: ChartCacheWarmUpRequest, dao: ChartDAOProtocol
    ) -> dict[str, Any]:
        cmd = WarmUpChartCacheCommand(
            dao=dao,
            chart_id=data.chart_id,
            dashboard_id=data.dashboard_id,
        )
        result = await cmd.execute()
        event_logger.log("chart.warm_up_cache", object_ref=f"chart:{data.chart_id}")
        return {"result": result}

    @post(
        "/import/",
        guards=[require_permission("can_write", "Chart")],
        media_type="application/json",
    )
    async def import_chart(
        self,
        dao: ChartDAOProtocol,
        data: UploadFile = Body(media_type=RequestEncodingType.MULTI_PART),  # noqa: B008
        overwrite: bool = False,
        passwords: str | None = None,
        ssh_tunnel_passwords: str | None = None,
    ) -> dict[str, str]:
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
        cmd = ImportChartsCommand(
            contents=buf,
            dao=dao,
            overwrite=overwrite,
            passwords=passwords_dict,
            ssh_tunnel_passwords=ssh_dict,
        )
        await cmd.execute()
        event_logger.log("chart.import")
        return {"message": "OK"}

    # ------------------------------------------------------------------
    # Visualization types
    # ------------------------------------------------------------------

    @get(
        "/viz_types",
        guards=[require_permission("can_read", "Chart")],
    )
    async def viz_types(self) -> dict[str, Any]:
        """GET /api/v1/chart/viz_types — list available visualization types.

        Returns the set of viz type identifiers known to the platform.
        In Superset this is derived from ``superset.viz.viz_types``; liteset
        exposes the same list as a static registry so the frontend can
        populate the chart-type picker without importing legacy viz code.
        """
        # BL-H2: Static registry of common Superset viz types.
        # This mirrors the keys produced by ``superset.viz.viz_types`` at the
        # time of the liteset migration.  When the legacy viz module is
        # fully retired, this list becomes the single source of truth.
        known_viz_types: list[str] = [
            "area",
            "bar",
            "big_number",
            "big_number_total",
            "box_plot",
            "bubble",
            "bubble_v2",
            "cal_heatmap",
            "chord",
            "compare",
            "country_map",
            "deck_arc",
            "deck_geojson",
            "deck_grid",
            "deck_hex",
            "deck_multi",
            "deck_path",
            "deck_polygon",
            "deck_scatter",
            "deck_screengrid",
            "dist_bar",
            "dual_line",
            "echarts_area",
            "echarts_timeseries_bar",
            "echarts_timeseries_line",
            "echarts_timeseries_scatter",
            "echarts_timeseries_smooth",
            "echarts_timeseries_step",
            "event_flow",
            "filter_box",
            "funnel",
            "gauge_chart",
            "graph_chart",
            "heatmap",
            "histogram",
            "horizon",
            "line",
            "line_multi",
            "mapbox",
            "markup",
            "mixed_timeseries",
            "paired_ttest",
            "para",
            "partition",
            "pie",
            "pivot_table",
            "pivot_table_v2",
            "radar",
            "rose",
            "sankey",
            "separator",
            "sunburst",
            "sunburst_v2",
            "table",
            "time_pivot",
            "time_table",
            "treemap",
            "treemap_v2",
            "word_cloud",
            "world_map",
        ]
        return {
            "result": [
                {"viz_type": vt, "label": vt.replace("_", " ").title()}
                for vt in known_viz_types
            ],
            "count": len(known_viz_types),
        }

    # ------------------------------------------------------------------
    # Chart Data endpoints (merged from ChartDataController)
    # ------------------------------------------------------------------

    @get(
        "/{pk:int}/data/",
        guards=[require_permission("can_read", "Chart")],
    )
    async def get_chart_data(
        self,
        pk: int,
        dao: ChartDAOProtocol,
        ds_dao: DatasourceDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
        state: State,
        format: str | None = None,
        type: str | None = None,
        force: str | None = None,
    ) -> dict[str, Any] | Response[Any]:
        """GET /api/v1/chart/{pk}/data/ — fetch data for a specific chart."""
        from liteset.exceptions import LitesetValidationException

        # BL-C2: Check GLOBAL_ASYNC_QUERIES feature flag
        settings = getattr(state, "settings", None)
        if getattr(settings, "global_async_queries", False):
            result_format = (format or "json").lower()
            result_type = (type or "full").lower()
            if result_format == "json" and result_type == "full":
                # TODO: Implement full async query dispatch via Celery +
                # CreateAsyncChartDataJobCommand (see superset/charts/data/api.py
                # _run_async for reference).
                return Response(
                    content={
                        "message": "Async queries not yet implemented in liteset"
                    },
                    status_code=202,
                )

        chart = await dao.find_by_id(pk)
        if not chart:
            raise ObjectNotFoundError("Chart", pk)

        query_context_str = getattr(chart, "query_context", None)
        if not query_context_str:
            raise LitesetValidationException("Chart has no query context saved")

        try:
            qc_data = _json.loads(query_context_str)
        except (ValueError, TypeError):
            raise LitesetValidationException("Chart has invalid query context")

        # Apply query param overrides
        if format is not None:
            qc_data["result_format"] = format
        if type is not None:
            qc_data["result_type"] = type
        if force is not None:
            qc_data["force"] = force.lower() in ("true", "1", "yes")

        ds_ref = qc_data.get("datasource", {})
        datasource = await ds_dao.get_datasource(
            ds_ref.get("type", "table"),
            ds_ref.get("id", 0),
        )
        if not datasource:
            raise ObjectNotFoundError("Datasource", ds_ref.get("id", 0))

        settings = getattr(state, "settings", None)
        queries_data = qc_data.get("queries", [])
        ds_dict = {"type": ds_ref.get("type", "table"), "id": ds_ref.get("id", 0)}
        query_objects = [
            AsyncQueryObject.from_request(q, ds_dict) for q in queries_data
        ]
        query_context = AsyncQueryContext(
            datasource=datasource,
            queries=query_objects,
            force=qc_data.get("force", False),
        )
        processor = AsyncQueryContextProcessor(
            datasource=datasource,
            settings=settings,
            security_manager=security_manager,
            user=current_user,
            query_context=query_context,
        )
        cmd = ChartDataCommand(query_context=query_context, processor=processor)
        result = await cmd.execute()

        # Strip raw SQL from response for guest users (C3)
        if security_manager.is_guest_user(current_user):
            for q in result.get("queries", []):
                if isinstance(q, dict):
                    q.pop("query", None)

        event_logger.log("chart.data", object_ref=f"chart:{pk}", user_id=current_user.id)
        return result

    @post(
        "/data",
        guards=[require_permission("can_read", "Chart")],
    )
    async def data(
        self,
        request: Request[Any, Any, Any],
        ds_dao: DatasourceDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
        state: State,
        data: ChartDataQueryContext | None = None,
    ) -> Response[Any]:
        """POST /api/v1/chart/data — execute ad-hoc chart data query.

        Accepts either a JSON body or a ``form_data`` multipart field
        containing a JSON string (used by CSV export in Superset).
        """
        import msgspec as _msgspec

        # BL-H5: Fallback to form_data multipart field when JSON body is absent
        if data is None:
            form_data_str: str | None = None
            content_type = request.content_type or ()
            if "multipart" in content_type or "form" in content_type:
                form = await request.form()
                form_data_str = form.get("form_data")  # type: ignore[assignment]
            if form_data_str is None:
                # Also try raw body as JSON
                body = await request.body()
                if body:
                    form_data_str = body.decode("utf-8")
            if not form_data_str:
                return Response(
                    content={"message": "Request is not JSON"},
                    status_code=400,
                )
            try:
                data = _msgspec.json.decode(
                    form_data_str
                    if isinstance(form_data_str, bytes)
                    else form_data_str.encode(),
                    type=ChartDataQueryContext,
                )
            except (_msgspec.ValidationError, _msgspec.DecodeError) as exc:
                return Response(
                    content={"message": f"Invalid form_data JSON: {exc}"},
                    status_code=400,
                )

        settings = getattr(state, "settings", None)

        # BL-C2: Check GLOBAL_ASYNC_QUERIES feature flag
        if getattr(settings, "global_async_queries", False):
            result_format = (getattr(data, "result_format", None) or "json").lower()
            result_type = (getattr(data, "result_type", None) or "full").lower()
            if result_format == "json" and result_type == "full":
                # TODO: Implement full async query dispatch via Celery +
                # CreateAsyncChartDataJobCommand (see superset/charts/data/api.py
                # _run_async for reference).
                return Response(
                    content={
                        "message": "Async queries not yet implemented in liteset"
                    },
                    status_code=202,
                )

        datasource = await ds_dao.get_datasource(
            data.datasource.type, data.datasource.id
        )
        if not datasource:
            raise ObjectNotFoundError("Datasource", data.datasource.id)

        ds_ref = {"type": data.datasource.type, "id": data.datasource.id}
        query_objects = [AsyncQueryObject.from_request(q, ds_ref) for q in data.queries]
        query_context = AsyncQueryContext(
            datasource=datasource,
            queries=query_objects,
            force=data.force,
            result_format=getattr(data, "result_format", None),
        )
        processor = AsyncQueryContextProcessor(
            datasource=datasource,
            settings=settings,
            security_manager=security_manager,
            user=current_user,
            query_context=query_context,
        )
        cmd = ChartDataCommand(query_context=query_context, processor=processor)
        result = await cmd.execute()

        result_format = getattr(data, "result_format", None) or "json"
        if result_format in ("csv", "xlsx"):
            # Check can_csv permission
            if not await security_manager.can_access(
                "can_csv", "Superset", user=current_user
            ):
                from liteset.exceptions import LitesetSecurityException

                raise LitesetSecurityException(
                    message="You don't have permission to download data"
                )

            import zipfile

            import pandas as pd

            # Extract data from queries result
            queries = result.get("queries", [])
            frames: list[pd.DataFrame] = []
            for q in queries:
                if isinstance(q, dict):
                    if isinstance(q.get("df"), pd.DataFrame):
                        frames.append(q["df"])
                    elif q.get("data"):
                        frames.append(pd.DataFrame(q["data"]))

            if len(frames) <= 1:
                # Single query (or no data): return file directly
                df = frames[0] if frames else pd.DataFrame()
                if result_format == "csv":
                    csv_content = AsyncQueryContextProcessor.get_data(df, "csv")
                    return Response(
                        content=csv_content,
                        media_type="text/csv",
                        headers={
                            "Content-Disposition": "attachment; filename=data.csv"
                        },
                    )
                xlsx_data = AsyncQueryContextProcessor.get_data(df, "xlsx")
                return Response(
                    content=xlsx_data,
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={
                        "Content-Disposition": 'attachment; filename="data.xlsx"'
                    },
                )

            # Multiple queries: bundle individual files into a ZIP
            ext = "csv" if result_format == "csv" else "xlsx"
            zip_buf = io.BytesIO()
            with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for idx, df in enumerate(frames, start=1):
                    file_data = AsyncQueryContextProcessor.get_data(df, result_format)
                    file_bytes = (
                        file_data.encode("utf-8")
                        if isinstance(file_data, str)
                        else file_data
                    )
                    zf.writestr(f"query_{idx}.{ext}", file_bytes)
            zip_buf.seek(0)
            return Response(
                content=zip_buf.getvalue(),
                media_type="application/zip",
                headers={
                    "Content-Disposition": "attachment; filename=chart_data.zip"
                },
            )
        # Strip raw SQL from response for guest users (C3)
        if security_manager.is_guest_user(current_user):
            for q in result.get("queries", []):
                if isinstance(q, dict):
                    q.pop("query", None)

        event_logger.log("chart.data_post")
        return Response(content=result, media_type="application/json")

    @get(
        "/data/{cache_key:str}",
        guards=[require_permission("can_read", "Chart")],
    )
    async def get_cached_data(
        self,
        cache_key: str,
        request: Request[Any, Any, Any],
        security_manager: SecurityManagerProtocol,
        ds_dao: DatasourceDAOProtocol,
        state: State,
        current_user: UserProtocol,
    ) -> dict[str, Any]:
        """GET /api/v1/chart/data/{cache_key} — retrieve cached chart data."""
        cache_manager = getattr(request.app.state, "cache_manager", None)
        settings = getattr(state, "settings", None)
        cmd = GetCachedChartDataCommand(
            cache_key=cache_key,
            cache_manager=cache_manager,
            security_manager=security_manager,
            datasource_dao=ds_dao,
            settings=settings,
            user=current_user,
        )
        result = await cmd.execute()
        event_logger.log("chart.cached_data", object_ref=f"cache:{cache_key}")
        if result is None:
            return {"result": [], "message": "Cache miss"}
        return result
