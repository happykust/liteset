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
"""msgspec Structs for the Chart API — replaces Marshmallow schemas."""

# ruff: noqa: N815  — camelCase field names required for JSON API contract parity
from __future__ import annotations

import json as _json
from typing import Annotated, Any, Literal

import msgspec
from msgspec import Meta

from superset.schemas.base import ApiListResponse, ApiResponse

# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class ChartPostSchema(msgspec.Struct):
    """POST /api/v1/chart/"""

    slice_name: Annotated[str, Meta(min_length=1)]
    viz_type: str
    datasource_id: int
    datasource_type: Literal["table", "query", "saved_query", "dataset", "view"]
    params: str | None = None
    query_context: str | None = None
    query_context_generation: bool | None = None
    cache_timeout: int | None = None
    description: str | None = None
    certified_by: str | None = None
    certification_details: str | None = None
    is_managed_externally: bool = False
    external_url: str | None = None
    tags: list[int] | None = None
    owners: list[int] | None = None
    dashboards: list[int] | None = None
    datasource_name: str | None = None
    uuid: str | None = None

    def __post_init__(self) -> None:
        for attr in ("params", "query_context"):
            value = getattr(self, attr)
            if value is not None:
                try:
                    _json.loads(value)
                except (ValueError, TypeError) as exc:
                    raise msgspec.ValidationError(
                        f"'{attr}' must be a valid JSON string"
                    ) from exc


class ChartPutSchema(msgspec.Struct):
    """PUT /api/v1/chart/<pk>"""

    slice_name: str | None | msgspec.UnsetType = msgspec.UNSET
    viz_type: str | None | msgspec.UnsetType = msgspec.UNSET
    datasource_id: int | None | msgspec.UnsetType = msgspec.UNSET
    datasource_type: (
        Literal["table", "query", "saved_query", "dataset", "view"]
        | None
        | msgspec.UnsetType
    ) = msgspec.UNSET
    params: str | None | msgspec.UnsetType = msgspec.UNSET
    query_context: str | None | msgspec.UnsetType = msgspec.UNSET
    query_context_generation: bool | None | msgspec.UnsetType = msgspec.UNSET
    cache_timeout: int | None | msgspec.UnsetType = msgspec.UNSET
    description: str | None | msgspec.UnsetType = msgspec.UNSET
    certified_by: str | None | msgspec.UnsetType = msgspec.UNSET
    certification_details: str | None | msgspec.UnsetType = msgspec.UNSET
    is_managed_externally: bool | None | msgspec.UnsetType = msgspec.UNSET
    external_url: str | None | msgspec.UnsetType = msgspec.UNSET
    tags: list[int] | None | msgspec.UnsetType = msgspec.UNSET
    owners: list[int] | None | msgspec.UnsetType = msgspec.UNSET
    dashboards: list[int] | None | msgspec.UnsetType = msgspec.UNSET

    def __post_init__(self) -> None:
        for attr in ("params", "query_context"):
            value = getattr(self, attr)
            if value is not None and not isinstance(value, msgspec.UnsetType):
                try:
                    _json.loads(value)
                except (ValueError, TypeError) as exc:
                    raise msgspec.ValidationError(
                        f"'{attr}' must be a valid JSON string"
                    ) from exc


# ---------------------------------------------------------------------------
# Query-parameter schemas (Rison / URL params)
# ---------------------------------------------------------------------------


class ChartExportParams(msgspec.Struct):
    """GET /api/v1/chart/export/?q=(ids:!(...))"""

    ids: list[int] = []


class FavoriteStatusParams(msgspec.Struct):
    """GET /api/v1/chart/favorite_status/?q=(...)"""

    ids: list[int] = []


class BulkDeleteParams(msgspec.Struct):
    """DELETE /api/v1/chart/?q=(...)"""

    ids: list[int] = []


class ChartCacheWarmUpRequest(msgspec.Struct):
    """PUT /api/v1/chart/warm_up_cache"""

    chart_id: int
    dashboard_id: int | None = None
    extra_filters: str | None = None


# ---------------------------------------------------------------------------
# Chart data query schemas
# ---------------------------------------------------------------------------


class ChartDataDatasource(msgspec.Struct):
    """Datasource reference in a data request."""

    id: int
    type: Literal["table", "dataset", "query", "saved_query", "view"] = "table"


FilterOperator = Literal[
    "==",
    "!=",
    ">",
    "<",
    ">=",
    "<=",
    "LIKE",
    "NOT LIKE",
    "ILIKE",
    "IS NULL",
    "IS NOT NULL",
    "IN",
    "NOT IN",
    "IS TRUE",
    "IS FALSE",
    "TEMPORAL_RANGE",
]


