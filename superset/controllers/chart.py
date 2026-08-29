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

import asyncio
import io
import json as _json
import logging
import math
import uuid
from typing import Any, cast, TYPE_CHECKING

from litestar import Controller, delete, get, post, put
from litestar.connection import Request
from litestar.datastructures import State
from litestar.di import Provide
from litestar.exceptions import NotAuthorizedException
from litestar.params import Parameter
from litestar.response import Response, Stream

from superset.commands.chart.create import CreateChartCommand
from superset.commands.chart.data.get_data_command import ChartDataCommand
from superset.commands.chart.delete import BulkDeleteChartsCommand, DeleteChartCommand
from superset.commands.chart.export import ExportChartsCommand
from superset.commands.chart.fave import AddFavoriteChartCommand
from superset.commands.chart.importers.dispatcher import ImportChartsCommand
from superset.commands.chart.unfave import RemoveFavoriteChartCommand
from superset.commands.chart.update import UpdateChartCommand
from superset.commands.chart.warm_up_cache import WarmUpChartCacheCommand
from superset.common.query_context import AsyncQueryContext
from superset.common.query_context_processor import (
    AsyncQueryContextProcessor,
    load_cached_query_context_form,
)
from superset.common.query_object import AsyncQueryObject
from superset.controllers.base import (
    build_export_headers,
    build_rison_query_params,
    extract_ids,
    extract_ids_required,
    get_distinct_payload,
    get_info_payload,
    get_related_payload,
    parse_import_request,
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

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Chart data response serialization
# ---------------------------------------------------------------------------
def _truncate_results_query(q: dict[str, Any]) -> dict[str, Any]:
    """Reduce a ``result_type=results`` query payload to the five keys emitted
    for ``RESULTS`` (data / colnames / coltypes / rowcount / sql_rowcount).
    Only applied to non-failed queries by the caller.
    """
    return {
        "data": q.get("data"),
        "colnames": q.get("colnames"),
        "coltypes": q.get("coltypes"),
        "rowcount": q.get("rowcount"),
        "sql_rowcount": q.get("sql_rowcount"),
    }


def _effective_result_types(result: dict[str, Any], n: int) -> list[str]:
    """Per-query effective result_type: ``query_obj.result_type`` wins
    over ``query_context.result_type``.
    """
    qc = result.get("query_context")
    ctx_rt = getattr(qc, "result_type", None)
    qos = list(getattr(qc, "queries", []) or [])
    return [
        (getattr(qos[i], "result_type", None) if i < len(qos) else None)
        or ctx_rt
        or "full"
        for i in range(n)
    ]


def _normalize_post_processed_value(val: Any, epoch_date: Any) -> Any:
    """JSON-safe scalar for post-processed (pivot/table) dict-of-dicts values.

    Mirrors the in-place normalization applied to list-of-records ``data`` rows
    (NaN/Inf→None, Decimal→int/float, numpy scalars, Timestamp/datetime/date→
    epoch ms) so the msgspec encoder — which rejects NaN — doesn't 500 on the
    ``{col: {idx: val}}`` output of ``apply_client_processing``.
    """
    from datetime import date as _date_t, datetime as _datetime_t
    from decimal import Decimal

    import numpy as np
    import pandas as pd

    from superset.utils.json import datetime_to_epoch

    if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
        return None
    if isinstance(val, Decimal):
        return int(val) if val == val.to_integral_value() else float(val)
    if isinstance(val, np.integer):
        return int(val)
    if isinstance(val, np.floating):
        return float(val) if not np.isnan(val) else None
    if isinstance(val, np.bool_):
        return bool(val)
    if isinstance(val, pd.Timestamp):
        return None if pd.isna(val) else datetime_to_epoch(val.to_pydatetime())
    if isinstance(val, _datetime_t):
        return datetime_to_epoch(val)
    if isinstance(val, _date_t):
        return (val - epoch_date).total_seconds() * 1000
    return val


def _render_chart_data_payload(  # noqa: C901
    result: dict[str, Any],
    *,
    is_guest: bool,
    form_data: dict[str, Any] | None = None,
    datasource: Any | None = None,
) -> Response[Any]:
    """Serialize ``ChartDataCommand`` output as ``{"result": [<query>, ...]}``.

    Drops ``query_context`` (which holds a non-JSON ``SqlaTable`` ORM instance),
    converts each query's pandas ``DataFrame`` to ``data``/``colnames``/``coltypes``,
    normalizes Decimal/numpy/NaN/datetime values inside row dicts, and emits the
    response through ``msgspec`` with the ``json_int_dttm_ser`` fallback.

    When ``form_data`` and ``datasource`` are supplied and the result type
    is ``post_processed``, ``apply_client_processing`` is applied (used by
    email reports for Pivot Table v2 / Table charts).
    """
    from datetime import date as _date_t, datetime as _datetime_t
    from decimal import Decimal

    import msgspec as _msgspec
    import numpy as np
    import pandas as pd

    from superset.typing import GenericDataType
    from superset.utils.json import datetime_to_epoch, json_int_dttm_ser

    queries = result.get("queries", []) or []

    # Strip raw SQL for guest users so embedded dashboards never leak it.
    if is_guest:
        for q in queries:
            if isinstance(q, dict):
                q.pop("query", None)

    # 1. df -> data/colnames/coltypes
    for q in queries:
        if isinstance(q, dict) and isinstance(q.get("df"), pd.DataFrame):
            df = q.pop("df")
            q.setdefault("data", df.to_dict(orient="records"))
            q.setdefault("colnames", list(df.columns))
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
                    elif isinstance(sample, (int, float, Decimal)):
                        coltypes.append(GenericDataType.NUMERIC)
                    elif isinstance(sample, (_datetime_t, _date_t)):
                        coltypes.append(GenericDataType.TEMPORAL)
                    else:
                        coltypes.append(GenericDataType.STRING)
            q["coltypes"] = coltypes
            q.setdefault("rowcount", len(df))

    # 2. NaN / Inf / numpy / datetime / Decimal cleanup inside row dicts
    epoch_date = _datetime_t(1970, 1, 1).date()
    for q in queries:
        if isinstance(q, dict) and "data" in q:
            for row in q["data"]:
                if not isinstance(row, dict):
                    continue
                for key, val in row.items():
                    if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
                        row[key] = None
                    elif isinstance(val, Decimal):
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
                        row[key] = (
                            None
                            if pd.isna(val)
                            else datetime_to_epoch(val.to_pydatetime())
                        )
                    elif isinstance(val, _datetime_t):
                        row[key] = datetime_to_epoch(val)
                    elif isinstance(val, _date_t):
                        row[key] = (val - epoch_date).total_seconds() * 1000

    # When result_type == post_processed, apply pivot/table transforms.
    qc = result.get("query_context")
    _result_type = getattr(qc, "result_type", None) or ""
    if str(_result_type).lower() == "post_processed" and form_data is not None:
        from superset.charts.post_processing import apply_client_processing

        apply_client_processing(result, form_data=form_data, datasource=datasource)
        # ``apply_client_processing`` may produce dict-of-dicts output
        # (``processed_df.to_dict()`` yields ``{col: {idx: val}}``);
        # clean NaN/Inf/numpy/Decimal/datetime in those nested values so
        # the msgspec encoder doesn't 500.
        for q in queries:
            if isinstance(q, dict) and isinstance(q.get("data"), dict):
                for _col, _col_map in q["data"].items():
                    if isinstance(_col_map, dict):
                        for _k, _val in list(_col_map.items()):
                            _col_map[_k] = _normalize_post_processed_value(
                                _val, epoch_date
                            )

    # 3. ensure indexnames is present for non-post-processed queries
    for q in queries:
        if isinstance(q, dict) and "indexnames" not in q:
            q["indexnames"] = list(range(len(q.get("data", []))))

    # Truncate result_type=results queries to the 5-key RESULTS shape (non-failed only).
    result_types = _effective_result_types(result, len(queries))
    queries = [
        _truncate_results_query(q)
        if isinstance(q, dict) and rt == "results" and q.get("status") != "failed"
        else q
        for q, rt in zip(queries, result_types, strict=True)
    ]

    def _enc_hook(obj: Any) -> Any:
        if isinstance(obj, pd.Timestamp):
            if pd.isna(obj):
                return None
            return datetime_to_epoch(obj.to_pydatetime())
        try:
            return json_int_dttm_ser(obj)
        except TypeError:
            return str(obj)

    encoded = _msgspec.json.encode({"result": queries}, enc_hook=_enc_hook)
    return Response(content=encoded, media_type="application/json")


def _table_like_file_response(
    result: dict[str, Any],
    result_format: str,
    verbose_map: dict[str, str] | None = None,
) -> Response[Any]:
    """Render a chart-data ``result`` as a CSV / XLSX download.

    A single query returns the file directly; multiple queries are bundled
    into a ZIP. Shared by the POST /data and GET /{pk}/data/ handlers.
    """
    import zipfile
    from datetime import datetime as _dt

    import pandas as pd

    queries = result.get("queries", []) or []
    frames: list[pd.DataFrame] = []
    for q in queries:
        if isinstance(q, dict):
            if isinstance(q.get("df"), pd.DataFrame):
                frames.append(q["df"])
            elif q.get("data"):
                frames.append(pd.DataFrame(q["data"]))

    _ts = _dt.now().strftime("%Y%m%d_%H%M%S")

    if len(frames) <= 1:
        df = frames[0] if frames else pd.DataFrame()
        if result_format == "csv":
            csv_content = AsyncQueryContextProcessor.get_data(
                df, "csv", verbose_map=verbose_map
            )
            return Response(
                content=csv_content,
                media_type="text/csv",
                headers={"Content-Disposition": f"attachment; filename={_ts}.csv"},
            )
        xlsx_data = AsyncQueryContextProcessor.get_data(
            df, "xlsx", verbose_map=verbose_map
        )
        return Response(
            content=xlsx_data,
            media_type=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
            headers={"Content-Disposition": f"attachment; filename={_ts}.xlsx"},
        )

    ext = "csv" if result_format == "csv" else "xlsx"
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for idx, df in enumerate(frames, start=1):
            file_data = AsyncQueryContextProcessor.get_data(
                df, result_format, verbose_map=verbose_map
            )
            file_bytes = (
                file_data.encode("utf-8") if isinstance(file_data, str) else file_data
            )
            zf.writestr(f"query_{idx}.{ext}", file_bytes)
    zip_buf.seek(0)
    return Response(
        content=zip_buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={_ts}.zip"},
    )


# ---------------------------------------------------------------------------
# Async chart-data channel resolution
# ---------------------------------------------------------------------------
def _resolve_async_channel_id(
    request: Request[Any, Any, Any],
    settings: Any,
) -> str | None:
    """Return the ``channel`` claim from the request's ``async-token`` cookie.

    Thin wrapper around the shared
    :func:`superset.middleware.async_token.resolve_async_channel_id_from_request`
    helper so that all channel-resolution logic lives in one place.
    Returns ``None`` when the cookie is missing or invalid — the caller maps
    that to HTTP 401.
    """
    from superset.middleware.async_token import resolve_async_channel_id_from_request

    return resolve_async_channel_id_from_request(request, settings)


async def _try_cached_chart_data(
    *,
    query_context: AsyncQueryContext,
    datasource: Any,
    settings: Any,
    security_manager: Any,
    current_user: Any,
) -> Response[Any] | None:
    """Cache-first short-circuit for the GLOBAL_ASYNC_QUERIES submit path.

    Before dispatching a background Celery job, run the command with
    ``force_cached=True`` and, on a cache hit, return the already-computed
    result inline (HTTP 200) so a chart whose data is already cached skips
    the whole async round-trip.

    ``validate()`` (``raise_for_access``) runs first, so an inline cache hit
    can never bypass the access check (a denial propagates as 403).

    Returns the rendered ``Response`` on a cache hit, or ``None`` on a cache
    miss / any failure (the caller then dispatches the background job and
    returns 202).

    The whole attempt is best-effort: any failure falls through to dispatch.
    An inline ``Response`` is only returned after ``validate()``
    (``raise_for_access``) has passed.
    """
    from superset.extensions import cache_manager

    try:
        processor = AsyncQueryContextProcessor(
            datasource=datasource,
            settings=settings,
            security_manager=security_manager,
            user=current_user,
            cache_manager=cache_manager,
            query_context=query_context,
        )
        cmd = ChartDataCommand(query_context=query_context, processor=processor)
        # Access check first — must run before cache hit returns.
        await cmd.validate()
        result = await cmd.run(force_cached=True)
    except Exception:  # noqa: BLE001 — best-effort; fall through to dispatch
        logger.debug("GAQ cache-first probe missed/failed", exc_info=True)
        return None
    # A per-query failed status is a miss too (the processor returns a
    # failed-dict rather than raising on a ``force_cached`` miss).
    queries = result.get("queries", [])
    if not queries or any(
        isinstance(q, dict) and q.get("status") == "failed" for q in queries
    ):
        return None
    return _render_chart_data_payload(
        result,
        is_guest=security_manager.is_guest_user(current_user),
    )


# ---------------------------------------------------------------------------
# Custom RISON filters for charts
# ---------------------------------------------------------------------------
def _chart_custom_filters(current_user: Any) -> dict[str, Any]:  # noqa: C901
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

    def _chart_all_text(model_cls: Any, value: Any) -> Any:
        """OR-match the search term across slice_name, description,
        viz_type and the associated dataset's table_name.
        """
        if not value:
            return None
        from sqlalchemy import or_, select as sa_select

        from superset.models.connectors import SqlaTable

        ilike_value = f"%{value}%"
        table_subq = sa_select(SqlaTable.id).where(
            SqlaTable.table_name.ilike(ilike_value)
        )
        return or_(
            model_cls.slice_name.ilike(ilike_value),
            model_cls.description.ilike(ilike_value),
            model_cls.viz_type.ilike(ilike_value),
            model_cls.datasource_id.in_(table_subq),
        )

    def _chart_tag_name(model_cls: Any, value: Any) -> Any:
        """Resolve the slice via ``TaggedObject(object_type='chart',
        tag_id=<Tag.id WHERE Tag.name == value>)``.
        """
        if not value:
            return None
        from sqlalchemy import select as sa_select

        from superset.models.tags import Tag, TaggedObject

        tag_id_subq = sa_select(Tag.id).where(Tag.name == value)
        tagged_subq = sa_select(TaggedObject.object_id).where(
            TaggedObject.object_type == "chart",
            TaggedObject.tag_id.in_(tag_id_subq),
        )
        return model_cls.id.in_(tagged_subq)

    def _chart_tag_id(model_cls: Any, value: Any) -> Any:
        if value is None:
            return None
        from sqlalchemy import select as sa_select

        from superset.models.tags import TaggedObject

        try:
            tag_id_int = int(value)
        except (TypeError, ValueError):
            return None
        tagged_subq = sa_select(TaggedObject.object_id).where(
            TaggedObject.object_type == "chart",
            TaggedObject.tag_id == tag_id_int,
        )
        return model_cls.id.in_(tagged_subq)

    def _chart_has_created_by(model_cls: Any, value: Any) -> Any:
        if value is True:
            return model_cls.created_by_fk.isnot(None)
        if value is False:
            return model_cls.created_by_fk.is_(None)
        return None

    def _chart_created_by_me(model_cls: Any, value: Any) -> Any:
        from sqlalchemy import or_

        user_id = getattr(current_user, "id", None)
        if user_id is None:
            return None
        return or_(
            model_cls.created_by_fk == user_id,
            model_cls.changed_by_fk == user_id,
        )

    def _chart_owned_created_favored_by_me(model_cls: Any, value: Any) -> Any:
        from sqlalchemy import or_, select as sa_select

        from superset.models.core import FavStar

        user_id = getattr(current_user, "id", None)
        if user_id is None:
            return None
        owner_subq = sa_select(model_cls.id).where(model_cls.owners.any(id=user_id))
        fav_subq = sa_select(FavStar.obj_id).where(
            FavStar.class_name == "slice",
            FavStar.user_id == user_id,
        )
        return or_(
            model_cls.id.in_(owner_subq),
            model_cls.created_by_fk == user_id,
            model_cls.changed_by_fk == user_id,
            model_cls.id.in_(fav_subq),
        )

    return {
        "chart_is_favorite": _chart_is_favorite,
        "chart_is_certified": _chart_is_certified,
        "chart_all_text": _chart_all_text,
        "chart_tags": _chart_tag_name,
        "chart_tag_id": _chart_tag_id,
        "chart_has_created_by": _chart_has_created_by,
        "chart_created_by_me": _chart_created_by_me,
        "chart_owned_created_favored_by_me": _chart_owned_created_favored_by_me,
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
    import markdown as md
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


def _context_form_data(payload: Any) -> dict[str, Any]:
    """Return the ``form_data`` a query-context payload carries.

    ``security_manager.query_context_modified`` needs it to find the saved
    chart (``form_data["slice_id"]``) and compare the requested metrics and
    columns against it — the "Guest user cannot modify chart payload" control.
    Upstream's ``QueryContextFactory`` receives ``form_data`` directly; here the
    controllers build the context, so each construction site passes it through.

    Accepts the chart-data body in either shape: a plain dict (legacy JSON
    body) or a msgspec struct.
    """
    if payload is None:
        return {}
    nested = (
        payload.get("form_data")
        if isinstance(payload, dict)
        else getattr(payload, "form_data", None)
    )
    return nested if isinstance(nested, dict) else {}


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
        if order_by is None:
            order_by = [Slice.changed_on.desc()]

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
        await event_logger.alog_with_context("chart.list")
        # ``Slice.thumbnail_url`` → digest does blocking metadata-DB I/O (the
        # sync RLS lookups in ``thumbnails.digest``).  Compute every chart's
        # thumbnail URL in a single batch off the event-loop thread via
        # ``asyncio.to_thread`` (which copies the ``contextvars`` context, so
        # the digest executor's ``get_current_user`` stays visible) and inject
        # the result below — instead of reading the property on the loop.
        # ``thumbnail_url`` is therefore dropped from the serialized column set
        # (it was overwritten manually anyway) and re-declared on the response
        # ``list_columns`` afterwards so the frontend contract is unchanged.
        thumbnail_urls = await asyncio.to_thread(
            lambda: {chart.id: chart.thumbnail_url for chart in charts}
        )
        payload = serialize_list_response(
            charts,
            total,
            [
                "is_managed_externally",
                "certified_by",
                "certification_details",
                "cache_timeout",
                "changed_by.first_name",
                "changed_by.last_name",
                "changed_by.id",
                "changed_by_name",
                "changed_on_delta_humanized",
                "changed_on_dttm",
                "changed_on_utc",
                "created_by.first_name",
                "created_by.id",
                "created_by.last_name",
                "created_by_name",
                "created_on_delta_humanized",
                "datasource_id",
                "datasource_name_text",
                "datasource_type",
                "datasource_url",
                "description",
                "description_markeddown",
                "edit_url",
                "form_data",
                "id",
                "last_saved_at",
                "last_saved_by.id",
                "last_saved_by.first_name",
                "last_saved_by.last_name",
                "owners.first_name",
                "owners.id",
                "owners.last_name",
                "dashboards.id",
                "dashboards.dashboard_title",
                "params",
                "slice_name",
                "slice_url",
                "table.default_endpoint",
                "table.table_name",
                "url",
                "viz_type",
                "tags.id",
                "tags.name",
                "tags.type",
                "uuid",
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
        # Re-declare ``thumbnail_url`` on the response contract (dropped from
        # the serialized column set above so the blocking digest isn't computed
        # on the event loop); the value is injected per-item from the batch
        # computed off-thread.
        if "thumbnail_url" not in payload["list_columns"]:
            payload["list_columns"].append("thumbnail_url")
            payload["label_columns"]["thumbnail_url"] = "Thumbnail Url"
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
                item["thumbnail_url"] = thumbnail_urls.get(chart_id)
                item["url"] = chart_obj.url
                item["edit_url"] = chart_obj.edit_url
                item["slice_url"] = chart_obj.slice_url

                # changed_on_dttm — epoch milliseconds of changed_on
                item["changed_on_dttm"] = (
                    float(chart_obj.changed_on.timestamp()) * 1000
                    if chart_obj.changed_on
                    else None
                )

                # description_markeddown — HTML from markdown description
                desc = chart_obj.description or ""
                if desc:
                    item["description_markeddown"] = _md_to_html(desc)
                else:
                    item["description_markeddown"] = ""

                # form_data — uses Slice.form_data property which applies
                # update_time_range() to migrate since/until → time_range.
                item["form_data"] = chart_obj.form_data

                # table.default_endpoint and table.table_name
                tbl = chart_obj.table
                item["table"] = (
                    {
                        "default_endpoint": tbl.default_endpoint if tbl else None,
                        "table_name": tbl.table_name if tbl else None,
                    }
                    if tbl
                    else None
                )
                # Drop the flat-dotted keys that _serialize_item emits for the
                # ``table.default_endpoint`` / ``table.table_name`` entries in
                # ``list_columns`` — the frontend reads ``item["table"]``.
                item.pop("table.default_endpoint", None)
                item.pop("table.table_name", None)

        return payload

    @get(
        "/_info",
        guards=[require_permission("can_read", "Chart")],
    )
    async def info(
        self,
        dao: ChartDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
        rison_params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """GET /api/v1/chart/_info — API metadata for frontend."""
        return await get_info_payload(
            dao=dao,
            model_name="Chart",
            permissions=["can_warm_up_cache", "can_read", "can_write", "can_export"],
            security_manager=security_manager,
            current_user=current_user,
            class_permission_name="Chart",
            rison_params=rison_params,
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
        """GET /api/v1/chart/distinct/{column_name} — distinct values for filters.

        Upstream ``ChartRestApi`` does not override ``allowed_distinct_fields`` →
        inherits the empty default → every distinct request 404s. Mirror that
        with ``allowed_fields=frozenset()`` rather than answering with stale or
        broken (relationship column → ``NotImplementedError``) data.
        """
        from superset.db.filters import chart_access_filters

        base_filters = await chart_access_filters(security_manager, current_user)
        return await get_distinct_payload(
            dao=dao,
            column_name=column_name,
            rison_params=rison_params,
            allowed_fields=frozenset(),
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
        await event_logger.alog_with_context(
            "chart.get", object_ref=f"chart:{id_or_uuid}"
        )
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
        await event_logger.alog_with_context(
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
        await event_logger.alog_with_context(
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
        await event_logger.alog_with_context(
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
        await event_logger.alog_with_context(
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
        current_user: UserProtocol,
        security_manager: SecurityManagerProtocol,
        rison_params: dict[str, Any] | None,
    ) -> Response[Any]:
        """Compute and cache a chart screenshot.

        Returns 200 when the existing cache payload is still valid;
        returns 202 and dispatches the Celery task when a new screenshot
        must be generated.
        """
        import asyncio

        # Gate on THUMBNAILS feature flag
        feature_flags = getattr(state.settings, "feature_flags", {})
        if not feature_flags.get("THUMBNAILS", False):
            return Response(
                content={"message": "Not found"},
                status_code=404,
                media_type="application/json",
            )

        from sqlalchemy.orm import selectinload

        # Eager-load the datasource (``table``) so ``chart.digest`` →
        # get_chart_digest can read ``chart.datasource`` without a sync
        # lazy-load (MissingGreenlet) on the async session.
        # Access-scoped lookup (404 for an inaccessible chart) — the screenshot
        # endpoints must not serve images or trigger Celery compute for a chart
        # the user cannot access.
        from superset.db.filters import chart_access_filters
        from superset.models.slice import Slice
        from superset.utils.screenshots import (
            ChartScreenshot,
            ScreenshotCachePayload,
        )

        _base = await chart_access_filters(security_manager, current_user)
        _found = await dao.find_all(
            filters=[Slice.id == pk] + (_base or []),
            page=0,
            page_size=1,
            options=[selectinload(Slice.table)],
        )
        chart = _found[0] if _found else None
        if not chart:
            raise ObjectNotFoundError("Chart", pk)

        # Extract optional rison query params (mirrors screenshot_query_schema)
        rison_dict: dict[str, Any] = rison_params or {}
        force: bool = bool(rison_dict.get("force", False))
        window_size = rison_dict.get("window_size") or (800, 600)
        # Don't shrink the image if thumb_size is not specified
        thumb_size = rison_dict.get("thumb_size") or window_size

        chart_url = f"/explore/?slice_id={pk}"
        chart_digest = getattr(chart, "digest", None) or str(pk)
        screenshot_obj = ChartScreenshot(chart_url, chart_digest)
        cache_key = await asyncio.to_thread(
            screenshot_obj.get_cache_key, window_size, thumb_size
        )
        cache_payload = (
            await asyncio.to_thread(ChartScreenshot.get_from_cache_key, cache_key)
            or ScreenshotCachePayload()
        )
        image_url = f"/api/v1/chart/{pk}/screenshot/{cache_key}/"

        def _build_response(status_code: int) -> Response[Any]:
            return Response(
                content={
                    "cache_key": cache_key,
                    "chart_url": chart_url,
                    "image_url": image_url,
                    "task_updated_at": cache_payload.get_timestamp(),
                    "task_status": cache_payload.get_status(),
                },
                status_code=status_code,
                media_type="application/json",
            )

        if cache_payload.should_trigger_task(force):
            await asyncio.to_thread(
                screenshot_obj.cache.set,
                cache_key,
                ScreenshotCachePayload().to_dict(),
            )
            from superset.tasks.thumbnails import cache_chart_thumbnail

            cache_chart_thumbnail.delay(
                current_user=getattr(current_user, "username", None),
                chart_id=str(chart.id),
                window_size=window_size,
                thumb_size=thumb_size,
                force=force,
            )
            return _build_response(202)
        return _build_response(200)

    @get(
        "/{pk:int}/screenshot/{digest:str}/",
        guards=[require_permission("can_read", "Chart")],
        media_type="image/png",
    )
    async def screenshot(
        self,
        pk: int,
        digest: str,
        dao: ChartDAOProtocol,
        state: State,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
    ) -> Response[Any]:
        """Get a computed screenshot from cache.

        The *digest* path parameter is the cache key written by the Celery
        screenshot task.  If the cache contains an image we serve it;
        otherwise we return 404.
        """
        import asyncio

        # Gate on the feature flag *before* importing screenshots — that
        # module pulls in webdriver/playwright/selenium which can raise
        # at import time in deployments without the optional deps.
        #
        # NB: every non-image response here returns JSON (``image/png`` ONLY on
        # a real image). The frontend ``ImageLoader``
        # (packages/.../ListViewCard/ImageLoader.tsx) fetches the URL and tests
        # ``/image/.test(blob.type)``; an empty ``image/png`` body would pass
        # that test and render a blank/broken tile instead of the fallback.
        feature_flags = getattr(state.settings, "feature_flags", {})
        if not feature_flags.get("THUMBNAILS", False):
            return Response(
                content={"message": "Not found"},
                status_code=404,
                media_type="application/json",
            )

        # Access-scoped lookup — this endpoint serves the
        # cached PNG bytes, so it must not return an image for a chart the user
        # cannot access.
        from superset.db.filters import chart_access_filters
        from superset.models.slice import Slice
        from superset.utils.screenshots import (
            ChartScreenshot,
            ScreenshotImageNotAvailableException,
            StatusValues,
        )

        _base = await chart_access_filters(security_manager, current_user)
        _found = await dao.find_all(
            filters=[Slice.id == pk] + (_base or []),
            page=0,
            page_size=1,
        )
        chart = _found[0] if _found else None
        if not chart:
            raise ObjectNotFoundError("Chart", pk)

        cache_payload = await asyncio.to_thread(
            ChartScreenshot.get_from_cache_key, digest
        )
        if cache_payload and cache_payload.status == StatusValues.UPDATED:
            try:
                image = cache_payload.get_image()
            except ScreenshotImageNotAvailableException:
                return Response(
                    content={"message": "Not found"},
                    status_code=404,
                    media_type="application/json",
                )
            return Response(
                content=image.getvalue(),
                status_code=200,
                media_type="image/png",
            )
        return Response(
            content={"message": "Not found"},
            status_code=404,
            media_type="application/json",
        )

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
        security_manager: SecurityManagerProtocol,
    ) -> Response[Any]:
        """Compute or get already computed chart thumbnail from cache.

        If the chart's current digest differs from *digest* we redirect to
        the canonical URL.  Otherwise we check the thumbnail cache: if the
        image exists we serve it directly; if not we queue a Celery task and
        return 202.
        """
        import asyncio

        from litestar.response import Redirect

        # Gate on the feature flag *before* importing screenshots —
        # screenshots imports webdriver which depends on optional
        # extensions (machine_auth_provider_factory).
        #
        # NB: non-image responses (404 / 202) return JSON, not an empty
        # ``image/png`` — the frontend ``ImageLoader`` keys off
        # ``/image/.test(blob.type)`` and an empty image body would render a
        # blank tile instead of the fallback.
        feature_flags = getattr(state.settings, "feature_flags", {})
        if not feature_flags.get("THUMBNAILS", False):
            return Response(
                content={"message": "Not found"},
                status_code=404,
                media_type="application/json",
            )

        from sqlalchemy.orm import selectinload

        # Eager-load the datasource (``table``) so the ``chart.digest`` read
        # below doesn't trip a sync lazy-load (MissingGreenlet).
        from superset.db.filters import chart_access_filters
        from superset.models.slice import Slice
        from superset.utils.screenshots import (
            ChartScreenshot,
            ScreenshotCachePayload,
            ScreenshotImageNotAvailableException,
        )

        _base = await chart_access_filters(security_manager, current_user)
        _found = await dao.find_all(
            filters=[Slice.id == pk] + (_base or []),
            page=0,
            page_size=1,
            options=[selectinload(Slice.table)],
        )
        chart = _found[0] if _found else None
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
            # JSON, NOT an empty ``image/png`` (see ImageLoader note above).
            return Response(
                content={
                    "task_updated_at": cache_payload.get_timestamp(),
                    "task_status": cache_payload.get_status(),
                },
                status_code=202,
                media_type="application/json",
            )

        # Serve from cache
        try:
            image = cache_payload.get_image()
        except ScreenshotImageNotAvailableException:
            return Response(
                content={"message": "Not found"},
                status_code=404,
                media_type="application/json",
            )
        return Response(
            content=image.getvalue(),
            status_code=200,
            media_type="image/png",
        )

    @get(
        "/export/",
        guards=[require_permission("can_export", "Chart")],
        media_type="application/zip",
    )
    async def export(
        self,
        dao: ChartDAOProtocol,
        rison_params: list[int] | dict[str, Any] | None,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
        token: str | None = Parameter(query="token", default=None),
    ) -> Stream:
        ids = extract_ids(rison_params)
        if not ids:
            raise CommandInvalidError("At least one ID is required for export")
        # Nest every ZIP entry under ``chart_export_{timestamp}/`` so the v1 importer's
        # ``remove_root`` (parts[1:]) strips it back off and the re-import
        # round-trip works. Without the root prefix ``remove_root("metadata.yaml")``
        # returns ``"."`` and the bundle fails validation with
        # ``Missing metadata.yaml``.
        from datetime import datetime as _datetime

        timestamp = _datetime.now().strftime("%Y%m%dT%H%M%S")
        root = f"chart_export_{timestamp}"
        cmd = ExportChartsCommand(
            model_ids=ids,
            dao=cast("AsyncChartDAO", dao),
            security_manager=security_manager,
            user=current_user,
        )
        cmd._root = root  # noqa: SLF001
        buf = await cmd.execute()
        await event_logger.alog_with_context("chart.export", extra={"count": len(ids)})
        return Stream(
            stream_zip(buf),
            status_code=200,
            media_type="application/zip",
            headers=build_export_headers(f"{root}.zip", token=token),
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
    ) -> FavoriteStatusResponse | Response[Any]:
        ids = extract_ids(rison_params)
        charts = await dao.find_by_ids(ids) if ids else []
        if not charts:
            return Response(
                content={"message": "Not found"},
                status_code=404,
                media_type="application/json",
            )
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
        self,
        pk: int,
        dao: ChartDAOProtocol,
        current_user: UserProtocol,
        security_manager: SecurityManagerProtocol,
    ) -> dict[str, str]:
        cmd = AddFavoriteChartCommand(
            dao=cast("AsyncChartDAO", dao),
            chart_id=pk,
            user_id=current_user.id,
            security_manager=security_manager,
            user=current_user,
        )
        await cmd.execute()
        await event_logger.alog_with_context(
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
        self,
        pk: int,
        dao: ChartDAOProtocol,
        current_user: UserProtocol,
        security_manager: SecurityManagerProtocol,
    ) -> dict[str, str]:
        cmd = RemoveFavoriteChartCommand(
            dao=cast("AsyncChartDAO", dao),
            chart_id=pk,
            user_id=current_user.id,
            security_manager=security_manager,
            user=current_user,
        )
        await cmd.execute()
        await event_logger.alog_with_context(
            "chart.remove_favorite",
            object_ref=f"chart:{pk}",
            user_id=current_user.id,
        )
        return {"result": "OK"}

    @put(
        "/warm_up_cache",
        guards=[require_permission("can_warm_up_cache", "Chart")],
    )
    async def warm_up_cache(
        self,
        data: ChartCacheWarmUpRequest,
        dao: ChartDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
    ) -> dict[str, Any]:
        cmd = WarmUpChartCacheCommand(
            dao=cast("AsyncChartDAO", dao),
            chart_id=data.chart_id,
            dashboard_id=data.dashboard_id,
            extra_filters=data.extra_filters,
            security_manager=security_manager,
            current_user=current_user,
        )
        result = await cmd.execute()
        await event_logger.alog_with_context(
            "chart.warm_up_cache", object_ref=f"chart:{data.chart_id}"
        )
        return {"result": [result]}

    @post(
        "/import/",
        guards=[require_permission("can_write", "Chart")],
        media_type="application/json",
        # Returns 200, not 201 — import succeeds against an existing resource,
        # no new top-level resource is created.
        status_code=200,
    )
    async def import_chart(
        self,
        request: Request[Any, Any, Any],
        dao: ChartDAOProtocol,
        security_manager: SecurityManagerProtocol,
    ) -> dict[str, str]:
        # Read the multipart body manually (see parse_import_request): the
        # ``data: UploadFile = Body(MULTI_PART)`` injection 500'd when no file
        # field was present (Litestar StopIteration). Missing upload -> 4xx.
        (
            buf,
            _filename,
            overwrite,
            passwords_dict,
            ssh_dict,
            ssh_private_keys_dict,
            ssh_private_key_passwords_dict,
        ) = await parse_import_request(request)
        # ``security_manager`` is what makes the importer enforce
        # ``can_write`` on Database/Dataset. Without it the importer falls
        # back to ``ignore_permissions``, which is how a bundle carrying a
        # ``databases/`` entry could create a connection with an
        # attacker-controlled URI.
        cmd = ImportChartsCommand(
            contents=buf,
            dao=cast("AsyncChartDAO", dao),
            security_manager=security_manager,
            overwrite=overwrite,
            passwords=passwords_dict,
            ssh_tunnel_passwords=ssh_dict,
            ssh_tunnel_private_keys=ssh_private_keys_dict,
            ssh_tunnel_private_key_passwords=ssh_private_key_passwords_dict,
        )
        await cmd.execute()
        await event_logger.alog_with_context("chart.import")
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
        request: Request[Any, Any, Any],
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
        # Access-scoped lookup: returns 404 (not 403) so the datasource name
        # is never leaked for charts the user cannot access.
        from superset.db.filters import chart_access_filters
        from superset.models.slice import Slice

        _chart_base_filters = await chart_access_filters(security_manager, current_user)
        _chart_found = await dao.find_all(
            filters=[Slice.id == pk] + (_chart_base_filters or []),
            page=0,
            page_size=1,
        )
        chart = _chart_found[0] if _chart_found else None
        if not chart:
            raise ObjectNotFoundError("Chart", pk)

        # BL-C2: Check GLOBAL_ASYNC_QUERIES feature flag
        settings: SupersetSettings = cast(
            "SupersetSettings", getattr(state, "settings", None)
        )
        if getattr(settings, "global_async_queries", False):
            result_format = (format or "json").lower()
            result_type = (type or "full").lower()
            if result_format == "json" and result_type == "full":
                from superset.async_events.manager import (
                    build_job_metadata,
                    maybe_forward_guest_token,
                )
                from superset.tasks.async_queries import load_chart_data_into_cache

                # Both an empty/missing ``query_context`` and a JSON parse
                # failure collapse to a single 400 with the same message.
                query_context_str = getattr(chart, "query_context", None)
                form_data = None
                if query_context_str:
                    try:
                        form_data = _json.loads(query_context_str)
                    except (ValueError, TypeError):
                        form_data = None
                if form_data is None:
                    return Response(
                        content={
                            "message": (
                                "Chart has no query context saved. "
                                "Please save the chart again."
                            )
                        },
                        status_code=400,
                    )

                # Cache-first short-circuit: if this chart's data is already
                # cached, return it inline (200) and skip the async round-trip.
                ds_ref = form_data.get("datasource", {}) or {}
                ds_dict = {
                    "type": ds_ref.get("type", "table"),
                    "id": ds_ref.get("id", 0),
                }
                datasource = await ds_dao.get_datasource(ds_dict["type"], ds_dict["id"])
                if not datasource:
                    raise ObjectNotFoundError("Datasource", ds_dict["id"])
                # Enforce datasource access BEFORE dispatching the GAQ Celery
                # job — ``_try_cached_chart_data`` swallows the access error
                # in its broad ``except``, so without this a user with no
                # datasource access could trigger background compute against it.
                await security_manager.raise_for_access(
                    datasource=datasource, user=current_user
                )
                query_objects = [
                    AsyncQueryObject.from_request(q, ds_dict)
                    for q in form_data.get("queries", [])
                ]
                query_context = AsyncQueryContext(
                    datasource=datasource,
                    queries=query_objects,
                    force=form_data.get("force", False),
                    form_data=_context_form_data(form_data),
                    result_type="full",
                    result_format="json",
                )
                cached_response = await _try_cached_chart_data(
                    query_context=query_context,
                    datasource=datasource,
                    settings=settings,
                    security_manager=security_manager,
                    current_user=current_user,
                )
                if cached_response is not None:
                    return cached_response

                # Channel id MUST come from the request's ``async-token``
                # cookie — NOT a random uuid. A random channel would mean
                # results are written where no reader is listening.
                channel_id = _resolve_async_channel_id(request, settings)
                if not channel_id:
                    raise NotAuthorizedException(
                        detail="Failed to parse async query channel token"
                    )
                job_id = str(uuid.uuid4())
                job_metadata = build_job_metadata(
                    channel_id=channel_id,
                    job_id=job_id,
                    user_id=current_user.id,
                    status="pending",
                )
                # Forward the embedded guest JWT so the worker rebuilds the
                # same GuestUser (and matching RLS cache key). Only the
                # *dispatched* metadata carries the token; the 202 response
                # returns clean job_metadata.
                dispatch_metadata = await maybe_forward_guest_token(
                    job_metadata,
                    request=request,
                    settings=settings,
                    security_manager=security_manager,
                    current_user=current_user,
                )
                load_chart_data_into_cache.delay(dispatch_metadata, form_data)
                return Response(
                    content=job_metadata,
                    status_code=202,
                )

        # Both an empty/missing ``query_context`` and a JSON parse failure
        # collapse to a single 400 with the same message.
        query_context_str = getattr(chart, "query_context", None)
        qc_data = None
        if query_context_str:
            try:
                qc_data = _json.loads(query_context_str)
            except (ValueError, TypeError):
                qc_data = None
        if qc_data is None:
            return Response(
                content={
                    "message": (
                        "Chart has no query context saved. Please save the chart again."
                    )
                },
                status_code=400,
            )

        # Apply query param overrides
        if format is not None:
            qc_data["result_format"] = format
        qc_data["result_type"] = type or "full"
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
            form_data=_context_form_data(qc_data),
            # Honor the ``?type=`` override so the processor runs the matching
            # result-type branch (``query_obj.result_type or
            # query_context.result_type`` precedence).
            result_type=qc_data.get("result_type"),
            result_format=qc_data.get("result_format"),
        )
        processor = AsyncQueryContextProcessor(
            datasource=datasource,
            settings=settings,
            security_manager=security_manager,
            user=current_user,
            query_context=query_context,
        )
        # Table-like formats (?format=csv|xlsx): check ``can_csv`` permission,
        # then return a raw CSV/XLSX (or ZIP) download.
        _result_format_get = str(qc_data.get("result_format") or "json").lower()
        if _result_format_get in ("csv", "xlsx"):
            if not await security_manager.can_access(
                "can_csv", "Superset", user=current_user
            ):
                return Response(
                    content={"message": "You don't have permission to download data"},
                    status_code=403,
                )

        cmd = ChartDataCommand(query_context=query_context, processor=processor)
        result = await cmd.execute()

        if _result_format_get in ("csv", "xlsx"):
            await event_logger.alog_with_context(
                "chart.data",
                object_ref=f"chart:{pk}",
                user_id=current_user.id,
            )
            return _table_like_file_response(
                result,
                _result_format_get,
                verbose_map=getattr(query_context.datasource, "verbose_map", None),
            )

        # Extract ``form_data`` from ``chart.params`` so that
        # ``apply_client_processing`` runs when ``result_type == post_processed``
        # (pivot table / table chart email reports).
        try:
            form_data = _json.loads(chart.params) if chart.params else {}
        except (TypeError, ValueError):
            form_data = {}

        await event_logger.alog_with_context(
            "chart.data",
            object_ref=f"chart:{pk}",
            user_id=current_user.id,
        )
        return _render_chart_data_payload(
            result,
            is_guest=security_manager.is_guest_user(current_user),
            form_data=form_data,
            datasource=datasource,
        )

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
    ) -> Response[Any]:
        """POST /api/v1/chart/data — execute ad-hoc chart data query.

        Accepts either a JSON body or a ``form_data`` multipart field
        containing a JSON string (used by CSV export in Superset).

        Body is parsed manually rather than via the typed-parameter
        injection so Litestar does not attempt to JSON-decode
        ``application/x-www-form-urlencoded`` / ``multipart`` bodies as
        JSON (which raises ``invalid character (byte 5)`` on the URL-encoded
        ``form_data=...`` payload that Apache Superset's CSV export
        button submits).
        """
        import contextlib as _contextlib

        import msgspec as _msgspec

        # Litestar's typed-parameter injection cannot be used here because
        # Apache Superset's CSV-export button submits the body as
        # ``application/x-www-form-urlencoded`` with the JSON wrapped in a
        # ``form_data=`` field — Litestar would attempt to decode the URL-
        # encoded payload as JSON and fail at the first ``=`` (byte 5).
        data: ChartDataQueryContext | None = None
        json_bytes: bytes | None = None
        content_type_str = request.content_type[0] if request.content_type else ""
        is_json = "json" in content_type_str

        if is_json:
            body = await request.body()
            if body:
                json_bytes = body
        else:
            # CSV export submits regular form data — match the
            # ``request.form.get("form_data")`` branch of the original.
            with _contextlib.suppress(Exception):
                form = await request.form()
                form_data_str = form.get("form_data")
                if form_data_str:
                    json_bytes = (
                        form_data_str
                        if isinstance(form_data_str, bytes)
                        else form_data_str.encode()
                    )

        if json_bytes is None:
            # Body was not JSON-decodable and no form_data field was supplied —
            # upstream's ``request.json`` returns None and the API responds
            # with 400 "Request is not JSON" (charts/data/api.py:234).
            return Response(
                content={"message": "Request is not JSON"},
                status_code=400,
            )

        try:
            data = _msgspec.json.decode(json_bytes, type=ChartDataQueryContext)
        except _msgspec.ValidationError as ex:
            # NB: ``ValidationError`` is a subclass of ``DecodeError`` in
            # msgspec — catch it FIRST or the more general except below
            # swallows schema-mismatch errors and you lose field detail.
            # Schema mismatch (wrong field type / unknown enum value / etc.).
            # Upstream emits ``Request is incorrect: <field>: <message>`` via
            # marshmallow's ``ValidationError.normalized_messages``; mirror
            # that 400 with the underlying msgspec detail.
            return Response(
                content={"message": f"Request is incorrect: {ex}"},
                status_code=400,
            )
        except _msgspec.DecodeError:
            # Body wasn't valid JSON syntax — 400 "Request is not JSON"
            # matches upstream's ``json.JSONDecodeError`` branch.
            return Response(
                content={"message": "Request is not JSON"},
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
                from superset.async_events.manager import (
                    build_job_metadata,
                    maybe_forward_guest_token,
                )
                from superset.tasks.async_queries import load_chart_data_into_cache

                # Cache-first short-circuit: if this query's data is already
                # cached, return it inline (200) and skip the async round-trip.
                ds_ref = {"type": data.datasource.type, "id": data.datasource.id}
                datasource = await ds_dao.get_datasource(
                    data.datasource.type, data.datasource.id
                )
                if not datasource:
                    raise ObjectNotFoundError("Datasource", data.datasource.id)
                # Enforce datasource access BEFORE dispatching the GAQ Celery
                # job — ``_try_cached_chart_data`` swallows the access error
                # in its broad ``except``, so without this a user with no
                # datasource access could trigger background compute against it.
                await security_manager.raise_for_access(
                    datasource=datasource, user=current_user
                )
                query_objects = [
                    AsyncQueryObject.from_request(q, ds_ref) for q in data.queries
                ]
                query_context = AsyncQueryContext(
                    datasource=datasource,
                    queries=query_objects,
                    force=data.force,
                    form_data=_context_form_data(data),
                    result_type="full",
                    result_format="json",
                )
                cached_response = await _try_cached_chart_data(
                    query_context=query_context,
                    datasource=datasource,
                    settings=settings,
                    security_manager=security_manager,
                    current_user=current_user,
                )
                if cached_response is not None:
                    return cached_response

                # Channel id MUST come from the request's ``async-token``
                # cookie — NOT a random uuid. A random channel would mean
                # results are written where no reader is listening.
                channel_id = _resolve_async_channel_id(request, settings)
                if not channel_id:
                    raise NotAuthorizedException(
                        detail="Failed to parse async query channel token"
                    )
                job_id = str(uuid.uuid4())
                job_metadata = build_job_metadata(
                    channel_id=channel_id,
                    job_id=job_id,
                    user_id=current_user.id,
                    status="pending",
                )
                # Forward the embedded guest JWT so the worker rebuilds the
                # same GuestUser (and matching RLS cache key). Only the
                # *dispatched* metadata carries the token; the 202 response
                # returns clean job_metadata.
                form_data = _msgspec.to_builtins(data)
                dispatch_metadata = await maybe_forward_guest_token(
                    job_metadata,
                    request=request,
                    settings=settings,
                    security_manager=security_manager,
                    current_user=current_user,
                )
                load_chart_data_into_cache.delay(dispatch_metadata, form_data)
                return Response(
                    content=job_metadata,
                    status_code=202,
                )

        datasource = await ds_dao.get_datasource(
            data.datasource.type, data.datasource.id
        )
        if not datasource:
            raise ObjectNotFoundError("Datasource", data.datasource.id)

        # Enforce datasource access BEFORE dispatching on result_type. The
        # ``result_type=query`` SQL-preview path returns early without reaching
        # ``ChartDataCommand.execute()``, so without this gate a user with no
        # datasource access could read the generated SQL (incl. the physical
        # table name) of any datasource.
        await security_manager.raise_for_access(
            datasource=datasource, user=current_user
        )

        # --- P1-5: result_type dispatch -------------------------------------------

        ds_ref = {"type": data.datasource.type, "id": data.datasource.id}

        # RLS clauses for the SQL-preview (``result_type=query``) path — the
        # generated SQL is shown to the user, so it must respect Row Level
        # Security. ``compose_rls_where_clauses`` returns ``list[ClauseElement]``
        # which ``_build_sql`` dialect-compiles for proper quoting/translation.
        # (``samples`` is NOT handled here — it falls through to the processor's
        # faithful ``_get_samples`` → ``get_df_payload`` → ``_get_query_result``,
        # which applies the same RLS as the ``full`` path.)
        from sqlalchemy.sql.elements import ClauseElement

        from superset.utils.rls import compose_rls_where_clauses

        rls_clauses_for_preview: list[ClauseElement] = []
        if result_type == "query":
            rls_clauses_for_preview = await compose_rls_where_clauses(
                datasource,
                user=current_user,
                security_manager=security_manager,
            )

        if result_type == "query":
            # Return generated SQL without executing the query
            query_results: list[dict[str, Any]] = []
            for q_schema in data.queries:
                qobj = AsyncQueryObject.from_request(q_schema, ds_ref)
                query_dict = qobj.to_dict()
                sql, _from_dttm, _to_dttm = datasource._build_sql(
                    query_dict, rls_filters=rls_clauses_for_preview
                )
                query_results.append(
                    {
                        "query": sql,
                        "status": "success",
                        "language": "sql",
                    }
                )
            await event_logger.alog_with_context("chart.data_post")
            return Response(
                content={"result": query_results},
                media_type="application/json",
            )

        # ``result_type=samples`` falls through to the default JSON path, where
        # the processor's ``_get_samples`` clears metrics/orderby/post-processing/
        # time-window, selects every datasource column, and runs the full
        # ``get_df_payload`` pipeline with RLS applied via ``_get_query_result``.

        # --- result_format: csv / xlsx (early return) ----------------------------

        if result_format in ("csv", "xlsx"):
            # Check can_csv permission
            if not await security_manager.can_access(
                "can_csv", "Superset", user=current_user
            ):
                return Response(
                    content={"message": "You don't have permission to download data"},
                    status_code=403,
                )

            query_objects = [
                AsyncQueryObject.from_request(q, ds_ref) for q in data.queries
            ]
            query_context = AsyncQueryContext(
                datasource=datasource,
                queries=query_objects,
                force=data.force,
                form_data=_context_form_data(data),
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
            return _table_like_file_response(
                result,
                result_format,
                verbose_map=getattr(datasource, "verbose_map", None),
            )

        # When result_type is "post_processed", execute the query (full path)
        # then apply pivot/table client-side transforms BEFORE the NaN/Decimal
        # cleanup pass.  Used by Pivot Table v2 and Table chart email reports.
        if result_type == "post_processed":
            _pp_qobjs = [AsyncQueryObject.from_request(q, ds_ref) for q in data.queries]
            _pp_qctx = AsyncQueryContext(
                datasource=datasource,
                queries=_pp_qobjs,
                force=data.force,
                form_data=_context_form_data(data),
                result_format=result_format,
            )
            _pp_proc = AsyncQueryContextProcessor(
                datasource=datasource,
                settings=settings,
                security_manager=security_manager,
                user=current_user,
                query_context=_pp_qctx,
            )
            result = await ChartDataCommand(
                query_context=_pp_qctx, processor=_pp_proc
            ).execute()
            # NB: post-processing (apply_client_processing) runs AFTER the
            # shared df→data materialization + NaN cleanup below — it needs
            # ``query["data"]`` populated and reads ``viz_type`` from the
            # request's nested ``form_data`` (NOT the whole query-context).

        # --- Default JSON path (result_type: full / results / columns / etc.) ----
        else:
            query_objects = [
                AsyncQueryObject.from_request(q, ds_ref) for q in data.queries
            ]
            query_context = AsyncQueryContext(
                datasource=datasource,
                queries=query_objects,
                force=data.force,
                form_data=_context_form_data(data),
                result_format=result_format,
                # Propagate result_type so the processor runs the matching
                # branch (``results`` skips post-processing; columns/timegrains
                # return datasource metadata).
                result_type=result_type,
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
        from datetime import date as _date_t, datetime as _datetime_t
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
        # milliseconds. Frontend chart components (Table, TimeSeries, …)
        # expect numeric timestamps so they can apply ``smart_date``
        # formatting driven by ``time_grain_sqla`` — ISO strings break this.
        from datetime import date as _date_t, datetime as _datetime_t
        from decimal import Decimal

        from superset.utils.json import datetime_to_epoch

        epoch_date = _datetime_t(1970, 1, 1).date()

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
                            # matches the original SQLAlchemy path
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
                            row[key] = (val - epoch_date).total_seconds() * 1000

        # Ensure indexnames is present in each query result
        for q in result.get("queries", []):
            if isinstance(q, dict):
                q["indexnames"] = list(range(len(q.get("data", []))))

        # Client-side post-processing (pivot_table_v2 / table): when
        # ``result_type=post_processed`` the materialized ``query["data"]`` is
        # pivoted/reshaped via ``apply_client_processing``. ``viz_type`` lives
        # in the request's NESTED ``form_data`` sub-field — passing the whole
        # query-context leaves ``viz_type`` unset → the processor short-circuits
        # → pivot/table email reports receive RAW unpivoted rows. Must run AFTER
        # df→data + NaN cleanup; the dict-of-dicts output is then re-normalized
        # for msgspec (which rejects NaN).
        if result_type == "post_processed":
            from superset.charts.post_processing import apply_client_processing

            _pp_form_data = data.form_data or {}
            apply_client_processing(
                result, form_data=_pp_form_data, datasource=datasource
            )
            # ``processed_df.to_dict()`` yields ``{col: {idx: val}}``; clean
            # NaN/Inf/numpy/Decimal/datetime in those nested values so the
            # msgspec encoder (which can't serialize NaN) doesn't 500.
            for q in result.get("queries", []):
                if isinstance(q, dict) and isinstance(q.get("data"), dict):
                    for _col, _col_map in q["data"].items():
                        if isinstance(_col_map, dict):
                            for _k, _val in list(_col_map.items()):
                                _col_map[_k] = _normalize_post_processed_value(
                                    _val, epoch_date
                                )

        # result_type=results truncation: 5 keys only, for non-failed queries.
        if result_type == "results":
            result["queries"] = [
                _truncate_results_query(q)
                if isinstance(q, dict) and q.get("status") != "failed"
                else q
                for q in result.get("queries", [])
            ]

        await event_logger.alog_with_context("chart.data_post")
        # Frontend expects {"result": [...]} not {"queries": [...]}
        response_payload = {"result": result.get("queries", [])}

        # Walk the entire response tree and convert ANY datetime/date value
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
    async def data_from_cache(
        self,
        cache_key: str,
        ds_dao: DatasourceDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
        state: State,
    ) -> Response[Any]:
        """GET /api/v1/chart/data/{cache_key} — return data for a cached query.

        This is the ``result_url`` target of the GLOBAL_ASYNC_QUERIES flow:
        once ``load_chart_data_into_cache`` finishes, the worker broadcasts a
        ``result_url`` of ``/api/v1/chart/data/<cache_key>`` and the frontend
        (``asyncEvent.ts::fetchCachedData``) GETs it. Flow:

        1. Load the cached query-context *form* (404 on miss).
        2. Rebuild the query context from the form.
        3. Build the command and run it. The per-query RESULT is already cached
           (the worker computed and stored it), so this is a cache hit — no
           re-execution against the warehouse.
        4. Render the chart-data payload (same shape as ``POST /data``).
        """
        from superset.extensions import cache_manager
        from superset.tasks.async_queries import _create_query_context_from_form

        settings: SupersetSettings = cast(
            "SupersetSettings", getattr(state, "settings", None)
        )

        form = await load_cached_query_context_form(cache_manager, cache_key)
        if form is None:
            raise ObjectNotFoundError("ChartCachedData", cache_key)

        # Set form_data context as a fallback for async queries with jinja
        # context (``_form_data_ctx`` ContextVar is the equivalent of the
        # legacy ``g.form_data`` global in Flask).
        if isinstance(form, dict):
            from superset.jinja_context import set_form_data

            set_form_data(form)

        query_context = _create_query_context_from_form(form)

        ds_ref = query_context.datasource
        ds_id: int | None = None
        ds_type: str = "table"
        if isinstance(ds_ref, dict):
            ds_id = ds_ref.get("id")
            ds_type = ds_ref.get("type") or "table"
        elif isinstance(ds_ref, str) and "__" in ds_ref:
            parts = ds_ref.split("__")
            try:
                ds_id = int(parts[0])
                ds_type = parts[1]
            except (ValueError, IndexError):
                ds_id = None
        if ds_id is None:
            raise ObjectNotFoundError("ChartCachedData", cache_key)

        datasource = await ds_dao.get_datasource(ds_type, ds_id)
        if not datasource:
            raise ObjectNotFoundError("Datasource", ds_id)
        query_context.datasource = datasource

        # Build the processor *with* the cache manager so the per-query
        # RESULT cache is hit on ``command.run(force_cached=True)``.
        processor = AsyncQueryContextProcessor(
            datasource=datasource,
            settings=settings,
            security_manager=security_manager,
            user=current_user,
            cache_manager=cache_manager,
            query_context=query_context,
        )
        command = ChartDataCommand(query_context=query_context, processor=processor)
        await command.validate()
        # ``force_cached=True`` reads the result the worker already stored;
        # without it the processor would re-execute against the warehouse.
        result = await command.run(force_cached=True)

        await event_logger.alog_with_context(
            "chart.data_from_cache", object_ref=f"cache:{cache_key}"
        )
        return _render_chart_data_payload(
            result,
            is_guest=security_manager.is_guest_user(current_user),
        )
