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

from __future__ import annotations

from typing import Any

import msgspec

from superset.schemas.base import ApiListResponse, ApiResponse, ModelStruct, UserRef

# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


def _validate_uuid_field(field_name: str, value: Any) -> None:
    """Reject malformed UUID strings before the SA UUID column does.

    Mirrors upstream's ``fields.UUID(allow_none=True)`` — marshmallow
    parses the string into ``UUID`` up-front and 400s on failure.
    Without this gate a bad UUID propagates to ``process_bind_param``
    which calls ``uuid.UUID(value)`` and crashes with
    ``StatementError: badly formed hexadecimal UUID string`` (500).
    """
    if value is None or value is msgspec.UNSET:
        return
    if not isinstance(value, str):
        return
    if value == "":
        return
    import uuid as _uuid

    try:
        _uuid.UUID(value)
    except (ValueError, TypeError) as ex:
        raise msgspec.ValidationError(f"{field_name} is not a valid UUID: {ex}") from ex


class DatasetPostSchema(msgspec.Struct):
    table_name: str
    database: int
    schema: str | None = None
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

    def __post_init__(self) -> None:
        _validate_uuid_field("uuid", self.uuid)


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

    def __post_init__(self) -> None:
        # 1:1 with original DatasetColumnsPutSchema.column_name:
        # fields.String(required=True, validate=Length(1, 255))
        # DatasetPutSchema.handle_error (superset_old/datasets/schemas.py:182-191)
        # converts ALL schema validation errors into SupersetMarshmallowValidationError
        # (status=422), which is caught by superset_exception_handler and returned as
        # HTTP 422. Raising msgspec.ValidationError here instead would be caught by
        # Litestar's validation_error_handler and mapped to 400 (not 422), so we raise
        # SupersetMarshmallowValidationError directly to preserve the original
        # status code.
        if not self.column_name or len(self.column_name) > 255:
            from superset.exceptions import SupersetMarshmallowValidationError

            raise SupersetMarshmallowValidationError(
                messages={
                    "column_name": ["column_name must be between 1 and 255 characters"]
                }
            )


class DatasetMetricCurrency(msgspec.Struct, rename="camel"):
    symbol: str
    symbol_position: str = "prefix"


class DatasetMetricsPut(msgspec.Struct):
    metric_name: str
    expression: str
    metric_type: str | None = None
    verbose_name: str | None = None
    description: str | None = None
    d3format: str | None = None
    currency: DatasetMetricCurrency | None = None
    warning_text: str | None = None
    extra: str | None = None
    id: int | None = None
    uuid: str | None = None


class DatasetPutSchema(msgspec.Struct):
    table_name: str | None | msgspec.UnsetType = msgspec.UNSET
    database_id: int | None | msgspec.UnsetType = msgspec.UNSET
    sql: str | None | msgspec.UnsetType = msgspec.UNSET
    schema: str | None | msgspec.UnsetType = msgspec.UNSET
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
    folders: list[Any] | None | msgspec.UnsetType = msgspec.UNSET

    def __post_init__(self) -> None:
        _validate_uuid_field("uuid", self.uuid)


class DatasetDuplicateSchema(msgspec.Struct):
    base_model_id: int
    table_name: str


class GetOrCreateDatasetSchema(msgspec.Struct):
    table_name: str
    database_id: int
    catalog: str | None = None
    schema: str | None = None
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
    currency: dict[str, Any] | None = None
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
    schema: str | None = None
    sql: str | None = None
    params: dict[str, Any] = {}
    template_params: dict[str, Any] | None = None
    filter_select_enabled: bool = True
    fetch_values_predicate: str | None = None
    extra: str | dict[str, Any] | None = None
    columns: list[ImportV1Column] = []
    metrics: list[ImportV1Metric] = []
    is_managed_externally: bool = False
    catalog: str | None = None
    external_url: str | None = None
    normalize_columns: bool = False
    always_filter_main_dttm: bool = False
    folders: list[Any] = []


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


# ---------------------------------------------------------------------------
# Detail result Structs for GET /{pk}
# ---------------------------------------------------------------------------


class ColumnRef(ModelStruct):
    """Column reference in a dataset detail response."""

    id: int | None = None
    column_name: str = ""
    verbose_name: str | None = None
    description: str | None = None
    expression: str | None = None
    type: str | None = None
    type_generic: int | None = None
    python_date_format: str | None = None
    is_dttm: bool = False
    is_active: bool = True
    groupby: bool = True
    filterable: bool = True
    uuid: str | None = None
    advanced_data_type: str | None = None
    extra: str | None = None
    changed_on: str | None = None
    created_on: str | None = None
    # Populated only on GET with ?include_rendered_sql=true (Jinja-rendered
    # ``expression``) — 1:1 with upstream ``render_dataset_fields``.
    rendered_expression: str | None = None