class ChartDataFilter(msgspec.Struct):
    """Filter within a query object."""

    col: str | dict[str, Any]
    op: FilterOperator
    val: Any = None
    grain: str | None = None
    isExtra: bool = False


class ChartDataExtras(msgspec.Struct):
    """Extras within a query object."""

    time_grain_sqla: str | None = None
    having: str = ""
    where: str = ""
    relative_start: Literal["today", "now"] | None = None
    relative_end: Literal["today", "now"] | None = None
    time_range_endpoints: list[str] | None = None
    instant_time_comparison_range: str | None = None


class ChartDataAdhocMetric(msgspec.Struct):
    """Adhoc metric definition."""

    expressionType: str
    label: str | None = None
    column: dict[str, Any] | None = None
    sqlExpression: str | None = None
    aggregate: str | None = None
    hasCustomLabel: bool = False
    optionName: str | None = None
    datasourceWarning: bool = False
    timeGrain: str | None = None


class ChartDataColumn(msgspec.Struct):
    """Column definition in a query object."""

    columnType: str | None = None
    expressionType: str | None = None
    label: str | None = None
    sqlExpression: str | None = None
    timeGrain: str | None = None


class AnnotationLayer(msgspec.Struct):
    """Annotation config within a query object."""

    name: str
    annotationType: str | None = None
    color: str | None = None
    descriptionColumns: list[str] = []
    hideLine: bool = False
    intervalEndColumn: str | None = None
    opacity: str | None = None
    overrides: dict[str, Any] = {}
    show: bool = True
    showMarkers: bool = False
    sourceType: str | None = None
    style: str | None = None
    titleColumn: str | None = None
    value: Any = None
    width: float | None = None
    timeColumn: str | None = None
    showLabel: bool | None = None


# --- Post-processing option structs ---


class AggregateOptions(msgspec.Struct):
    groupby: list[str] = []
    aggregates: dict[str, dict[str, Any]] = {}


class BoxplotOptions(msgspec.Struct):
    groupby: list[str] = []
    metrics: list[str] = []
    whisker_type: str = "tukey"
    percentiles: tuple[float, ...] = (1, 5, 25, 50, 75, 95, 99)


class CompareOptions(msgspec.Struct):
    source_columns: list[str] = []
    compare_columns: list[str] = []
    compare_type: str | None = None
    drop_original_columns: bool = False


class ContributionOptions(msgspec.Struct):
    columns: list[str] = []
    orientation: str = "column"
    rename_columns: list[str] | None = None


class CumOptions(msgspec.Struct):
    columns: dict[str, str] = {}
    operator: str = "sum"


class DiffOptions(msgspec.Struct):
    columns: dict[str, str] = {}
    periods: int = 1
    axis: int = 0


class FlattenOptions(msgspec.Struct):
    columns: list[str] | None = None
    reset_index: bool = True


class GeodeticParseOptions(msgspec.Struct):
    geodetic: str = ""
    latitude: str = "latitude"
    longitude: str = "longitude"
    altitude: str | None = None


class GeohashDecodeOptions(msgspec.Struct):
    geohash: str = ""
    latitude: str = "latitude"
    longitude: str = "longitude"


class GeohashEncodeOptions(msgspec.Struct):
    latitude: str = "latitude"
    longitude: str = "longitude"
    geohash: str = "geohash"


class HistogramOptions(msgspec.Struct):
    column: str = ""
    groupby: list[str] = []
    bins: int = 5
    cumulative: bool = False
    normalize: bool = False


class PivotOptions(msgspec.Struct):
    index: list[str] = []
    columns: list[str] = []
    aggregates: dict[str, dict[str, Any]] = {}
    marginal_distributions: bool = False
    marginal_distribution_name: str | None = None
    flatten_columns: bool = True
    reset_index: bool = True
    column_fill_value: str | None = None
    value_fill_value: int | float | None = None
    drop_missing_columns: bool = True
    combine_value_with_metric: bool = False
    metric_fill_value: int | float | None = None


class ProphetOptions(msgspec.Struct):
    time_grain: str = "P1D"
    periods: int = 0
    confidence_interval: float = 0.8
    yearly_seasonality: bool | str = "auto"
    weekly_seasonality: bool | str = "auto"
    daily_seasonality: bool | str = "auto"
    monthly_seasonality: bool | str = "auto"


class RankOptions(msgspec.Struct):
    metric: str = ""
    group_by: list[str] | None = None


class RenameOptions(msgspec.Struct):
    columns: dict[str, str] = {}
    level: int | None = None
    inplace: bool = True


