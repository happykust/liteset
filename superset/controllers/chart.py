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
import math
import uuid
from typing import Any, cast, TYPE_CHECKING

from litestar import Controller, delete, get, post, put
from litestar.connection import Request
from litestar.datastructures import State, UploadFile
from litestar.di import Provide
from litestar.enums import RequestEncodingType
from litestar.params import Body, Parameter
from litestar.response import Response, Stream

from superset.commands.chart import (
    BulkDeleteChartsCommand,
    CreateChartCommand,
    DeleteChartCommand,
    ExportChartsCommand,
    ImportChartsCommand,
    UpdateChartCommand,
    WarmUpChartCacheCommand,
)
from superset.commands.chart_data import ChartDataCommand, GetCachedChartDataCommand
from superset.common.query_context import AsyncQueryContext
from superset.common.query_context_processor import AsyncQueryContextProcessor
from superset.common.query_object import AsyncQueryObject
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
from superset.providers import provide_chart_dao, provide_datasource_dao
from superset.schemas.base import FavoriteStatusItem, FavoriteStatusResponse
from superset.schemas.chart import (
    ChartCacheScreenshotResponse,
    ChartCacheWarmUpRequest,
    ChartDataQueryContext,
    ChartDetailResult,
    ChartGetResponse,
    ChartPostSchema,
    ChartPutSchema,
)
from superset.typing import (
    ChartDAOProtocol,
    DatasourceDAOProtocol,
    SecurityManagerProtocol,
    UserProtocol,
)
from superset.utils import filter_none, filter_unset

if TYPE_CHECKING:
    from superset.config import SupersetSettings
    from superset.db.daos.chart import AsyncChartDAO


# ---------------------------------------------------------------------------
# Custom RISON filters for charts
# ---------------------------------------------------------------------------
def _chart_custom_filters(current_user: Any) -> dict[str, Any]:
    """Return custom filter callables keyed by RISON ``opr`` name.

    Each callable has signature ``(model_cls, value) -> clause | None``.
    """

    def _chart_is_favorite(model_cls: Any, value: Any) -> Any:
        """Filter charts that the current user has favorited."""
        from sqlalchemy import select as sa_select

        from superset.models.core import FavStar

        user_id = getattr(current_user, "id", None)
        if user_id is None:
            return None
        fav_subq = sa_select(FavStar.obj_id).where(
            FavStar.class_name == "slice",
            FavStar.user_id == user_id,
        )
        if value:
            return model_cls.id.in_(fav_subq)
        return ~model_cls.id.in_(fav_subq)

    def _chart_is_certified(model_cls: Any, value: Any) -> Any:
        """Filter charts by certified status."""
        if value:
            return model_cls.certified_by.isnot(None)
        return model_cls.certified_by.is_(None)

    return {
        "chart_is_favorite": _chart_is_favorite,
        "chart_is_certified": _chart_is_certified,
    }


# ---------------------------------------------------------------------------
# Markdown helper — converts description to sanitised HTML
# ---------------------------------------------------------------------------
_SAFE_MD_TAGS = {
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "b",
    "i",
    "strong",
    "em",
    "tt",
    "p",
    "br",
    "span",
    "div",
    "blockquote",
    "code",
    "hr",
    "ul",
    "ol",
    "li",
    "dd",
    "dt",
    "img",
    "a",
}
_SAFE_MD_ATTRS: dict[str, set[str]] = {
    "img": {"src", "alt", "title"},
    "a": {"href", "alt", "title"},
}


