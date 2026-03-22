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
"""msgspec Structs for the Dataset API — replaces Marshmallow schemas."""

# ruff: noqa: N815  — camelCase field names required for JSON API contract parity
from __future__ import annotations

from typing import Any

import msgspec

from liteset.schemas.base import ApiListResponse, ApiResponse

# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class DatasetPostBody(msgspec.Struct):
    table_name: str
    database: int
    schema_name: str | None = None
    sql: str | None = None
    owners: list[int] | None = None
    is_managed_externally: bool = False
    external_url: str | None = None
    normalize_columns: bool = False
    always_filter_main_dttm: bool = False
    tags: list[dict[str, Any]] | None = None
    catalog: str | None = None
    template_params: str | None = None
    uuid: str | None = None


class DatasetColumnsPut(msgspec.Struct):
    column_name: str | None = None
    type: str | None = None
    is_dttm: bool | None = None
    is_active: bool | None = None
    groupby: bool | None = None
    filterable: bool | None = None
    description: str | None = None
    expression: str | None = None
    verbose_name: str | None = None
    python_date_format: str | None = None
    extra: str | None = None
    id: int | None = None
    uuid: str | None = None
    advanced_data_type: str | None = None


class DatasetMetricCurrency(msgspec.Struct):
    symbol: str
    symbolPosition: str = "prefix"


class DatasetMetricsPut(msgspec.Struct):
    metric_name: str | None = None
    expression: str | None = None
    metric_type: str | None = None
    verbose_name: str | None = None
    description: str | None = None
    d3format: str | None = None
    currency: DatasetMetricCurrency | None = None
    warning_text: str | None = None
    extra: str | None = None
    id: int | None = None
    uuid: str | None = None


class DatasetPutBody(msgspec.Struct):
    table_name: str | None | msgspec.UnsetType = msgspec.UNSET
    database_id: int | None | msgspec.UnsetType = msgspec.UNSET
    sql: str | None | msgspec.UnsetType = msgspec.UNSET
    schema_name: str | None | msgspec.UnsetType = msgspec.UNSET
    description: str | None | msgspec.UnsetType = msgspec.UNSET
    main_dttm_col: str | None | msgspec.UnsetType = msgspec.UNSET
    offset: int | None | msgspec.UnsetType = msgspec.UNSET
    default_endpoint: str | None | msgspec.UnsetType = msgspec.UNSET
    cache_timeout: int | None | msgspec.UnsetType = msgspec.UNSET
    is_sqllab_view: bool | None | msgspec.UnsetType = msgspec.UNSET
    template_params: str | None | msgspec.UnsetType = msgspec.UNSET
    owners: list[int] | None | msgspec.UnsetType = msgspec.UNSET
    columns: list[DatasetColumnsPut] | None | msgspec.UnsetType = msgspec.UNSET
    metrics: list[DatasetMetricsPut] | None | msgspec.UnsetType = msgspec.UNSET
    extra: str | None | msgspec.UnsetType = msgspec.UNSET
    is_managed_externally: bool | None | msgspec.UnsetType = msgspec.UNSET
    external_url: str | None | msgspec.UnsetType = msgspec.UNSET
    normalize_columns: bool | None | msgspec.UnsetType = msgspec.UNSET
    always_filter_main_dttm: bool | None | msgspec.UnsetType = msgspec.UNSET
    tags: list[dict[str, Any]] | None | msgspec.UnsetType = msgspec.UNSET
    filter_select_enabled: bool | None | msgspec.UnsetType = msgspec.UNSET
    fetch_values_predicate: str | None | msgspec.UnsetType = msgspec.UNSET
    catalog: str | None | msgspec.UnsetType = msgspec.UNSET
    uuid: str | None | msgspec.UnsetType = msgspec.UNSET


class DatasetDuplicateBody(msgspec.Struct):
    base_model_id: int
    table_name: str


class GetOrCreateDatasetBody(msgspec.Struct):
    table_name: str
    database: int
    schema_name: str | None = None
    template_params: str | None = None
    normalize_columns: bool = False
    always_filter_main_dttm: bool = False


# ---------------------------------------------------------------------------
# Import / Export
# ---------------------------------------------------------------------------


class ImportV1Column(msgspec.Struct):
    column_name: str
    is_dttm: bool = False
    is_active: bool | None = None
    type: str | None = None
    groupby: bool = True
    filterable: bool = True
    expression: str | None = None
    verbose_name: str | None = None
    description: str | None = None
    python_date_format: str | None = None
    extra: dict[str, Any] = {}
    uuid: str | None = None
    advanced_data_type: str | None = None


class ImportV1Metric(msgspec.Struct):
    metric_name: str
    expression: str = ""
    metric_type: str | None = None
    verbose_name: str | None = None
    description: str | None = None
    d3format: str | None = None
    currency: str | None = None
    warning_text: str | None = None
    extra: str | None = None
    uuid: str | None = None


class ImportV1Dataset(msgspec.Struct):
    table_name: str
    uuid: str
    version: str
    database_uuid: str
    main_dttm_col: str | None = None
    description: str | None = None
    default_endpoint: str | None = None
    offset: int = 0
    cache_timeout: int | None = None
    schema_name: str | None = None
    sql: str | None = None
    params: dict[str, Any] = {}
    template_params: dict[str, Any] | None = None
    filter_select_enabled: bool = True
    fetch_values_predicate: str | None = None
    extra: str | dict[str, Any] | None = None
    columns: list[ImportV1Column] = []
    metrics: list[ImportV1Metric] = []
    is_managed_externally: bool = False


# ---------------------------------------------------------------------------
# Cache warm-up
# ---------------------------------------------------------------------------


class DatasetCacheWarmUpRequest(msgspec.Struct):
    db_name: str
    table_name: str
    dashboard_id: int | None = None
    extra_filters: str | None = None


# ---------------------------------------------------------------------------
# Drill / Response
# ---------------------------------------------------------------------------


class DatasetDrillInfo(msgspec.Struct):
    column_name: str
    groupby: bool = True
    is_dttm: bool = False
    type: str = ""


class DatasetDrillResponse(msgspec.Struct):
    columns: list[DatasetDrillInfo] = []


DatasetGetResponse = ApiResponse
DatasetListResponse = ApiListResponse