class MetricRef(ModelStruct):
    """Metric reference in a dataset detail response."""

    id: int | None = None
    metric_name: str = ""
    verbose_name: str | None = None
    description: str | None = None
    expression: str = ""
    metric_type: str | None = None
    d3format: str | None = None
    currency: Any = None
    warning_text: str | None = None
    extra: str | None = None
    uuid: str | None = None
    changed_on: str | None = None
    created_on: str | None = None
    # Populated only on GET with ?include_rendered_sql=true (Jinja-rendered
    # ``expression``) — 1:1 with upstream ``render_dataset_fields``.
    rendered_expression: str | None = None


class DatabaseRef(ModelStruct):
    """Database reference embedded in a dataset detail response."""

    id: int
    database_name: str = ""
    uuid: str | None = None
    backend: str | None = None
    allow_multi_catalog: bool = False


class DatasetDetailResult(ModelStruct):
    """Full dataset detail returned by GET /api/v1/dataset/{pk}."""

    # --- identifiers ---
    id: int | None = None
    table_name: str = ""
    datasource_name: str = ""
    name: str = ""
    uid: str = ""
    uuid: str | None = None
    url: str = ""
    # --- core fields ---
    schema: str | None = None
    catalog: str | None = None
    sql: str | None = None
    description: str | None = None
    cache_timeout: int | None = None
    main_dttm_col: str | None = None
    template_params: str | None = None
    datasource_type: str = "table"
    kind: str | None = None
    filter_select_enabled: bool = True
    fetch_values_predicate: str | None = None
    normalize_columns: bool = False
    always_filter_main_dttm: bool = False
    offset: int = 0
    default_endpoint: str | None = None
    is_sqllab_view: bool = False
    extra: str | None = None
    is_managed_externally: bool = False
    folders: Any | msgspec.UnsetType = None
    select_star: str | None = None
    # --- audit timestamps ---
    created_on: str | None = None
    changed_on: str | None = None
    changed_on_humanized: str | None = None
    created_on_humanized: str | None = None
    # --- audit users ---
    created_by: UserRef | None = None
    changed_by: UserRef | None = None
    # --- database ---
    database: DatabaseRef | None = None
    # --- relationships ---
    owners: list[UserRef] = []
    columns: list[ColumnRef] = []
    metrics: list[MetricRef] = []
    # --- computed fields ---
    granularity_sqla: list[str] = []
    time_grain_sqla: list[Any] = []
    order_by_choices: list[list[Any]] = []
    verbose_map: dict[str, str | None] = {}
    column_formats: dict[str, Any] = {}
    # --- optional (include_rendered_sql) ---
    rendered_sql: str | None = None

    # -- custom resolvers for computed / derived fields --

    @classmethod
    def _resolve_owners(cls, obj: Any) -> list[UserRef]:
        owners = getattr(obj, "owners", None) or []
        return [UserRef.from_model(u) for u in owners]  # type: ignore[misc]

    @classmethod
    def _resolve_changed_by(cls, obj: Any) -> UserRef | None:
        user = getattr(obj, "changed_by", None)
        return UserRef.from_model(user) if user else None  # type: ignore[return-value]

    @classmethod
    def _resolve_created_by(cls, obj: Any) -> UserRef | None:
        user = getattr(obj, "created_by", None)
        return UserRef.from_model(user) if user else None  # type: ignore[return-value]

    @classmethod
    def _resolve_datasource_name(cls, obj: Any) -> str:
        return getattr(obj, "table_name", "")

    @classmethod
    def _resolve_name(cls, obj: Any) -> str:
        return getattr(obj, "table_name", "")

    @classmethod
    def _resolve_uid(cls, obj: Any) -> str:
        ds_type = getattr(obj, "datasource_type", "table")
        return f"{obj.id}__{ds_type}"

    @classmethod
    def _resolve_url(cls, obj: Any) -> str:
        return f"/explore/?datasource_type=table&datasource_id={obj.id}"

    @classmethod
    def _resolve_changed_on_humanized(cls, obj: Any) -> str | None:
        return getattr(obj, "changed_on_delta_humanized", None)

    @classmethod
    def _resolve_created_on_humanized(cls, obj: Any) -> str | None:
        return getattr(obj, "created_on_delta_humanized", None)

    @classmethod
    def _resolve_database(cls, obj: Any) -> DatabaseRef | None:
        database = getattr(obj, "database", None)
        if database is None:
            return None
        # Derive backend from sqlalchemy_uri
        backend: str | None = None
        uri = getattr(database, "sqlalchemy_uri", None) or ""
        if "://" in uri:
            backend = uri.split("://")[0].split("+")[0]
        return DatabaseRef.from_model(database, backend=backend)  # type: ignore[return-value]

    @classmethod
    def _resolve_granularity_sqla(cls, obj: Any) -> list[str]:
        return [
            getattr(col, "column_name", "")
            for col in (getattr(obj, "columns", None) or [])
            if getattr(col, "is_dttm", False)
        ]

    @classmethod
    def _resolve_verbose_map(cls, obj: Any) -> dict[str, str | None]:
        return {
            getattr(col, "column_name", ""): getattr(col, "verbose_name", None)
            for col in (getattr(obj, "columns", None) or [])
        }

    @classmethod
    def _resolve_order_by_choices(cls, obj: Any) -> list[list[Any]]:
        """Build ``order_by_choices`` in the shape expected by the
        frontend control ``order_by_cols`` and the table-viz save flow.

        The original implementation in
        ``superset_old/connectors/sqla/models.py:337`` returns a list of
        ``(json.dumps([column, asc]), "column [asc|desc]")`` pairs.
        The frontend's SelectControl compares saved values (JSON-encoded
        strings like ``'["num", false]'``) against the ``value`` of each
        choice, so the first element MUST be the json-encoded string and
        not a raw ``[column, bool]`` list — otherwise the control can't
        match its saved state and silently drops the selection, resulting
        in empty ``orderby`` in the chart query.
        """
        import json as _json

        choices: list[list[Any]] = []
        for col in getattr(obj, "columns", None) or []:
            col_name = getattr(col, "column_name", "") or ""
            if not col_name:
                continue
            choices.append([_json.dumps([col_name, True]), f"{col_name} [asc]"])
            choices.append([_json.dumps([col_name, False]), f"{col_name} [desc]"])
        return choices

    @classmethod
    def _resolve_time_grain_sqla(cls, obj: Any) -> list[Any]:
        # 1:1 with ``SqlaTable.time_grain_sqla`` (connectors/sqla/models.py):
        # ``[(g.duration, g.name) for g in database.grains()]`` — the choices
        # for the Explore "Time Grain" control. Guard against an unloaded
        # ``database`` relationship (sync lazy-load under asyncpg →
        # MissingGreenlet); the detail handler eager-loads it. ``grains()`` is
        # pure-CPU (reads the engine spec's ``_time_grain_expressions``), no I/O.
        import sqlalchemy as _sa

        try:
            if _sa.inspect(obj).unloaded.intersection({"database"}):
                return []
        except Exception:  # noqa: BLE001, S110
            pass
        database = getattr(obj, "database", None)
        if database is None or not hasattr(database, "grains"):
            return []
        try:
            return [[g.duration, g.name] for g in (database.grains() or [])]
        except Exception:  # noqa: BLE001
            return []

    @classmethod
    def _resolve_column_formats(cls, obj: Any) -> dict[str, Any]:
        # 1:1 with ``SqlaTable.column_formats``: the d3 number format per
        # metric. Metrics are eager-loaded by the detail handler, so this is
        # pure attribute access (async-safe).
        return {
            m.metric_name: m.d3format
            for m in (getattr(obj, "metrics", None) or [])
            if getattr(m, "d3format", None)
        }

    @classmethod
    def _resolve_select_star(cls, obj: Any) -> str | None:
        # 1:1 with ``SqlaTable.select_star`` → ``Database.select_star`` — a
        # ``SELECT * … LIMIT 100`` preview. The model property guards an
        # unloaded ``database`` and opens a lazy sync engine (``create_engine``,
        # no connection) for offline compilation — async-safe.
        return getattr(obj, "select_star", None)

    @classmethod
    def _resolve_rendered_sql(cls, obj: Any) -> None:
        return None

    @classmethod
    def from_model(
        cls,
        obj: Any,
        *,
        rendered_sql: str | None = None,
        **overrides: Any,
    ) -> DatasetDetailResult:
        """Build from ORM model with optional rendered_sql."""
        if rendered_sql is not None:
            overrides["rendered_sql"] = rendered_sql
        return super().from_model(obj, **overrides)  # type: ignore[return-value]


DatasetGetResponse = ApiResponse
DatasetListResponse = ApiListResponse