def _md_to_html(raw: str) -> str:
    """Convert a markdown string to sanitised HTML, matching the original
    ``superset.utils.core.markdown`` behaviour."""
    import markdown as md  # type: ignore[import-untyped]
    import nh3

    html = md.markdown(
        raw,
        extensions=[
            "markdown.extensions.tables",
            "markdown.extensions.fenced_code",
            "markdown.extensions.codehilite",
        ],
    )
    return nh3.clean(html, tags=_SAFE_MD_TAGS, attributes=_SAFE_MD_ATTRS)


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
        from sqlalchemy.orm import selectinload

        from superset.db.filters import chart_access_filters
        from superset.models.slice import Slice

        rison_filters, order_by, page, page_size = build_rison_query_params(
            Slice,
            rison_params,
            custom_filters=_chart_custom_filters(current_user),
        )
        base_filters = await chart_access_filters(security_manager, current_user)
        all_filters = (base_filters or []) + rison_filters

        charts = await dao.find_all(
            filters=all_filters or None,
            page=page,
            page_size=page_size,
            order_by=order_by,
            options=[
                selectinload(Slice.owners),
                selectinload(Slice.changed_by),
                selectinload(Slice.created_by),
                selectinload(Slice.last_saved_by),
                selectinload(Slice.table),
                selectinload(Slice.dashboards),  # type: ignore[attr-defined]
                selectinload(Slice.tags),
            ],
        )
        total = await dao.count(filters=all_filters or None)
        event_logger.log("chart.list")
        payload = serialize_list_response(
            charts,
            total,
            [
                "id",
                "uuid",
                "slice_name",
                "viz_type",
                "params",
                "cache_timeout",
                "description",
                "certified_by",
                "certification_details",
                "is_managed_externally",
                "datasource_id",
                "datasource_type",
                "changed_by.first_name",
                "changed_by.last_name",
                "changed_by.id",
                "created_by.first_name",
                "created_by.id",
                "created_by.last_name",
                "last_saved_at",
                "last_saved_by.id",
                "last_saved_by.first_name",
                "last_saved_by.last_name",
                "owners.id",
                "owners.first_name",
                "owners.last_name",
                "dashboards.id",
                "dashboards.dashboard_title",
                "tags.id",
                "tags.name",
                "tags.type",
            ],
            list_title="List Slice",
            order_columns=[
                "changed_by.first_name",
                "changed_on_delta_humanized",
                "datasource_id",
                "datasource_name",
                "last_saved_at",
                "last_saved_by.id",
                "last_saved_by.first_name",
                "last_saved_by.last_name",
                "slice_name",
                "viz_type",
            ],
        )
        for item in payload["result"]:
            # Computed properties from the Slice model
            chart_id = item["id"]
            item["changed_by_name"] = ""
            changed_by = item.get("changed_by")
            if changed_by and isinstance(changed_by, dict):
                fn = changed_by.get("first_name", "") or ""
                ln = changed_by.get("last_name", "") or ""
                item["changed_by_name"] = f"{fn} {ln}".strip()

            # created_by_name — same pattern as changed_by_name
            created_by = item.get("created_by")
            if created_by and isinstance(created_by, dict):
                cb_fn = created_by.get("first_name", "") or ""
                cb_ln = created_by.get("last_name", "") or ""
                item["created_by_name"] = f"{cb_fn} {cb_ln}".strip()
            else:
                item["created_by_name"] = ""

            # Find the matching ORM object for computed properties
            chart_obj = next((c for c in charts if c.id == chart_id), None)
            if chart_obj:
                item["changed_on_utc"] = chart_obj.changed_on_utc
                item["changed_on_delta_humanized"] = (
                    chart_obj.changed_on_delta_humanized
                )
                item["created_on_delta_humanized"] = (
                    chart_obj.created_on_delta_humanized
                )
                item["datasource_name_text"] = chart_obj.datasource_name_text
                item["datasource_url"] = chart_obj.datasource_url
                item["thumbnail_url"] = chart_obj.thumbnail_url
                item["url"] = chart_obj.url
                item["edit_url"] = chart_obj.edit_url
                item["slice_url"] = chart_obj.slice_url

                # changed_on_dttm — epoch float of changed_on
                item["changed_on_dttm"] = (
                    float(chart_obj.changed_on.timestamp())
                    if chart_obj.changed_on
                    else None
                )

                # description_markeddown — HTML from markdown description
                desc = chart_obj.description or ""
                if desc:
                    item["description_markeddown"] = _md_to_html(desc)
                else:
                    item["description_markeddown"] = ""

                # form_data — dict from params JSON + slice_id/viz_type/datasource
                try:
                    fd: dict[str, Any] = _json.loads(chart_obj.params or "{}")
                except Exception:
                    fd = {}
                fd.update(
                    {
                        "slice_id": chart_obj.id,
                        "viz_type": chart_obj.viz_type,
                        "datasource": (
                            f"{chart_obj.datasource_id}__{chart_obj.datasource_type}"
                        ),
                    }
                )
                if chart_obj.cache_timeout:
                    fd["cache_timeout"] = chart_obj.cache_timeout
                item["form_data"] = fd

                # table.default_endpoint and table.table_name
                tbl = chart_obj.table
                item["table.default_endpoint"] = tbl.default_endpoint if tbl else None
                item["table.table_name"] = tbl.table_name if tbl else None

        return payload

    @get(
        "/_info",
        guards=[require_permission("can_read", "Chart")],
    )
    async def info(self, dao: ChartDAOProtocol) -> dict[str, Any]:
        """GET /api/v1/chart/_info — API metadata for frontend."""
        return await get_info_payload(
            dao=dao,
            model_name="Chart",
            permissions=["can_warm_up_cache", "can_read", "can_write", "can_export"],
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
        from superset.db.filters import chart_access_filters

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
        from superset.db.filters import chart_access_filters

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
        from sqlalchemy.orm import selectinload

        from superset.db.filters import chart_access_filters
        from superset.models.slice import Slice

        # Build id/uuid filter
        id_filter: list[Any] = []
        try:
            chart_id = int(id_or_uuid)
            id_filter = [Slice.id == chart_id]
        except (ValueError, TypeError):
            try:
                uuid_val = uuid.UUID(str(id_or_uuid))
                id_filter = [Slice.uuid == uuid_val]
            except ValueError as exc:
                raise ObjectNotFoundError("Chart", id_or_uuid) from exc

        base_filters = await chart_access_filters(security_manager, current_user)
        all_filters = id_filter + (base_filters or [])

        results = await dao.find_all(
            filters=all_filters,
            page=0,
            page_size=1,
            options=[
                selectinload(Slice.owners),
                selectinload(Slice.changed_by),
                selectinload(Slice.created_by),
                selectinload(Slice.last_saved_by),
                selectinload(Slice.table),
                selectinload(Slice.dashboards),  # type: ignore[attr-defined]
                selectinload(Slice.tags),
            ],
        )
        if not results:
            raise ObjectNotFoundError("Chart", id_or_uuid)
        chart = results[0]
        event_logger.log("chart.get", object_ref=f"chart:{id_or_uuid}")
        return ChartGetResponse(
            id=chart.id,
            result=ChartDetailResult.from_model(chart),
        )

    @post(
        "/",
        guards=[require_permission("can_write", "Chart")],
        status_code=201,
    )
    async def create(
        self,
        data: ChartPostSchema,
        dao: ChartDAOProtocol,
        current_user: UserProtocol,
        security_manager: SecurityManagerProtocol,
    ) -> ChartGetResponse:
        cmd = CreateChartCommand(
            dao=cast("AsyncChartDAO", dao),
            data=filter_none(
                {
                    "slice_name": data.slice_name,
                    "viz_type": data.viz_type,
                    "datasource_id": data.datasource_id,
                    "datasource_type": data.datasource_type,
                    "params": data.params,
                    "query_context": data.query_context,
                    "query_context_generation": data.query_context_generation,
                    "cache_timeout": data.cache_timeout,
                    "description": data.description,
                    "certified_by": data.certified_by,
                    "certification_details": data.certification_details,
                    "is_managed_externally": data.is_managed_externally,
                    "external_url": data.external_url,
                    "tags": data.tags,
                    "owners": data.owners,
                    "dashboards": data.dashboards,
                    "datasource_name": data.datasource_name,
                    "uuid": data.uuid,
                }
            ),
            user_id=current_user.id,
            security_manager=security_manager,
        )
        chart = await cmd.execute()
        chart_id = int(chart.id)
        event_logger.log(
            "chart.create",
            object_ref=f"chart:{chart_id}",
            user_id=current_user.id,
        )
        return ChartGetResponse(
            id=chart_id,
            result=ChartDetailResult.from_model(chart),
        )

    @put(
        "/{pk:int}",
        guards=[require_permission("can_write", "Chart")],
    )
    async def update(
        self,
        pk: int,
        data: ChartPutSchema,
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
                "query_context_generation": data.query_context_generation,
                "cache_timeout": data.cache_timeout,
                "description": data.description,
                "certified_by": data.certified_by,
                "certification_details": data.certification_details,
                "is_managed_externally": data.is_managed_externally,
                "external_url": data.external_url,
                "tags": data.tags,
                "owners": data.owners,
                "dashboards": data.dashboards,
            }
        )
        cmd = UpdateChartCommand(
            dao=cast("AsyncChartDAO", dao),
            chart_id=pk,
            data=update_data,
            user_id=current_user.id,
            security_manager=security_manager,
        )
        chart = await cmd.execute()
        event_logger.log(
            "chart.update",
            object_ref=f"chart:{pk}",
            user_id=current_user.id,
        )
        return ChartGetResponse(
            id=int(chart.id),
            result=ChartDetailResult.from_model(chart),
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
            dao=cast("AsyncChartDAO", dao),
            chart_id=pk,
            security_manager=security_manager,
            user_id=current_user.id,
        )
        await cmd.execute()
        event_logger.log(
            "chart.delete",
            object_ref=f"chart:{pk}",
            user_id=current_user.id,
        )
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
        rison_params: list[int] | dict[str, Any] | None,
    ) -> dict[str, str]:
        ids = extract_ids_required(rison_params)
        cmd = BulkDeleteChartsCommand(
            dao=cast("AsyncChartDAO", dao),
            chart_ids=ids,
            security_manager=security_manager,
            user_id=current_user.id,
        )
        await cmd.execute()
        event_logger.log(
            "chart.bulk_delete",
            user_id=current_user.id,
            extra={"count": len(ids)},
        )
        num = len(ids)
        msg = f"Deleted {num} chart" if num == 1 else f"Deleted {num} charts"
        return {"message": msg}

    @get(
        "/{pk:int}/cache_screenshot/",
        guards=[require_permission("can_read", "Chart")],
    )
    async def cache_screenshot(
        self,
        pk: int,
        dao: ChartDAOProtocol,
        state: State,
        rison_params: dict[str, Any] | None,
    ) -> ChartCacheScreenshotResponse | Response[Any]:
        # BL-C1: Gate on THUMBNAILS feature flag
        feature_flags = getattr(state.settings, "feature_flags", {})
        if not feature_flags.get("THUMBNAILS", False):
            return Response(content={"message": "Not found"}, status_code=404)
        chart = await dao.find_by_id(pk)
        if not chart:
            raise ObjectNotFoundError("Chart", pk)
        # Extract optional rison query params (mirrors screenshot_query_schema)
        rison_dict: dict[str, Any] = rison_params or {}
        _force: bool = bool(rison_dict.get("force", False))  # noqa: F841
        _window_size: tuple[int, int] | None = rison_dict.get("window_size")  # noqa: F841
        _thumb_size: tuple[int, int] | None = rison_dict.get("thumb_size")  # noqa: F841
        # Trigger Celery screenshot task (actual dispatch happens in thumbnails module)
        cache_key = f"chart_{pk}_screenshot"
        return ChartCacheScreenshotResponse(
            cache_key=cache_key,
            chart_url=f"/explore/?slice_id={pk}",
            image_url=f"/api/v1/chart/{pk}/screenshot/{cache_key}/",
            task_status="not_available",
            task_updated_at=None,
        )

    @get(
        "/{pk:int}/screenshot/{digest:str}/",
        guards=[require_permission("can_read", "Chart")],
        media_type="image/png",
    )
    async def screenshot(
        self, pk: int, digest: str, dao: ChartDAOProtocol, state: State
    ) -> Response[bytes]:
        """Get a computed screenshot from cache.

        The *digest* path parameter is the cache key written by the Celery
        screenshot task.  If the cache contains an image we serve it;
        otherwise we return 404.
        """
        import asyncio

        from superset.utils.screenshots import (
            ChartScreenshot,
            ScreenshotImageNotAvailableException,
            StatusValues,
        )

        feature_flags = getattr(state.settings, "feature_flags", {})
        if not feature_flags.get("THUMBNAILS", False):
            return Response(content=b"", status_code=404, media_type="image/png")

        chart = await dao.find_by_id(pk)
        if not chart:
            raise ObjectNotFoundError("Chart", pk)

        cache_payload = await asyncio.to_thread(
            ChartScreenshot.get_from_cache_key, digest
        )
        if cache_payload and cache_payload.status == StatusValues.UPDATED:
            try:
                image = cache_payload.get_image()
            except ScreenshotImageNotAvailableException:
                return Response(content=b"", status_code=404, media_type="image/png")
            return Response(
                content=image.getvalue(),
                status_code=200,
                media_type="image/png",
            )
        return Response(content=b"", status_code=404, media_type="image/png")

    @get(
        "/{pk:int}/thumbnail/{digest:str}/",
        guards=[require_permission("can_read", "Chart")],
        media_type="image/png",
    )
    async def thumbnail(
        self,
        pk: int,
        digest: str,
        dao: ChartDAOProtocol,
        state: State,
        current_user: UserProtocol,
    ) -> Response[bytes]:
        """Compute or get already computed chart thumbnail from cache.

        If the chart's current digest differs from *digest* we redirect to
        the canonical URL.  Otherwise we check the thumbnail cache: if the
        image exists we serve it directly; if not we queue a Celery task and
        return 202.
        """
        import asyncio

        from litestar.response import Redirect

        from superset.utils.screenshots import (
            ChartScreenshot,
            ScreenshotCachePayload,
            ScreenshotImageNotAvailableException,
        )

        feature_flags = getattr(state.settings, "feature_flags", {})
        if not feature_flags.get("THUMBNAILS", False):
            return Response(content=b"", status_code=404, media_type="image/png")

        chart = await dao.find_by_id(pk)
        if not chart:
            raise ObjectNotFoundError("Chart", pk)

        # Redirect to the canonical digest URL if stale
        chart_digest = getattr(chart, "digest", None)
        if chart_digest and chart_digest != digest:
            return Redirect(
                path=f"/api/v1/chart/{pk}/thumbnail/{chart_digest}/",
            )

        # Build screenshot object and compute cache key
        chart_url = f"/explore/?slice_id={pk}"
        screenshot_obj = ChartScreenshot(chart_url, chart_digest or digest)
        cache_key = await asyncio.to_thread(screenshot_obj.get_cache_key)
        cache_payload = (
            await asyncio.to_thread(ChartScreenshot.get_from_cache_key, cache_key)
            or ScreenshotCachePayload()
        )

        if cache_payload.should_trigger_task():
            # Mark as pending in cache and dispatch Celery task
            await asyncio.to_thread(
                screenshot_obj.cache.set,
                cache_key,
                ScreenshotCachePayload().to_dict(),
            )
            from superset.tasks.thumbnails import cache_chart_thumbnail

            cache_chart_thumbnail.delay(
                current_user=getattr(current_user, "username", None),
                chart_id=str(chart.id),
                force=False,
            )
            return Response(
                content=b"",
                status_code=202,
                media_type="image/png",
            )

        # Serve from cache
        try:
            image = cache_payload.get_image()
        except ScreenshotImageNotAvailableException:
            return Response(content=b"", status_code=404, media_type="image/png")
        return Response(
            content=image.getvalue(),
            status_code=200,
            media_type="image/png",
        )

    @get(
        "/export/",
        guards=[require_permission("can_read", "Chart")],
        media_type="application/zip",
    )
    async def export(
        self,
        dao: ChartDAOProtocol,
        rison_params: list[int] | dict[str, Any] | None,
        token: str | None = Parameter(query="token", default=None),
    ) -> Stream:
        ids = extract_ids(rison_params)
        if not ids:
            raise CommandInvalidError("At least one ID is required for export")
        cmd = ExportChartsCommand(model_ids=ids, dao=cast("AsyncChartDAO", dao))
        buf = await cmd.execute()
        event_logger.log("chart.export", extra={"count": len(ids)})
        return Stream(
            stream_zip(buf),
            status_code=200,
            media_type="application/zip",
            headers=build_export_headers("charts_export.zip", token=token),
        )

    @get(
        "/favorite_status/",
        guards=[require_permission("can_read", "Chart")],
    )
    async def favorite_status(
        self,
        dao: ChartDAOProtocol,
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
        event_logger.log(
            "chart.add_favorite",
            object_ref=f"chart:{pk}",
            user_id=current_user.id,
        )
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
        event_logger.log(
            "chart.remove_favorite",
            object_ref=f"chart:{pk}",
            user_id=current_user.id,
        )
        return {"result": "OK"}

    @put(
        "/warm_up_cache",
        guards=[require_permission("can_write", "Chart")],
    )
    async def warm_up_cache(
        self, data: ChartCacheWarmUpRequest, dao: ChartDAOProtocol
    ) -> dict[str, Any]:
        cmd = WarmUpChartCacheCommand(
            dao=cast("AsyncChartDAO", dao),
            chart_id=data.chart_id,
            dashboard_id=data.dashboard_id,
            extra_filters=data.extra_filters,
        )
        result = await cmd.execute()
        event_logger.log("chart.warm_up_cache", object_ref=f"chart:{data.chart_id}")
        return {"result": [result]}

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
        ssh_tunnel_private_keys: str | None = None,
        ssh_tunnel_private_key_passwords: str | None = None,
    ) -> dict[str, str]:
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
        cmd = ImportChartsCommand(
            contents=buf,
            dao=cast("AsyncChartDAO", dao),
            overwrite=overwrite,
            passwords=passwords_dict,
            ssh_tunnel_passwords=ssh_dict,
            ssh_tunnel_private_keys=ssh_private_keys_dict,
            ssh_tunnel_private_key_passwords=ssh_private_key_passwords_dict,
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
        In Superset this is derived from ``superset.viz.viz_types``; superset
        exposes the same list as a static registry so the frontend can
        populate the chart-type picker without importing legacy viz code.
        """
        # BL-H2: Static registry of common Superset viz types.
        # This mirrors the keys produced by ``superset.viz.viz_types`` at the
        # time of the superset migration.  When the legacy viz module is
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
    async def get_chart_data(  # noqa: C901
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
        from superset.exceptions import SupersetValidationException

        # BL-C2: Check GLOBAL_ASYNC_QUERIES feature flag
        settings: SupersetSettings = cast(
            "SupersetSettings", getattr(state, "settings", None)
        )
        if getattr(settings, "global_async_queries", False):
            result_format = (format or "json").lower()
            result_type = (type or "full").lower()
            if result_format == "json" and result_type == "full":
                from superset.async_events.manager import build_job_metadata
                from superset.tasks.async_queries import load_chart_data_into_cache

                chart = await dao.find_by_id(pk)
                if not chart:
                    raise ObjectNotFoundError("Chart", pk)

                query_context_str = getattr(chart, "query_context", None)
                if not query_context_str:
                    raise SupersetValidationException(
                        "Chart has no query context saved"
                    )
                try:
                    form_data = _json.loads(query_context_str)
                except (ValueError, TypeError) as exc:
                    raise SupersetValidationException(
                        "Chart has invalid query context"
                    ) from exc

                channel_id = str(uuid.uuid4())
                job_id = str(uuid.uuid4())
                job_metadata = build_job_metadata(
                    channel_id=channel_id,
                    job_id=job_id,
                    user_id=current_user.id,
                    status="pending",
                )
                load_chart_data_into_cache.delay(job_metadata, form_data)
                return Response(
                    content={"channel_id": channel_id, "job_id": job_id},
                    status_code=202,
                )

        chart = await dao.find_by_id(pk)
        if not chart:
            raise ObjectNotFoundError("Chart", pk)

        query_context_str = getattr(chart, "query_context", None)
        if not query_context_str:
            raise SupersetValidationException("Chart has no query context saved")

        try:
            qc_data = _json.loads(query_context_str)
        except (ValueError, TypeError) as exc:
            raise SupersetValidationException(
                "Chart has invalid query context"
            ) from exc

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

        settings = cast("SupersetSettings", getattr(state, "settings", None))
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

        event_logger.log(
            "chart.data",
            object_ref=f"chart:{pk}",
            user_id=current_user.id,
        )
        return result

    @post(
        "/data",
        guards=[require_permission("can_read", "Chart")],
        status_code=200,
    )
    async def data(  # noqa: C901
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
            content_type_str = request.content_type[0] if request.content_type else ""
            if "multipart" in content_type_str or "form" in content_type_str:
                form = await request.form()
                form_data_str = form.get("form_data")
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

        import numpy as np
        import pandas as pd

        settings: SupersetSettings = cast(
            "SupersetSettings", getattr(state, "settings", None)
        )
        result_format = (getattr(data, "result_format", None) or "json").lower()
        result_type = (getattr(data, "result_type", None) or "full").lower()

        # BL-C2: Check GLOBAL_ASYNC_QUERIES feature flag
        if getattr(settings, "global_async_queries", False):
            if result_format == "json" and result_type == "full":
                from superset.async_events.manager import build_job_metadata
                from superset.tasks.async_queries import load_chart_data_into_cache

                channel_id = str(uuid.uuid4())
                job_id = str(uuid.uuid4())
                job_metadata = build_job_metadata(
                    channel_id=channel_id,
                    job_id=job_id,
                    user_id=current_user.id,
                    status="pending",
                )
                form_data = _msgspec.to_builtins(data)
                load_chart_data_into_cache.delay(job_metadata, form_data)
                return Response(
                    content={"channel_id": channel_id, "job_id": job_id},
                    status_code=202,
                )

        datasource = await ds_dao.get_datasource(
            data.datasource.type, data.datasource.id
        )
        if not datasource:
            raise ObjectNotFoundError("Datasource", data.datasource.id)

        # --- P1-5: result_type dispatch -------------------------------------------

        ds_ref = {"type": data.datasource.type, "id": data.datasource.id}

        if result_type == "query":
            # Return generated SQL without executing the query
            query_results: list[dict[str, Any]] = []
            for q_schema in data.queries:
                qobj = AsyncQueryObject.from_request(q_schema, ds_ref)
                query_dict = qobj.to_dict()
                sql, _from_dttm, _to_dttm = datasource._build_sql(query_dict)
                query_results.append(
                    {
                        "query": sql,
                        "status": "success",
                        "language": "sql",
                    }
                )
            event_logger.log("chart.data_post")
            return Response(
                content={"result": query_results},
                media_type="application/json",
            )

        if result_type == "samples":
            # Execute a simple SELECT * without metrics/filters
            row_limit = (
                data.queries[0].row_limit
                if data.queries and getattr(data.queries[0], "row_limit", None)
                else 1000
            )
            sql = f"SELECT * FROM {datasource.table_name} LIMIT {row_limit}"  # noqa: S608

            try:
                df = await datasource._execute_sql(sql)
                if not isinstance(df, pd.DataFrame):
                    df = pd.DataFrame()
            except Exception:
                df = pd.DataFrame()

            records = df.to_dict(orient="records")
            sample_result: dict[str, Any] = {
                "queries": [
                    {
                        "data": records,
                        "colnames": list(df.columns),
                        "coltypes": [],
                        "indexnames": list(range(len(records))),
                        "rowcount": len(records),
                        "status": "success",
                    }
                ],
            }
            event_logger.log("chart.data_post")
            return Response(
                content=sample_result,
                media_type="application/json",
            )

        # --- result_format: csv / xlsx (early return) ----------------------------

        if result_format in ("csv", "xlsx"):
            # Check can_csv permission
            if not await security_manager.can_access(
                "can_csv", "Superset", user=current_user
            ):
                from superset.exceptions import SupersetSecurityException

                raise SupersetSecurityException(
                    message="You don't have permission to download data"
                )

            query_objects = [
                AsyncQueryObject.from_request(q, ds_ref) for q in data.queries
            ]
            query_context = AsyncQueryContext(
                datasource=datasource,
                queries=query_objects,
                force=data.force,
                result_format=result_format,
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

            import zipfile

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
                    headers={"Content-Disposition": 'attachment; filename="data.xlsx"'},
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
                headers={"Content-Disposition": "attachment; filename=chart_data.zip"},
            )

        # --- Default JSON path (result_type: full / results / columns / etc.) ----

        query_objects = [AsyncQueryObject.from_request(q, ds_ref) for q in data.queries]
        query_context = AsyncQueryContext(
            datasource=datasource,
            queries=query_objects,
            force=data.force,
            result_format=result_format,
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

        # Convert DataFrames to JSON-serializable dicts before response
        from datetime import date as _date_t
        from datetime import datetime as _datetime_t
        from decimal import Decimal as _Decimal_t

        from superset.typing import GenericDataType

        for q in result.get("queries", []):
            if isinstance(q, dict) and isinstance(q.get("df"), pd.DataFrame):
                df = q.pop("df")
                q["data"] = df.to_dict(orient="records")
                q["colnames"] = list(df.columns)
                # Frontend viz plugins (gauge, graph, table, etc.) need
                # ``coltypes`` to know whether each column is temporal,
                # numeric, boolean or string. Map pandas dtypes to the
                # ``GenericDataType`` integers the frontend expects:
                # 0=NUMERIC, 1=STRING, 2=TEMPORAL, 3=BOOLEAN.
                #
                # For ``object`` dtype columns (the asyncpg path returns
                # ``Decimal`` for ``SUM(bigint)`` and ``date`` for
                # date/datetime, both of which collapse to ``object``),
                # peek at the first non-null value to infer the real type
                # rather than blindly tagging everything STRING.
                coltypes: list[int] = []
                for col in df.columns:
                    dtype = df[col].dtype
                    if pd.api.types.is_bool_dtype(dtype):
                        coltypes.append(GenericDataType.BOOLEAN)
                    elif pd.api.types.is_datetime64_any_dtype(dtype):
                        coltypes.append(GenericDataType.TEMPORAL)
                    elif pd.api.types.is_numeric_dtype(dtype):
                        coltypes.append(GenericDataType.NUMERIC)
                    else:
                        sample = next(
                            (v for v in df[col] if v is not None and v == v),  # noqa: PLR0124
                            None,
                        )
                        if isinstance(sample, bool):
                            coltypes.append(GenericDataType.BOOLEAN)
                        elif isinstance(sample, (int, float, _Decimal_t)):
                            coltypes.append(GenericDataType.NUMERIC)
                        elif isinstance(sample, (_datetime_t, _date_t)):
                            coltypes.append(GenericDataType.TEMPORAL)
                        else:
                            coltypes.append(GenericDataType.STRING)
                q["coltypes"] = coltypes
                q.setdefault("rowcount", len(df))

        # P2-11: NaN / Inf / numpy / datetime / Decimal cleanup for valid JSON.
        #
        # asyncpg returns PostgreSQL NUMERIC (and ``SUM(bigint)``) as
        # Python ``Decimal``; without this normalization those values
        # end up as strings in the JSON payload, breaking numeric
        # comparisons in Cypress snapshots (e.g. table viz sort tests).
        #
        # Datetime / date / Timestamp values are serialized as epoch
        # milliseconds to match the original Flask chart data API
        # (``json_int_dttm_ser`` in ``superset_old/utils/json.py``).
        # Frontend chart components (Table, TimeSeries, …) expect
        # numeric timestamps so they can apply ``smart_date`` formatting
        # driven by ``time_grain_sqla`` — ISO strings break this flow.
        from datetime import date as _date_t
        from datetime import datetime as _datetime_t
        from decimal import Decimal

        from superset.utils.json import datetime_to_epoch

        _EPOCH_DATE = _datetime_t(1970, 1, 1).date()

        for q in result.get("queries", []):
            if isinstance(q, dict) and "data" in q:
                for row in q["data"]:
                    for key, val in row.items():
                        if isinstance(val, float) and (
                            math.isnan(val) or math.isinf(val)
                        ):
                            row[key] = None
                        elif isinstance(val, Decimal):
                            # Preserve integer-ness when the value has no
                            # fractional part (e.g. SUM over BIGINT) —
                            # matches the original Flask/SQLAlchemy path
                            # which emitted ints for whole-number sums.
                            if val == val.to_integral_value():
                                row[key] = int(val)
                            else:
                                row[key] = float(val)
                        elif isinstance(val, np.integer):
                            row[key] = int(val)
                        elif isinstance(val, np.floating):
                            row[key] = float(val) if not np.isnan(val) else None
                        elif isinstance(val, np.bool_):
                            row[key] = bool(val)
                        elif isinstance(val, pd.Timestamp):
                            if pd.isna(val):
                                row[key] = None
                            else:
                                row[key] = datetime_to_epoch(val.to_pydatetime())
                        elif isinstance(val, _datetime_t):
                            row[key] = datetime_to_epoch(val)
                        elif isinstance(val, _date_t):
                            # plain ``date`` (no time component)
                            row[key] = (val - _EPOCH_DATE).total_seconds() * 1000

        # Ensure indexnames is present in each query result
        for q in result.get("queries", []):
            if isinstance(q, dict):
                q["indexnames"] = list(range(len(q.get("data", []))))

        event_logger.log("chart.data_post")
        # Frontend expects {"result": [...]} not {"queries": [...]}
        response_payload = {"result": result.get("queries", [])}

        # Port of original Flask ``json.dumps(..., default=json_int_dttm_ser)``
        # from ``superset_old/charts/data/api.py``. The original serializer
        # walks the entire response tree and converts ANY datetime/date value
        # to epoch milliseconds — not just the ones inside ``data`` rows.
        # This covers top-level and nested fields such as ``from_dttm``,
        # ``to_dttm``, ``cached_dttm``, ``changed_on``, and the values inside
        # ``applied_time_extras``. The in-place normalization above still
        # handles Decimal/numpy/NaN inside ``data`` rows which msgspec cannot
        # natively serialize — but datetime conversion is now global.
        from superset.utils.json import json_int_dttm_ser

        def _enc_hook(obj: Any) -> Any:
            # pd.Timestamp is a datetime subclass, so isinstance checks in
            # ``json_int_dttm_ser`` catch it. pd.NaT is also a Timestamp but
            # ``pd.isna`` is True — emit None in that case.
            if isinstance(obj, pd.Timestamp):
                if pd.isna(obj):
                    return None
                return datetime_to_epoch(obj.to_pydatetime())
            try:
                return json_int_dttm_ser(obj)
            except TypeError:
                # Fall back to string to avoid 500s on truly exotic types.
                return str(obj)

        encoded = _msgspec.json.encode(response_payload, enc_hook=_enc_hook)
        return Response(content=encoded, media_type="application/json")

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
        settings: SupersetSettings = cast(
            "SupersetSettings", getattr(state, "settings", None)
        )
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