class ResampleOptions(msgspec.Struct):
    method: str = "asfreq"
    rule: str = "1D"
    fill_value: int | float | None = None


class RollingOptions(msgspec.Struct):
    columns: dict[str, str] = {}
    rolling_type: str = "mean"
    window: int = 1
    min_periods: int = 0
    center: bool = False
    win_type: str | None = None
    rolling_type_options: dict[str, Any] = {}


class SelectOptions(msgspec.Struct):
    columns: list[str] | None = None
    exclude: list[str] | None = None
    rename: list[dict[str, str]] | None = None


class SortOptions(msgspec.Struct):
    columns: dict[str, bool] = {}
    is_sort_index: bool = False
    aggregates: dict[str, dict[str, Any]] = {}


class ChartDataPostProcessingOp(msgspec.Struct):
    """Post-processing operation within a query object."""

    operation: str
    options: dict[str, Any] = {}


class ChartDataQueryObject(msgspec.Struct):
    """Query object within a ChartData request."""

    columns: list[str | dict[str, Any]] = []
    metrics: list[str | ChartDataAdhocMetric] = []
    orderby: list[list[Any]] = []
    filters: list[ChartDataFilter] = []
    extras: ChartDataExtras | None = None
    time_range: str | None = None
    time_shift: str | None = None
    granularity: str | None = None
    granularity_sqla: str | None = None
    row_limit: int | None = None
    row_offset: int = 0
    order_desc: bool = True
    url_params: dict[str, str] = {}
    custom_params: dict[str, Any] = {}
    custom_form_data: dict[str, Any] = {}
    is_timeseries: bool = False
    timeseries_limit: int = 0
    timeseries_limit_metric: str | ChartDataAdhocMetric | None = None
    series_columns: list[str] = []
    series_limit: int = 0
    series_limit_metric: str | ChartDataAdhocMetric | None = None
    having: str = ""
    having_filters: list[dict[str, Any]] = []
    where: str = ""
    result_type: str | None = None
    time_offsets: list[str] = []
    annotation_layers: list[AnnotationLayer] = []
    post_processing: list[ChartDataPostProcessingOp] = []
    applied_time_extras: dict[str, str] = {}
    groupby: list[Any] | None = None  # deprecated, use columns
    apply_fetch_values_predicate: bool = False
    is_rowcount: bool = False
    group_others_when_limit_reached: bool = False


class ChartDataQueryContext(msgspec.Struct):
    """Full body for POST /api/v1/chart/data"""

    datasource: ChartDataDatasource
    queries: list[ChartDataQueryObject]
    result_type: str | None = None
    result_format: str | None = None
    force: bool = False
    form_data: dict[str, Any] = {}
    custom_cache_timeout: int | None = None


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


# Reuse base response schemas — avoid duplication
ChartGetResponse = ApiResponse
ChartListResponse = ApiListResponse


class ChartDataResponseResult(msgspec.Struct):
    """Single query result within a ChartDataResponse."""

    cache_key: str | None = None
    cached_dttm: str | None = None
    cache_timeout: int | None = None
    error: str | None = None
    is_cached: bool = False
    query: str = ""
    status: str = "success"
    stacktrace: str | None = None
    rowcount: int = 0
    from_dttm: str | None = None
    to_dttm: str | None = None
    data: list[dict[str, Any]] = []
    colnames: list[str] = []
    coltypes: list[int] = []
    applied_filters: list[dict[str, Any]] = []
    rejected_filters: list[dict[str, Any]] = []
    applied_template_filters: list[dict[str, Any]] | None = None
    annotation_data: dict[str, Any] = {}


class ChartDataResponse(msgspec.Struct):
    """Response for POST /api/v1/chart/data"""

    result: list[ChartDataResponseResult] = []


class ChartCacheScreenshotResponse(msgspec.Struct):
    """Response for GET /api/v1/chart/<pk>/cache_screenshot/"""

    cache_key: str
    chart_url: str
    image_url: str
    task_status: str | None = None
    task_updated_at: str | None = None


class ImportV1Chart(msgspec.Struct):
    """Import payload for a chart."""

    slice_name: str
    viz_type: str
    uuid: str
    version: str
    dataset_uuid: str
    params: dict[str, Any] = {}
    query_context: str | None = None
    cache_timeout: int | None = None
    datasource_type: str = "table"
    description: str | None = None
    certified_by: str | None = None
    certification_details: str | None = None
    is_managed_externally: bool = False
    external_url: str | None = None


class ChartCacheWarmUpResponseSingle(msgspec.Struct):
    """Single result in cache warm-up response."""

    chart_id: int
    viz_error: str | None = None
    viz_status: str | None = None
