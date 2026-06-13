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
"""Async port of the legacy Viz objects.

This module contains the ``BaseViz`` class and all legacy visualization
subclasses that are still served via the ``/superset/explore_json/``
endpoint.  The business logic is identical to the original Flask-based
``superset/viz.py``; the only structural change is that
``get_df`` / ``get_df_payload`` / ``get_payload`` are now ``async``,
calling ``datasource.async_query()`` instead of the synchronous
``datasource.query()``.
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import json as stdlib_json
import logging
import math
import re
from collections import defaultdict, OrderedDict
from datetime import datetime, timedelta
from itertools import product
from typing import Any, cast, TYPE_CHECKING

import geohash
import numpy as np
import pandas as pd
import polyline
from dateutil import relativedelta as rdelta
from geopy.point import Point
from pandas.tseries.frequencies import to_offset

from superset.constants import CACHE_DISABLED_TIMEOUT
from superset.exceptions import (
    CacheLoadError,
    NullValueException,
    QueryObjectValidationError,
    SpatialException,
    SupersetException,
    SupersetSecurityException,
)
from superset.models.connectors import QueryResult
from superset.utils.column import (
    Column,
    extract_dataframe_dtypes,
    get_column_name,
    get_column_names,
    get_column_names_from_columns,
    get_column_names_from_metrics,
    get_metric_name,
    get_metric_names,
    get_time_filter_status,
    Metric,
)
from superset.utils.core import (
    convert_legacy_filters_into_adhoc,
    simple_filter_to_adhoc,
    split_adhoc_filters_into_base_filters,
)
from superset.utils.date import (
    get_since_until,
    parse_human_timedelta,
    parse_past_timedelta,
)
from superset.utils.hashing import md5_sha_from_str

if TYPE_CHECKING:
    from superset.config import SupersetSettings
    from superset.models.connectors import SqlaTable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants ported from superset/utils/core.py
# ---------------------------------------------------------------------------
DTTM_ALIAS = "__timestamp"
JS_MAX_INTEGER = 9007199254740991  # 2^53-1
NO_TIME_RANGE = "No filter"
METRIC_KEYS = [
    "metric",
    "metrics",
    "percent_metrics",
    "metric_2",
    "secondary_metric",
    "x",
    "y",
    "size",
]

# Types
QueryObjectDict = dict[str, Any]
VizData = Any
VizPayload = dict[str, Any]


class ExtraFiltersReasonType:
    COL_NOT_IN_DATASOURCE = "not_in_datasource"
    NO_TEMPORAL_COLUMN = "no_temporal_column"


# ---------------------------------------------------------------------------
# Utility helpers that are needed by viz but don't exist in the new codebase
# ---------------------------------------------------------------------------


def _get_form_data_token(form_data: dict[str, Any]) -> str:
    """Return the token from form_data or generate one."""
    return form_data.get("token", "token_" + md5_sha_from_str(str(form_data))[:8])


def _error_msg_from_exception(ex: Exception) -> str:
    """Translate exception into error message.

    1:1 port of ``superset_old/utils/core.py:455-475``.  Handles dict-type
    ``ex.message`` (common with Presto/Trino drivers) by extracting the
    ``"message"`` key, falling back to ``str(ex)``.
    """
    msg: Any = ""
    if hasattr(ex, "message"):
        if isinstance(ex.message, dict):
            msg = ex.message.get("message")
        elif ex.message:
            msg = ex.message
    return str(msg) or str(ex)


def _get_stacktrace(settings: SupersetSettings | None = None) -> str | None:
    """Return the current stacktrace if SHOW_STACKTRACE is enabled, else None.

    1:1 with the original ``get_stacktrace()`` in
    ``superset_old/utils/core.py:1433-1438`` which checks
    ``app.config["SHOW_STACKTRACE"]`` and returns ``None`` when the flag
    is ``False`` (the production default).
    """
    import traceback

    if settings is not None and getattr(settings, "show_stacktrace", False):
        return traceback.format_exc()
    return None


def get_first_metric_name(
    metrics: list[Metric] | None,
) -> str | None:
    if not metrics:
        return None
    return get_metric_name(metrics[0])


def apply_max_row_limit(row_limit: int, max_limit: int = 100000) -> int:
    if row_limit == 0:
        return max_limit
    return min(row_limit, max_limit)


def _merge_extra_form_data(form_data: dict[str, Any]) -> None:  # noqa: C901
    """Merge extra_form_data into the main form_data payload."""
    extra_form_data = form_data.pop("extra_form_data", {})
    if not extra_form_data:
        return

    # Merge adhoc filters
    adhoc_filters: list[dict[str, Any]] = form_data.get("adhoc_filters", [])
    form_data["adhoc_filters"] = adhoc_filters
    append_adhoc = extra_form_data.get("adhoc_filters", [])
    adhoc_filters.extend({"isExtra": True, **af} for af in append_adhoc)

    # Merge simple filters
    append_filters = extra_form_data.get("filters")
    if append_filters:
        for key, value in form_data.items():
            if re.match(r"adhoc_filter.*", key) and isinstance(value, list):
                value.extend(
                    simple_filter_to_adhoc({"isExtra": True, **fltr})
                    for fltr in append_filters
                    if fltr
                )

    # Map override keys
    override_mappings = {
        "granularity_sqla": "granularity_sqla",
        "time_grain_sqla": "time_grain_sqla",
        "time_range": "time_range",
    }
    for src_key, target_key in override_mappings.items():
        value = extra_form_data.get(src_key)
        if value is not None:
            form_data[target_key] = value

    # Map extra keys
    extras = form_data.get("extras", {})
    for key in ("time_grain_sqla", "having", "where"):
        value = extra_form_data.get(key)
        if value is not None:
            extras[key] = value
    if extras:
        form_data["extras"] = extras

    if form_data.get("time_range") and not form_data.get("granularity_sqla"):
        for adhoc_filter in form_data.get("adhoc_filters", []):
            if adhoc_filter.get("operator") == "TEMPORAL_RANGE":
                adhoc_filter["comparator"] = form_data["time_range"]


def merge_extra_filters(form_data: dict[str, Any]) -> None:  # noqa: C901
    """Port of superset.utils.core.merge_extra_filters."""
    form_data.setdefault("applied_time_extras", {})
    adhoc_filters: list[dict[str, Any]] = form_data.get("adhoc_filters", [])
    form_data["adhoc_filters"] = adhoc_filters
    _merge_extra_form_data(form_data)

    if "extra_filters" not in form_data:
        return

    date_options = {
        "__time_range": "time_range",
        "__time_col": "granularity_sqla",
        "__time_grain": "time_grain_sqla",
    }

    def get_filter_key(f: dict[str, Any]) -> str:
        if "expressionType" in f:
            return f"{f['subject']}__{f['operator']}"
        return f"{f['col']}__{f['op']}"

    existing_filters: dict[str, Any] = {}
    for existing in adhoc_filters:
        # Support both camelCase and snake_case payload conventions.
        expression_type = existing.get("expressionType") or existing.get(
            "expression_type"
        )
        if (
            expression_type == "SIMPLE"
            and existing.get("comparator") is not None
            and existing.get("subject") is not None
        ):
            existing_filters[get_filter_key(existing)] = existing["comparator"]

    for filtr in form_data["extra_filters"]:
        filtr["isExtra"] = True
        filter_column = filtr["col"]
        if time_extra := date_options.get(filter_column):
            time_extra_value = filtr.get("val")
            if time_extra_value and time_extra_value != NO_TIME_RANGE:
                form_data[time_extra] = time_extra_value
                form_data["applied_time_extras"][filter_column] = time_extra_value
        elif filtr["val"]:
            filter_key = get_filter_key(filtr)
            if filter_key in existing_filters:
                if isinstance(filtr["val"], list):
                    if isinstance(existing_filters[filter_key], list):
                        if set(existing_filters[filter_key]) != set(filtr["val"]):
                            adhoc_filters.append(simple_filter_to_adhoc(filtr))
                    else:
                        adhoc_filters.append(simple_filter_to_adhoc(filtr))
                else:
                    if filtr["val"] != existing_filters[filter_key]:
                        adhoc_filters.append(simple_filter_to_adhoc(filtr))
            else:
                adhoc_filters.append(simple_filter_to_adhoc(filtr))

    del form_data["extra_filters"]


def _normalize_dttm_col(
    df: pd.DataFrame,
    timestamp_format: str | None = None,
    offset: int = 0,
    time_shift: str | None = None,
) -> None:
    """Normalize the __timestamp column in-place.

    1:1 port of ``superset_old/utils/core.py:1779-1822``
    (``normalize_dttm_col``), specialized for the single ``__timestamp``
    (``DTTM_ALIAS``) column that ``BaseViz.get_df`` passes.

    Key parity notes:
    - ``utc=False`` (not ``True``): produces naive ``datetime64[ns]``
      series matching the original's behavior.
    - Epoch formats: checks ``is_numeric_dtype`` and has a fallback to
      ``pd.Timestamp(x)`` for already-formatted timestamp strings.
    - Non-epoch formats: uses ``errors="coerce"`` and ``exact=False``.
    """
    from pandas.core.dtypes.common import is_numeric_dtype

    if DTTM_ALIAS not in df.columns:
        return
    if df[DTTM_ALIAS].empty:
        return

    if timestamp_format in ("epoch_s", "epoch_ms"):
        dttm_series = df[DTTM_ALIAS]
        if is_numeric_dtype(dttm_series):
            # Column is formatted as a numeric value
            unit = timestamp_format.replace("epoch_", "")
            df[DTTM_ALIAS] = pd.to_datetime(
                dttm_series,
                utc=False,
                unit=unit,
                origin="unix",
                errors="coerce",
                exact=False,
            )
        else:
            # Column has already been formatted as a timestamp.
            try:
                df[DTTM_ALIAS] = dttm_series.apply(
                    lambda x: pd.Timestamp(x) if pd.notna(x) else pd.NaT
                )
            except ValueError:
                logger.warning(
                    "Unable to convert column %s to datetime, ignoring",
                    DTTM_ALIAS,
                )
    else:
        df[DTTM_ALIAS] = pd.to_datetime(
            df[DTTM_ALIAS],
            utc=False,
            format=timestamp_format,
            errors="coerce",
            exact=False,
        )

    if offset:
        df[DTTM_ALIAS] += timedelta(hours=offset)
    if time_shift is not None:
        df[DTTM_ALIAS] += parse_human_timedelta(time_shift)


def _extract_dataframe_dtypes(
    df: pd.DataFrame,
    datasource: Any,
) -> list[int]:
    """Return GenericDataType int list for DataFrame columns."""
    return extract_dataframe_dtypes(df, datasource)


def _get_time_filter_status(
    datasource: Any,
    applied_time_extras: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (applied, rejected) time filter columns."""
    return get_time_filter_status(datasource, applied_time_extras)


# ---------------------------------------------------------------------------
# get_viz factory
# ---------------------------------------------------------------------------
def _resolve_settings() -> SupersetSettings | None:
    """Best-effort lazy ``SupersetSettings`` for denylist enforcement."""
    from superset.config import SupersetSettings

    try:
        return SupersetSettings()  # type: ignore[call-arg]
    except Exception:  # noqa: BLE001
        return None


def get_active_viz_types(
    settings: SupersetSettings | None = None,
) -> dict[str | None, type[BaseViz]]:
    """Return the viz registry with ``VIZ_TYPE_DENYLIST`` entries removed.

    Mirrors upstream's module-level ``viz_types`` comprehension, which filters
    by ``current_app.config["VIZ_TYPE_DENYLIST"]``. The port can't filter at
    import time (settings/app may not exist yet), so it filters on access.
    Routing guards (``if viz_type in get_active_viz_types()``) therefore skip a
    denied type exactly as upstream did — sending it to the non-legacy path
    instead of instantiating the legacy ``BaseViz`` pipeline.
    """
    if settings is None:
        settings = _resolve_settings()
    denylist = (getattr(settings, "viz_type_denylist", []) or []) if settings else []
    if not denylist:
        return dict(viz_types)
    return {vt: c for vt, c in viz_types.items() if vt not in denylist}


def get_viz(
    datasource: SqlaTable,
    form_data: dict[str, Any],
    force: bool = False,
    force_cached: bool = False,
    settings: SupersetSettings | None = None,
) -> BaseViz:
    """Build a viz object from datasource + form_data.

    The VIZ_TYPE_DENYLIST is applied lazily here rather than at
    import time because settings may not be available when the
    module is first loaded.
    """
    viz_type = form_data.get("viz_type", "table")

    # Apply VIZ_TYPE_DENYLIST. Upstream reads
    # ``current_app.config["VIZ_TYPE_DENYLIST"]`` unconditionally; lazily
    # resolve settings when the caller didn't thread them so a denied type is
    # rejected on EVERY path (warm-up / annotation included), not only when a
    # settings object happens to be passed.
    if settings is None:
        settings = _resolve_settings()
    if settings is not None:
        denylist = getattr(settings, "viz_type_denylist", []) or []
        if viz_type in denylist:
            raise SupersetException(
                f"Visualization type '{viz_type}' has been disabled"
                " by the administrator."
            )

    cls = viz_types.get(viz_type)
    if cls is None:
        cls = BaseViz
    return cls(
        datasource=datasource,
        form_data=form_data,
        force=force,
        force_cached=force_cached,
        settings=settings,
    )


# ---------------------------------------------------------------------------
# BaseViz
# ---------------------------------------------------------------------------
class BaseViz:
    """All visualizations derive this base class."""

    viz_type: str | None = None
    verbose_name = "Base Viz"
    credits = ""
    is_timeseries = False
    cache_type = "df"
    enforce_numerical_metrics = True
    # Injection point: explore_json wires a sync viz-cache manager here so
    # ``force_cached`` reads the worker-written DATA_CACHE_CONFIG slot.
    cache_manager: Any

    def __init__(
        self,
        datasource: SqlaTable,
        form_data: dict[str, Any],
        force: bool = False,
        force_cached: bool = False,
        settings: SupersetSettings | None = None,
    ) -> None:
        if not datasource:
            raise QueryObjectValidationError("Viz is missing a datasource")

        self.datasource = datasource
        self.viz_type = form_data.get("viz_type")
        self.form_data = form_data
        self.settings = settings

        self.query = ""
        self.token = _get_form_data_token(form_data)

        self.groupby: list[Column] = self.form_data.get("groupby") or []
        self.time_shift = timedelta()

        self.status: str | None = None
        self.error_msg = ""
        self.results: QueryResult | None = None
        self.applied_filter_columns: list[Column] = []
        self.rejected_filter_columns: list[Column] = []
        self.errors: list[dict[str, Any]] = []
        self.force = force
        self._force_cached = force_cached
        self.from_dttm: datetime | None = None
        self.to_dttm: datetime | None = None
        self._extra_chart_data: list[tuple[str, pd.DataFrame]] = []

        # RLS cache key — set externally by the controller/processor after
        # ``await security_manager.get_rls_cache_key(datasource, user=user)``
        # so that ``cache_key()`` (a sync method) can include the RLS
        # component without entering the event loop.  Defaults to [] which
        # matches the non-RLS behaviour (safe but cache is not RLS-aware).
        self._rls_cache_key: list[str] = []

        self.process_metrics()
        self.applied_filters: list[dict[str, str]] = []
        self.rejected_filters: list[dict[str, str]] = []

    @property
    def force_cached(self) -> bool:
        return self._force_cached

    def _get_setting(self, key: str, default: Any = None) -> Any:
        """Get a setting value, falling back to defaults."""
        if self.settings is not None:
            return getattr(self.settings, key, default)
        return default

    def process_metrics(self) -> None:
        self.metric_dict: OrderedDict[str, Any] = OrderedDict()
        for mkey in METRIC_KEYS:
            val = self.form_data.get(mkey)
            if val:
                if not isinstance(val, list):
                    val = [val]
                for o in val:
                    label = get_metric_name(o)
                    self.metric_dict[label] = o
        self.all_metrics = list(self.metric_dict.values())
        self.metric_labels = list(self.metric_dict.keys())

    @staticmethod
    def handle_js_int_overflow(
        data: dict[str, list[dict[str, Any]]],
    ) -> dict[str, list[dict[str, Any]]]:
        for record in data.get("records", {}):
            for k, v in list(record.items()):
                if isinstance(v, int):
                    if abs(v) > JS_MAX_INTEGER:
                        record[k] = str(v)
        return data

    def run_extra_queries(self) -> None:
        """Lifecycle method for multi-query visualizations."""
        pass

    async def async_run_extra_queries(self) -> None:
        """Async lifecycle method for multi-query visualizations."""
        pass

    def apply_rolling(self, df: pd.DataFrame) -> pd.DataFrame:
        rolling_type = self.form_data.get("rolling_type")
        rolling_periods = int(self.form_data.get("rolling_periods") or 0)
        min_periods = int(self.form_data.get("min_periods") or 0)

        if rolling_type in ("mean", "std", "sum") and rolling_periods:
            kwargs = {"window": rolling_periods, "min_periods": min_periods}
            if rolling_type == "mean":
                df = df.rolling(**kwargs).mean()
            elif rolling_type == "std":
                df = df.rolling(**kwargs).std()
            elif rolling_type == "sum":
                df = df.rolling(**kwargs).sum()
        elif rolling_type == "cumsum":
            df = df.cumsum()
        if min_periods:
            df = df[min_periods:]
        if df.empty:
            raise QueryObjectValidationError(
                "Applied rolling window did not return any data. Please make sure "
                "the source query satisfies the minimum periods defined in the "
                "rolling window."
            )
        return df

    @staticmethod
    def dedup_columns(*columns_args: list[Column] | None) -> list[Column]:
        labels: list[str] = []
        deduped_columns: list[Column] = []
        for columns in columns_args:
            for column in columns or []:
                label = get_column_name(column)
                if label not in labels:
                    labels.append(label)
                    deduped_columns.append(column)
        return deduped_columns

    def process_query_filters(self) -> None:
        convert_legacy_filters_into_adhoc(self.form_data)
        merge_extra_filters(self.form_data)
        engine = "unknown"
        if hasattr(self.datasource, "database") and self.datasource.database:
            engine = getattr(self.datasource.database, "backend", "unknown")
        split_adhoc_filters_into_base_filters(self.form_data, engine)

    def query_obj(self) -> QueryObjectDict:
        """Build a query object from form_data."""
        self.process_query_filters()

        metrics = self.all_metrics or []
        groupby = self.dedup_columns(self.groupby, self.form_data.get("columns"))

        is_timeseries = self.is_timeseries
        if DTTM_ALIAS in (groupby_labels := get_column_names(groupby)):
            del groupby[groupby_labels.index(DTTM_ALIAS)]
            is_timeseries = True

        granularity = self.form_data.get("granularity_sqla")
        limit = int(self.form_data.get("limit") or 0)
        timeseries_limit_metric = self.form_data.get("timeseries_limit_metric")

        row_limit = int(
            self.form_data.get("row_limit") or self._get_setting("row_limit", 50000)
        )
        row_limit = apply_max_row_limit(row_limit)

        order_desc = self.form_data.get("order_desc", True)

        default_relative_start = self._get_setting(
            "default_relative_start_time", "today"
        )
        default_relative_end = self._get_setting("default_relative_end_time", "today")

        try:
            since, until = get_since_until(
                relative_start=default_relative_start,
                relative_end=default_relative_end,
                time_range=self.form_data.get("time_range"),
                since=self.form_data.get("since"),
                until=self.form_data.get("until"),
            )
        except ValueError as ex:
            raise QueryObjectValidationError(str(ex)) from ex

        time_shift = self.form_data.get("time_shift", "")
        self.time_shift = parse_past_timedelta(time_shift)
        from_dttm = None if since is None else (since - self.time_shift)
        to_dttm = None if until is None else (until - self.time_shift)
        if from_dttm and to_dttm and from_dttm > to_dttm:
            raise QueryObjectValidationError("From date cannot be larger than to date")

        self.from_dttm = from_dttm
        self.to_dttm = to_dttm

        # Validate sql filters
        for param in ("where", "having"):
            clause = self.form_data.get(param)
            if clause:
                from superset.utils.sql import sanitize_clause

                engine_name = "unknown"
                if hasattr(self.datasource, "database") and self.datasource.database:
                    engine_name = getattr(
                        self.datasource.database, "backend", "unknown"
                    )
                sanitized = sanitize_clause(clause, engine_name)
                if sanitized != clause:
                    self.form_data[param] = sanitized

        extras = {
            "having": self.form_data.get("having", ""),
            "time_grain_sqla": self.form_data.get("time_grain_sqla"),
            "where": self.form_data.get("where", ""),
        }

        return {
            "granularity": granularity,
            "from_dttm": from_dttm,
            "to_dttm": to_dttm,
            "is_timeseries": is_timeseries,
            "groupby": groupby,
            "metrics": metrics,
            "row_limit": row_limit,
            "filter": self.form_data.get("filters", []),
            "timeseries_limit": limit,
            "extras": extras,
            "timeseries_limit_metric": timeseries_limit_metric,
            "order_desc": order_desc,
        }

    @property
    def cache_timeout(self) -> int:
        if self.form_data.get("cache_timeout") is not None:
            return int(self.form_data["cache_timeout"])
        if self.datasource.cache_timeout is not None:
            return int(self.datasource.cache_timeout)
        if (
            hasattr(self.datasource, "database")
            and self.datasource.database
            and getattr(self.datasource.database, "cache_timeout", None) is not None
        ):
            return self.datasource.database.cache_timeout
        return self._get_setting("cache_default_timeout", 300)

    def cache_key(self, query_obj: QueryObjectDict, **extra: Any) -> str:
        cache_dict = copy.copy(query_obj)
        cache_dict.update(extra)

        for k in ["from_dttm", "to_dttm", "inner_from_dttm", "inner_to_dttm"]:
            if k in cache_dict:
                del cache_dict[k]

        cache_dict["time_range"] = self.form_data.get("time_range")
        cache_dict["datasource"] = getattr(
            self.datasource, "uid", str(self.datasource.id)
        )
        if hasattr(self.datasource, "get_extra_cache_keys"):
            cache_dict["extra_cache_keys"] = self.datasource.get_extra_cache_keys(
                query_obj
            )
        cache_dict["changed_on"] = str(getattr(self.datasource, "changed_on", ""))
        # RLS cache key — populated externally before cache_key() is called
        # via ``viz._rls_cache_key = await security_manager.get_rls_cache_key(...)``.
        # Mirrors ``security_manager.get_rls_cache_key(self.datasource)`` from
        # the original ``superset_old/viz.py`` line 471.
        cache_dict["rls"] = self._rls_cache_key
        json_data = self.json_dumps(cache_dict, sort_keys=True)
        return md5_sha_from_str(json_data)

    async def get_df(self, query_obj: QueryObjectDict | None = None) -> pd.DataFrame:
        """Returns a pandas dataframe based on the query object (async)."""
        if not query_obj:
            query_obj = self.query_obj()
        if not query_obj:
            return pd.DataFrame()

        self.error_msg = ""

        # Resolve python_date_format from the granularity column
        timestamp_format = None
        if getattr(self.datasource, "type", None) == "table":
            granularity = (query_obj or {}).get("granularity")
            if granularity and hasattr(self.datasource, "get_column"):
                granularity_col = self.datasource.get_column(granularity)
                if granularity_col:
                    timestamp_format = getattr(
                        granularity_col, "python_date_format", None
                    )

        # The datasource here can be different backend but the interface is common
        self.results = await self.datasource.async_query(query_obj)
        self.applied_filter_columns = cast(
            list[Column], self.results.applied_filter_columns or []
        )
        self.rejected_filter_columns = cast(
            list[Column], self.results.rejected_filter_columns or []
        )
        self.query = self.results.query
        self.status = self.results.status
        self.errors = self.results.errors

        df = self.results.df
        if not df.empty:
            _normalize_dttm_col(
                df=df,
                timestamp_format=timestamp_format,
                offset=getattr(self.datasource, "offset", 0) or 0,
                time_shift=self.form_data.get("time_shift"),
            )

            if self.enforce_numerical_metrics:
                self.df_metrics_to_num(df)

            df.replace([np.inf, -np.inf], np.nan, inplace=True)
        return df

    def df_metrics_to_num(self, df: pd.DataFrame) -> None:
        metrics = self.metric_labels
        for col, dtype in df.dtypes.items():
            if dtype.type == np.object_ and col in metrics:
                df[col] = pd.to_numeric(df[col], errors="coerce")

    async def get_payload(self, query_obj: QueryObjectDict | None = None) -> VizPayload:
        """Returns a payload of metadata and data (async)."""
        try:
            await self.async_run_extra_queries()
        except SupersetSecurityException as ex:
            error = dataclasses.asdict(ex.error)
            self.errors.append(error)
            self.status = "failed"

        payload = await self.get_df_payload(query_obj)

        df = cast("pd.DataFrame | None", payload.get("df"))

        if self.status != "failed":
            payload["data"] = self.get_data(df)
        if "df" in payload:
            del payload["df"]

        applied_filter_columns = self.applied_filter_columns or []
        rejected_filter_columns = self.rejected_filter_columns or []
        applied_time_extras = self.form_data.get("applied_time_extras", {})
        applied_time_columns, rejected_time_columns = _get_time_filter_status(
            self.datasource, applied_time_extras
        )
        payload["applied_filters"] = [
            {"column": get_column_name(col)} for col in applied_filter_columns
        ] + applied_time_columns
        payload["rejected_filters"] = [
            {
                "reason": ExtraFiltersReasonType.COL_NOT_IN_DATASOURCE,
                "column": get_column_name(col),
            }
            for col in rejected_filter_columns
        ] + rejected_time_columns
        if df is not None:
            payload["colnames"] = list(df.columns)
        return payload

    def _get_data_cache(self) -> Any:
        """Return the data cache backend, or None if unavailable.

        Subclasses or callers may inject a cache_manager via ``self.cache_manager``.
        The attribute is expected to expose a ``.get()`` / ``.set()`` interface
        (e.g. a Flask-Caching or compatible backend).
        """
        cm = getattr(self, "cache_manager", None)
        if cm is not None:
            # cache_manager may be an object with a ``data_cache`` attribute
            # (like the original Flask CacheManager extension) or it may
            # *be* the data cache itself.
            if hasattr(cm, "data_cache"):
                return cm.data_cache
            if hasattr(cm, "get") and hasattr(cm, "set"):
                return cm
        return None

    async def get_df_payload(  # noqa: C901
        self, query_obj: QueryObjectDict | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        """Handles caching around the df payload retrieval (async)."""
        if not query_obj:
            query_obj = self.query_obj()
        cache_key = self.cache_key(query_obj, **kwargs) if query_obj else None
        cache_value: dict[str, Any] | None = None
        logger.info("Cache key: %s", cache_key)
        is_loaded = False
        stacktrace = None
        df = None
        cache_timeout = self.cache_timeout

        # --- Cache read ---
        # 1:1 with the original (viz.py:531): a datasource with a
        # ``CACHE_DISABLED_TIMEOUT`` (-1) cache_timeout bypasses the cache.
        force = self.force or cache_timeout == CACHE_DISABLED_TIMEOUT
        data_cache = self._get_data_cache()
        if cache_key and data_cache is not None and not force:
            try:
                # ``data_cache`` is a synchronous backend (Flask-Caching parity);
                # off-load the blocking Redis round-trip so it doesn't stall the
                # event loop in the async web process.
                raw = await asyncio.to_thread(data_cache.get, cache_key)
                if raw is not None:
                    if isinstance(raw, dict) and "df" in raw:
                        cache_value = raw
                        df = cache_value["df"]
                        self.query = cache_value.get("query", "")
                        self.applied_filter_columns = cache_value.get(
                            "applied_filter_columns", []
                        )
                        self.rejected_filter_columns = cache_value.get(
                            "rejected_filter_columns", []
                        )
                        self.status = "success"
                        is_loaded = True
                        logger.info("Serving from cache")
            except Exception as ex:
                logger.error("Error reading cache: %s", str(ex), exc_info=True)

        if query_obj and not is_loaded:
            if self.force_cached:
                logger.warning(
                    "force_cached (viz.py): value not found for cache key %s",
                    cache_key,
                )
                # 1:1 with the original BaseViz.get_df_payload (viz.py:563):
                # in force_cached mode a cache miss must NOT compute — it raises
                # so the explore_json GAQ branch falls through to an async job.
                raise CacheLoadError("Cached value not found")
            try:
                invalid_columns = [
                    col
                    for col in get_column_names_from_columns(
                        query_obj.get("columns") or []
                    )
                    + get_column_names_from_columns(query_obj.get("groupby") or [])
                    + get_column_names_from_metrics(
                        cast(list[Metric], query_obj.get("metrics") or [])
                    )
                    if col not in self.datasource.column_names
                ]
                if invalid_columns:
                    raise QueryObjectValidationError(
                        f"Columns missing in datasource: {invalid_columns}"
                    )
                df = await self.get_df(query_obj)
                if self.status != "failed":
                    is_loaded = True
            except QueryObjectValidationError as ex:
                error = dataclasses.asdict(_make_superset_error(message=str(ex)))
                self.errors.append(error)
                self.status = "failed"
            except Exception as ex:
                logger.exception(ex)
                error = dataclasses.asdict(_make_superset_error(message=str(ex)))
                self.errors.append(error)
                self.status = "failed"
                stacktrace = _get_stacktrace(self.settings)

            # --- Cache write ---
            # 1:1 with the original (viz.py:615-622): a successful load is
            # written to cache regardless of ``self.force`` — a forced refresh
            # must update the cache, not just bypass the read.
            # CACHE_DISABLED_TIMEOUT (-1) is the sentinel meaning "do not
            # cache" — skip the write entirely, matching the original's
            # ``set_and_log_cache`` guard in
            # ``superset_old/utils/cache.py:62-63``.
            if (
                is_loaded
                and cache_key
                and data_cache is not None
                and self.status != "failed"
                and cache_timeout != CACHE_DISABLED_TIMEOUT
            ):
                try:
                    # ``dttm`` — 1:1 with ``set_and_log_cache``
                    # (superset_old/utils/cache.py:65-66); the return value
                    # below reads it back as ``cached_dttm`` for the
                    # frontend's "Last cached at …" display.
                    from datetime import datetime as _datetime

                    cache_payload = {
                        "df": df,
                        "query": self.query,
                        "applied_filter_columns": self.applied_filter_columns,
                        "rejected_filter_columns": self.rejected_filter_columns,
                        "dttm": _datetime.utcnow().isoformat().split(".")[0],
                    }
                    # Sync backend — off-load the blocking write off the loop.
                    await asyncio.to_thread(
                        data_cache.set, cache_key, cache_payload, cache_timeout
                    )
                    logger.info("Stored result in cache, key: %s", cache_key)
                except Exception:
                    logger.warning(
                        "Failed to cache result for key %s",
                        cache_key,
                        exc_info=True,
                    )

        return {
            "cache_key": cache_key,
            "cached_dttm": cache_value.get("dttm") if cache_value is not None else None,
            "cache_timeout": cache_timeout,
            "df": df,
            "errors": self.errors,
            "form_data": self.form_data,
            "is_cached": cache_value is not None,
            "query": self.query,
            "from_dttm": self.from_dttm,
            "to_dttm": self.to_dttm,
            "status": self.status,
            "stacktrace": stacktrace,
            "rowcount": len(df.index) if df is not None else 0,
            "colnames": list(df.columns) if df is not None else None,
            "coltypes": (
                _extract_dataframe_dtypes(df, self.datasource)
                if df is not None
                else None
            ),
        }

    @staticmethod
    def json_dumps(query_obj: Any, sort_keys: bool = False) -> str:
        from superset.utils.json import json_int_dttm_ser

        def _default(obj: Any) -> Any:
            return json_int_dttm_ser(obj)

        # Pre-process NaN/Inf values since stdlib json.dumps doesn't
        # support ignore_nan (unlike simplejson used by the original).
        def _sanitize(obj: Any) -> Any:
            if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
                return None
            if isinstance(obj, dict):
                return {k: _sanitize(v) for k, v in obj.items()}
            if isinstance(obj, list):
                return [_sanitize(v) for v in obj]
            return obj

        return stdlib_json.dumps(
            _sanitize(query_obj),
            default=_default,
            sort_keys=sort_keys,
        )

    @staticmethod
    def has_error(payload: VizPayload) -> bool:
        return (
            payload.get("status") == "failed"
            or payload.get("error") is not None
            or bool(payload.get("errors"))
        )

    def payload_json_and_has_error(self, payload: VizPayload) -> tuple[str, bool]:
        return self.json_dumps(payload), self.has_error(payload)

    def get_data(self, df: pd.DataFrame | None) -> VizData:
        if df is None or df.empty:
            return None
        return df.to_dict(orient="records")

    async def get_samples(self) -> dict[str, Any]:
        """Build a simple SELECT * LIMIT N query and execute it."""
        query_obj = self.query_obj()
        samples_row_limit = self._get_setting("samples_row_limit", 1000)
        query_obj.update(
            {
                "is_timeseries": False,
                "groupby": [],
                "metrics": [],
                "orderby": [],
                "row_limit": samples_row_limit,
                "columns": [o.column_name for o in self.datasource.columns],
                "from_dttm": None,
                "to_dttm": None,
            }
        )
        payload = await self.get_df_payload(query_obj)
        df = payload.get("df")
        return {
            "data": df.to_dict(orient="records") if df is not None else [],
            "colnames": payload.get("colnames"),
            "coltypes": payload.get("coltypes"),
            "rowcount": payload.get("rowcount"),
            "sql_rowcount": payload.get("sql_rowcount"),
        }

    async def get_csv(self) -> str:
        """Return a CSV string of the query result."""
        from superset.utils.csv import df_to_escaped_csv

        payload = await self.get_df_payload()
        df = payload.get("df")
        if df is None or df.empty:
            return ""
        include_index = not isinstance(df.index, pd.RangeIndex)
        csv_export_config = self._get_setting("csv_export", {}) or {}
        return df_to_escaped_csv(df, index=include_index, **csv_export_config)

    async def get_json(self) -> str:
        """Return JSON string of the full payload."""
        payload = await self.get_payload()
        return self.json_dumps(payload)

    def raise_for_access(self) -> None:
        """Placeholder for access check."""
        pass


# ---------------------------------------------------------------------------
# Helper to create error dicts
# ---------------------------------------------------------------------------
def _make_superset_error(
    message: str,
    error_type: str = "VIZ_GET_DF_ERROR",
    level: str = "ERROR",
) -> Any:
    """Create a simple error dataclass-like object for serialization."""

    @dataclasses.dataclass
    class _Err:
        message: str
        error_type: str
        level: str

    return _Err(message=message, error_type=error_type, level=level)


# ---------------------------------------------------------------------------
# TimeTableViz
# ---------------------------------------------------------------------------
class TimeTableViz(BaseViz):
    viz_type = "time_table"
    verbose_name = "Time Table View"
    is_timeseries = True

    def query_obj(self) -> QueryObjectDict:
        query_obj = super().query_obj()
        if not self.form_data.get("metrics"):
            raise QueryObjectValidationError("Pick at least one metric")
        if self.form_data.get("groupby") and len(self.form_data["metrics"]) > 1:
            raise QueryObjectValidationError(
                "When using 'Group By' you are limited to use a single metric"
            )
        sort_by = get_first_metric_name(query_obj["metrics"])
        is_asc = not query_obj.get("order_desc")
        query_obj["orderby"] = [(sort_by, is_asc)]
        return query_obj

    def get_data(self, df: pd.DataFrame | None) -> VizData:
        if df is None or df.empty:
            return None
        columns = None
        values: list[str] | str = self.metric_labels
        if self.form_data.get("groupby"):
            values = self.metric_labels[0]
            columns = get_column_names(self.form_data.get("groupby"))
        pt = df.pivot_table(index=DTTM_ALIAS, columns=columns, values=values)
        pt.index = pt.index.map(str)
        pt = pt.sort_index()
        return {
            "records": pt.to_dict(orient="index"),
            "columns": list(pt.columns),
            "is_group_by": bool(self.form_data.get("groupby")),
        }


# ---------------------------------------------------------------------------
# CalHeatmapViz
# ---------------------------------------------------------------------------
class CalHeatmapViz(BaseViz):
    viz_type = "cal_heatmap"
    verbose_name = "Calendar Heatmap"
    is_timeseries = True

    def get_data(self, df: pd.DataFrame | None) -> VizData:  # noqa: C901
        if df is None or df.empty:
            return None
        form_data = self.form_data
        data: dict[str, Any] = {}
        records = df.to_dict("records")
        for metric in self.metric_labels:
            values: dict[str, Any] = {}
            for query_obj in records:
                v = query_obj[DTTM_ALIAS]
                if hasattr(v, "value"):
                    v = v.value
                values[str(v / 10**9)] = query_obj.get(metric)
            data[metric] = values

        try:
            relative_start = self._get_setting("default_relative_start_time", "today")
            relative_end = self._get_setting("default_relative_end_time", "today")
            start, end = get_since_until(
                relative_start=relative_start,
                relative_end=relative_end,
                time_range=form_data.get("time_range"),
                since=form_data.get("since"),
                until=form_data.get("until"),
            )
        except ValueError as ex:
            raise QueryObjectValidationError(str(ex)) from ex
        if not start or not end:
            raise QueryObjectValidationError(
                "Please provide both time bounds (Since and Until)"
            )
        domain = form_data.get("domain_granularity")
        diff_delta = rdelta.relativedelta(end, start)
        diff_secs = (end - start).total_seconds()

        if domain == "year":
            range_ = end.year - start.year + 1
        elif domain == "month":
            range_ = diff_delta.years * 12 + diff_delta.months + 1
        elif domain == "week":
            range_ = diff_delta.years * 53 + diff_delta.weeks + 1
        elif domain == "day":
            range_ = int(diff_secs // (24 * 60 * 60)) + 1
        else:
            range_ = int(diff_secs // (60 * 60)) + 1

        return {
            "data": data,
            "start": start,
            "domain": domain,
            "subdomain": form_data.get("subdomain_granularity"),
            "range": range_,
        }

    def query_obj(self) -> QueryObjectDict:
        query_obj = super().query_obj()
        query_obj["metrics"] = self.form_data.get("metrics")
        mapping = {
            "min": "PT1M",
            "hour": "PT1H",
            "day": "P1D",
            "week": "P1W",
            "month": "P1M",
            "year": "P1Y",
        }
        query_obj["extras"]["time_grain_sqla"] = mapping[
            self.form_data.get("subdomain_granularity", "min")
        ]
        return query_obj


# ---------------------------------------------------------------------------
# NVD3Viz (base for NVD3 chart types)
# ---------------------------------------------------------------------------
class NVD3Viz(BaseViz):
    credits = '<a href="http://nvd3.org/">NVD3.org</a>'
    viz_type: str | None = None
    verbose_name = "Base NVD3 Viz"
    is_timeseries = False


# ---------------------------------------------------------------------------
# BubbleViz
# ---------------------------------------------------------------------------
class BubbleViz(NVD3Viz):
    viz_type = "bubble"
    verbose_name = "Bubble Chart"
    is_timeseries = False

    def query_obj(self) -> QueryObjectDict:
        query_obj = super().query_obj()
        query_obj["groupby"] = [self.form_data.get("entity")]
        if self.form_data.get("series"):
            query_obj["groupby"].append(self.form_data.get("series"))
        query_obj["groupby"] = self.dedup_columns(query_obj["groupby"])

        self.x_metric = self.form_data["x"]
        self.y_metric = self.form_data["y"]
        self.z_metric = self.form_data["size"]
        self.entity = self.form_data.get("entity")
        self.series = self.form_data.get("series") or self.entity
        query_obj["row_limit"] = self.form_data.get("limit")
        query_obj["metrics"] = [self.z_metric, self.x_metric, self.y_metric]
        if len(set(self.metric_labels)) < 3:
            raise QueryObjectValidationError("Please use 3 different metric labels")
        if not all(query_obj["metrics"] + [self.entity]):
            raise QueryObjectValidationError("Pick a metric for x, y and size")
        return query_obj

    def get_data(self, df: pd.DataFrame | None) -> VizData:
        if df is None or df.empty:
            return None
        df["x"] = df[[get_metric_name(self.x_metric)]]
        df["y"] = df[[get_metric_name(self.y_metric)]]
        df["size"] = df[[get_metric_name(self.z_metric)]]
        df["shape"] = "circle"
        df["group"] = df[[get_column_name(self.series or "")]]

        series: dict[Any, list[Any]] = defaultdict(list)
        for row in df.to_dict(orient="records"):
            series[row["group"]].append(row)
        chart_data = []
        for k, v in series.items():
            chart_data.append({"key": k, "values": v})
        return chart_data


# ---------------------------------------------------------------------------
# BulletViz
# ---------------------------------------------------------------------------
class BulletViz(NVD3Viz):
    viz_type = "bullet"
    verbose_name = "Bullet Chart"
    is_timeseries = False

    def query_obj(self) -> QueryObjectDict:
        form_data = self.form_data
        query_obj = super().query_obj()
        self.metric = form_data["metric"]
        query_obj["metrics"] = [self.metric]
        if not self.metric:
            raise QueryObjectValidationError("Pick a metric to display")
        return query_obj

    def get_data(self, df: pd.DataFrame | None) -> VizData:
        if df is None or df.empty:
            return None
        df["metric"] = df[[get_metric_name(self.metric)]]
        values = df["metric"].values
        return {"measures": values.tolist()}


# ---------------------------------------------------------------------------
# NVD3TimeSeriesViz
# ---------------------------------------------------------------------------
class NVD3TimeSeriesViz(NVD3Viz):
    viz_type = "line"
    verbose_name = "Time Series - Line Chart"
    sort_series = False
    is_timeseries = True
    pivot_fill_value: int | None = None

    def query_obj(self) -> QueryObjectDict:
        query_obj = super().query_obj()
        sort_by = self.form_data.get(
            "timeseries_limit_metric"
        ) or get_first_metric_name(query_obj.get("metrics") or [])
        is_asc = not self.form_data.get("order_desc")
        if sort_by:
            sort_by_label = get_metric_name(sort_by)
            if sort_by_label not in get_metric_names(query_obj["metrics"]):
                query_obj["metrics"].append(sort_by)
            query_obj["orderby"] = [(sort_by, is_asc)]
        return query_obj

    def to_series(  # noqa: C901
        self, df: pd.DataFrame, classed: str = "", title_suffix: str = ""
    ) -> list[dict[str, Any]]:
        cols = []
        for col in df.columns:
            if col == "":
                cols.append("N/A")
            elif col is None:
                cols.append("NULL")
            else:
                cols.append(col)
        df.columns = cols
        series = df.to_dict("series")

        chart_data = []
        for name in df.T.index.tolist():
            ys = series[name]
            if df[name].dtype.kind not in "biufc":
                continue
            series_title: list[str] | str | tuple[str, ...]
            if isinstance(name, list):
                series_title = [str(title) for title in name]
            elif isinstance(name, tuple):
                series_title = tuple(str(title) for title in name)
            else:
                series_title = str(name)
            if (
                isinstance(series_title, (list, tuple))
                and len(series_title) > 1
                and len(self.metric_labels) == 1
            ):
                series_title = series_title[1:]
            if title_suffix:
                if isinstance(series_title, str):
                    series_title = (series_title, title_suffix)
                elif isinstance(series_title, list):
                    series_title = series_title + [title_suffix]
                elif isinstance(series_title, tuple):
                    series_title = series_title + (title_suffix,)

            values = []
            non_nan_cnt = 0
            for ds in df.index:
                if ds in ys:
                    data = {"x": ds, "y": ys[ds]}
                    if not np.isnan(ys[ds]):
                        non_nan_cnt += 1
                else:
                    data = {}
                values.append(data)

            if non_nan_cnt == 0:
                continue

            data_item = {"key": series_title, "values": values}
            if classed:
                data_item["classed"] = classed
            chart_data.append(data_item)
        return chart_data

    def process_data(self, df: pd.DataFrame, aggregate: bool = False) -> VizData:
        if df.empty:
            return df

        if aggregate:
            df = df.pivot_table(
                index=DTTM_ALIAS,
                columns=get_column_names(self.form_data.get("groupby")),
                values=self.metric_labels,
                fill_value=0,
                aggfunc=sum,
            )
        else:
            df = df.pivot_table(
                index=DTTM_ALIAS,
                columns=get_column_names(self.form_data.get("groupby")),
                values=self.metric_labels,
                fill_value=self.pivot_fill_value,
            )

        rule = self.form_data.get("resample_rule")
        method = self.form_data.get("resample_method")

        if rule and method:
            df = getattr(df.resample(rule), method)()

        if self.sort_series:
            dfs = df.sum()
            dfs.sort_values(ascending=False, inplace=True)
            df = df[dfs.index]

        df = self.apply_rolling(df)
        if self.form_data.get("contribution"):
            dft = df.T
            df = (dft / dft.sum()).T

        return df

    async def async_run_extra_queries(self) -> None:
        time_compare = self.form_data.get("time_compare") or []
        if not isinstance(time_compare, list):
            time_compare = [time_compare]

        for option in time_compare:
            query_object = self.query_obj()
            try:
                delta = parse_past_timedelta(option)
            except ValueError as ex:
                raise QueryObjectValidationError(str(ex)) from ex
            query_object["inner_from_dttm"] = query_object["from_dttm"]
            query_object["inner_to_dttm"] = query_object["to_dttm"]

            if not query_object["from_dttm"] or not query_object["to_dttm"]:
                raise QueryObjectValidationError(
                    "An enclosed time range (both start and end) must be specified "
                    "when using a Time Comparison."
                )
            query_object["from_dttm"] -= delta
            query_object["to_dttm"] -= delta

            df2 = (await self.get_df_payload(query_object, time_compare=option)).get(
                "df"
            )
            if df2 is not None and DTTM_ALIAS in df2:
                dttm_series = df2[DTTM_ALIAS] + delta
                df2 = df2.drop(DTTM_ALIAS, axis=1)
                df2 = pd.concat([dttm_series, df2], axis=1)
                label = f"{option} offset"
                df2 = self.process_data(df2)
                self._extra_chart_data.append((label, df2))

    def get_data(self, df: pd.DataFrame | None) -> VizData:
        if df is None or df.empty:
            return None
        comparison_type = self.form_data.get("comparison_type") or "values"
        df = self.process_data(df)
        if isinstance(df, pd.DataFrame) and df.empty:
            return None
        if comparison_type == "values":
            chart_data = self.to_series(df.dropna(axis=1, how="all"))

            for i, (label, df2) in enumerate(self._extra_chart_data):
                chart_data.extend(
                    self.to_series(df2, classed=f"time-shift-{i}", title_suffix=label)
                )
        else:
            chart_data = []
            for i, (label, df2) in enumerate(self._extra_chart_data):
                combined_index = df.index.union(df2.index)
                df2 = (
                    df2.reindex(combined_index)
                    .interpolate(method="time")
                    .reindex(df.index)
                )
                if comparison_type == "absolute":
                    diff = df - df2
                elif comparison_type == "percentage":
                    diff = (df - df2) / df2
                elif comparison_type == "ratio":
                    diff = df / df2
                else:
                    raise QueryObjectValidationError(
                        f"Invalid `comparison_type`: {comparison_type}"
                    )
                diff = diff[diff.first_valid_index() : diff.last_valid_index()]
                chart_data.extend(
                    self.to_series(diff, classed=f"time-shift-{i}", title_suffix=label)
                )

        if not self.sort_series:
            chart_data = sorted(chart_data, key=lambda x: tuple(x["key"]))
        return chart_data


# ---------------------------------------------------------------------------
# NVD3TimePivotViz
# ---------------------------------------------------------------------------
class NVD3TimePivotViz(NVD3TimeSeriesViz):
    viz_type = "time_pivot"
    sort_series = True
    verbose_name = "Time Series - Period Pivot"

    def query_obj(self) -> QueryObjectDict:
        query_obj = super().query_obj()
        query_obj["metrics"] = [self.form_data.get("metric")]
        return query_obj

    def get_data(self, df: pd.DataFrame | None) -> VizData:
        if df is None or df.empty:
            return None
        df = self.process_data(df)
        if isinstance(df, pd.DataFrame) and df.empty:
            return None
        freq = to_offset(self.form_data.get("freq"))
        try:
            freq = type(freq)(freq.n, normalize=True, **freq.kwds)
        except ValueError:
            freq = type(freq)(freq.n, **freq.kwds)
        df.index.name = None
        df[DTTM_ALIAS] = df.index.map(freq.rollback)
        df["ranked"] = df[DTTM_ALIAS].rank(method="dense", ascending=False) - 1
        df.ranked = df.ranked.map(int)
        df["series"] = "-" + df.ranked.map(str)
        df["series"] = df["series"].str.replace("-0", "current")
        rank_lookup = {
            row["series"]: row["ranked"] for row in df.to_dict(orient="records")
        }
        max_ts = df[DTTM_ALIAS].max()
        max_rank = df["ranked"].max()
        df[DTTM_ALIAS] = df.index + (max_ts - df[DTTM_ALIAS])
        df = df.pivot_table(
            index=DTTM_ALIAS,
            columns="series",
            values=get_metric_name(self.form_data["metric"]),
        )
        chart_data = self.to_series(df)
        for series in chart_data:
            series["rank"] = rank_lookup[series["key"]]
            series["perc"] = 1 - (series["rank"] / (max_rank + 1))
        return chart_data


# ---------------------------------------------------------------------------
# NVD3CompareTimeSeriesViz
# ---------------------------------------------------------------------------
class NVD3CompareTimeSeriesViz(NVD3TimeSeriesViz):
    viz_type = "compare"
    verbose_name = "Time Series - Percent Change"


# ---------------------------------------------------------------------------
# ChordViz
# ---------------------------------------------------------------------------
class ChordViz(BaseViz):
    viz_type = "chord"
    verbose_name = "Directed Force Layout"
    is_timeseries = False

    def query_obj(self) -> QueryObjectDict:
        query_obj = super().query_obj()
        query_obj["groupby"] = [
            self.form_data.get("groupby"),
            self.form_data.get("columns"),
        ]
        query_obj["metrics"] = [self.form_data.get("metric")]
        if self.form_data.get("sort_by_metric", False):
            query_obj["orderby"] = [(query_obj["metrics"][0], False)]
        return query_obj

    def get_data(self, df: pd.DataFrame | None) -> VizData:
        if df is None or df.empty:
            return None
        df.columns = ["source", "target", "value"]
        nodes = list(set(df["source"]) | set(df["target"]))
        matrix: dict[tuple[Any, Any], Any] = {}
        for source, target in product(nodes, nodes):
            matrix[(source, target)] = 0
        for source, target, value in df.to_records(index=False):
            matrix[(source, target)] = value
        return {
            "nodes": list(nodes),
            "matrix": [[matrix[(n1, n2)] for n1 in nodes] for n2 in nodes],
        }


# ---------------------------------------------------------------------------
# CountryMapViz
# ---------------------------------------------------------------------------
class CountryMapViz(BaseViz):
    viz_type = "country_map"
    verbose_name = "Country Map"
    is_timeseries = False

    def query_obj(self) -> QueryObjectDict:
        query_obj = super().query_obj()
        metric = self.form_data.get("metric")
        entity = self.form_data.get("entity")
        if not self.form_data.get("select_country"):
            raise QueryObjectValidationError("Must specify a country")
        if not metric:
            raise QueryObjectValidationError("Must specify a metric")
        if not entity:
            raise QueryObjectValidationError("Must provide ISO codes")
        query_obj["metrics"] = [metric]
        query_obj["groupby"] = [entity]
        return query_obj

    def get_data(self, df: pd.DataFrame | None) -> VizData:
        if df is None or df.empty:
            return None
        entity = self.form_data.get("entity") or ""
        cols = get_column_names([entity])
        metric = self.metric_labels[0]
        cols += [metric]
        ndf = df[cols]
        df = ndf
        df.columns = ["country_id", "metric"]
        return df.to_dict(orient="records")


# ---------------------------------------------------------------------------
# WorldMapViz
# ---------------------------------------------------------------------------
class WorldMapViz(BaseViz):
    viz_type = "world_map"
    verbose_name = "World Map"
    is_timeseries = False

    def query_obj(self) -> QueryObjectDict:
        query_obj = super().query_obj()
        query_obj["groupby"] = [self.form_data["entity"]]
        if self.form_data.get("sort_by_metric", False):
            query_obj["orderby"] = [(query_obj["metrics"][0], False)]
        return query_obj

    def get_data(self, df: pd.DataFrame | None) -> VizData:
        if df is None or df.empty:
            return None
        entity = self.form_data.get("entity") or ""
        cols = get_column_names([entity])
        metric = get_metric_name(self.form_data["metric"])
        secondary_metric = (
            get_metric_name(self.form_data["secondary_metric"])
            if self.form_data.get("secondary_metric")
            else None
        )
        columns = ["country", "m1", "m2"]
        if metric == secondary_metric:
            ndf = df[cols]
            ndf["m1"] = df[metric]
            ndf["m2"] = ndf["m1"]
        else:
            if secondary_metric:
                cols += [metric, secondary_metric]
            else:
                cols += [metric]
                columns = ["country", "m1"]
            ndf = df[cols]
        df = ndf
        df.columns = columns
        data = df.to_dict(orient="records")
        for row in data:
            country = None
            if isinstance(row["country"], str):
                if "country_fieldtype" in self.form_data:
                    try:
                        from superset.examples import countries

                        country = countries.get(
                            self.form_data["country_fieldtype"], row["country"]
                        )
                    except ImportError:
                        country = None
            if country:
                row["code"] = country[self.form_data["country_fieldtype"]]
                row["country"] = country["cca3"]
                row["latitude"] = country["lat"]
                row["longitude"] = country["lng"]
                row["name"] = country["name"]
            else:
                row["country"] = "XXX"
        return data


# ---------------------------------------------------------------------------
# ParallelCoordinatesViz
# ---------------------------------------------------------------------------
class ParallelCoordinatesViz(BaseViz):
    viz_type = "para"
    verbose_name = "Parallel Coordinates"
    is_timeseries = False

    def query_obj(self) -> QueryObjectDict:
        query_obj = super().query_obj()
        query_obj["groupby"] = [self.form_data.get("series")]
        if sort_by := self.form_data.get("timeseries_limit_metric"):
            sort_by_label = get_metric_name(sort_by)
            if sort_by_label not in get_metric_names(query_obj["metrics"]):
                query_obj["metrics"].append(sort_by)
            if self.form_data.get("order_desc"):
                query_obj["orderby"] = [
                    (sort_by, not self.form_data.get("order_desc", True))
                ]
        return query_obj

    def get_data(self, df: pd.DataFrame | None) -> VizData:
        if df is None or df.empty:
            return None
        return df.to_dict(orient="records")


# ---------------------------------------------------------------------------
# HorizonViz
# ---------------------------------------------------------------------------
class HorizonViz(NVD3TimeSeriesViz):
    viz_type = "horizon"
    verbose_name = "Horizon Charts"


# ---------------------------------------------------------------------------
# MapboxViz
# ---------------------------------------------------------------------------
class MapboxViz(BaseViz):
    viz_type = "mapbox"
    verbose_name = "Mapbox"
    is_timeseries = False

    def query_obj(self) -> QueryObjectDict:
        query_obj = super().query_obj()
        label_col = self.form_data.get("mapbox_label")

        if not self.form_data.get("groupby"):
            if (
                self.form_data.get("all_columns_x") is None
                or self.form_data.get("all_columns_y") is None
            ):
                raise QueryObjectValidationError(
                    "[Longitude] and [Latitude] must be set"
                )
            query_obj["columns"] = [
                self.form_data.get("all_columns_x"),
                self.form_data.get("all_columns_y"),
            ]

            if label_col and len(label_col) >= 1:
                if label_col[0] == "count":
                    raise QueryObjectValidationError(
                        "Must have a [Group By] column to have 'count' as the [Label]"
                    )
                query_obj["columns"].append(label_col[0])

            if self.form_data.get("point_radius") != "Auto":
                query_obj["columns"].append(self.form_data.get("point_radius"))

            query_obj["columns"] = sorted(set(query_obj["columns"]))
        else:
            if (
                label_col
                and len(label_col) >= 1
                and label_col[0] != "count"
                and label_col[0] not in self.form_data["groupby"]
            ):
                raise QueryObjectValidationError(
                    "Choice of [Label] must be present in [Group By]"
                )
            if (
                self.form_data.get("point_radius") != "Auto"
                and self.form_data.get("point_radius") not in self.form_data["groupby"]
            ):
                raise QueryObjectValidationError(
                    "Choice of [Point Radius] must be present in [Group By]"
                )
            if (
                self.form_data.get("all_columns_x") not in self.form_data["groupby"]
                or self.form_data.get("all_columns_y") not in self.form_data["groupby"]
            ):
                raise QueryObjectValidationError(
                    "[Longitude] and [Latitude] columns must be present in [Group By]"
                )
        return query_obj

    def get_data(self, df: pd.DataFrame | None) -> VizData:
        if df is None or df.empty:
            return None
        label_col: list[Any] | None = self.form_data.get("mapbox_label")
        has_custom_metric = label_col is not None and len(label_col) > 0
        metric_col = [None] * len(df.index)
        if has_custom_metric and label_col is not None:
            if label_col[0] == self.form_data.get("all_columns_x"):
                metric_col = df[self.form_data.get("all_columns_x")]
            elif label_col[0] == self.form_data.get("all_columns_y"):
                metric_col = df[self.form_data.get("all_columns_y")]
            else:
                metric_col = df[label_col[0]]
        point_radius_col = (
            [None] * len(df.index)
            if self.form_data.get("point_radius") == "Auto"
            else df[self.form_data.get("point_radius")]
        )

        geo_precision = 10
        geo_json = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"metric": metric, "radius": point_radius},
                    "geometry": {
                        "type": "Point",
                        "coordinates": [
                            round(lon, geo_precision),
                            round(lat, geo_precision),
                        ],
                    },
                }
                for lon, lat, metric, point_radius in zip(
                    df[self.form_data.get("all_columns_x")],
                    df[self.form_data.get("all_columns_y")],
                    metric_col,
                    point_radius_col,
                    strict=False,
                )
            ],
        }

        x_series, y_series = (
            df[self.form_data.get("all_columns_x")],
            df[self.form_data.get("all_columns_y")],
        )
        south_west = [x_series.min(), y_series.min()]
        north_east = [x_series.max(), y_series.max()]

        mapbox_api_key = self._get_setting("mapbox_api_key", "")
        return {
            "geoJSON": geo_json,
            "hasCustomMetric": has_custom_metric,
            "mapboxApiKey": mapbox_api_key,
            "mapStyle": self.form_data.get("mapbox_style"),
            "aggregatorName": self.form_data.get("pandas_aggfunc"),
            "clusteringRadius": self.form_data.get("clustering_radius"),
            "pointRadiusUnit": self.form_data.get("point_radius_unit"),
            "globalOpacity": self.form_data.get("global_opacity"),
            "bounds": [south_west, north_east],
            "renderWhileDragging": self.form_data.get("render_while_dragging"),
            "tooltip": self.form_data.get("rich_tooltip"),
            "color": self.form_data.get("mapbox_color"),
        }


# ---------------------------------------------------------------------------
# DeckGLMultiLayer
# ---------------------------------------------------------------------------
class DeckGLMultiLayer(BaseViz):
    viz_type = "deck_multi"
    verbose_name = "Deck.gl - Multiple Layers"
    is_timeseries = False

    def query_obj(self) -> QueryObjectDict:
        return {}

    def _filter_items_by_scope(
        self,
        items: list[Any],
        layer_index: int,
        layer_filter_scope: dict[str, list[int]],
    ) -> list[Any]:
        filtered_items = []
        for filter_item in items:
            filter_id = getattr(filter_item, "filterId", None)
            if filter_id:
                filter_scope = layer_filter_scope.get(filter_id, [])
                if filter_scope is None:
                    filter_scope = []
                if not filter_scope or layer_index in filter_scope:
                    filtered_items.append(filter_item)
            else:
                filtered_items.append(filter_item)
        return filtered_items

    def _process_extra_form_data_filters(
        self,
        layer_index: int,
        layer_filter_scope: dict[str, list[int]],
        filter_data_mapping: dict[str, list[Any]],
        extra_form_data: dict[str, Any],
    ) -> dict[str, Any]:
        if not extra_form_data or not filter_data_mapping:
            return extra_form_data

        filtered_extra_form_data_filters = []
        for filter_id, filter_scope in layer_filter_scope.items():
            if filter_scope is None:
                filter_scope = []
            if not filter_scope or layer_index in filter_scope:
                filters_from_this_filter = filter_data_mapping.get(filter_id, [])
                filtered_extra_form_data_filters.extend(filters_from_this_filter)

        return {
            **extra_form_data,
            "filters": filtered_extra_form_data_filters,
        }

    def _apply_layer_filtering(
        self, form_data: dict[str, Any], layer_index: int
    ) -> dict[str, Any]:
        layer_filter_scope = self.form_data.get("layer_filter_scope", {})
        filter_data_mapping = self.form_data.get("filter_data_mapping", {})

        if not layer_filter_scope:
            form_data["extra_filters"] = self.form_data.get("extra_filters", [])
            form_data["adhoc_filters"] = self.form_data.get("adhoc_filters")
            form_data["extra_form_data"] = self.form_data.get("extra_form_data")
            return form_data

        filtered_extra_filters = self._filter_items_by_scope(
            self.form_data.get("extra_filters", []), layer_index, layer_filter_scope
        )
        filtered_adhoc_filters = self._filter_items_by_scope(
            self.form_data.get("adhoc_filters", []), layer_index, layer_filter_scope
        )

        extra_form_data = self.form_data.get("extra_form_data", {})
        filtered_extra_form_data = self._process_extra_form_data_filters(
            layer_index, layer_filter_scope, filter_data_mapping, extra_form_data
        )

        form_data["extra_filters"] = filtered_extra_filters
        form_data["adhoc_filters"] = filtered_adhoc_filters
        form_data["extra_form_data"] = filtered_extra_form_data
        return form_data

    def get_data(self, df: pd.DataFrame | None) -> VizData:
        """Synchronous fallback — DeckGLMultiLayer needs async for sub-layers."""
        return {"features": {}, "mapboxApiKey": "", "slices": []}

    async def get_payload(self, query_obj: QueryObjectDict | None = None) -> VizPayload:
        """Override to call async_get_data with a DB session."""

        from superset.db.session import create_session_factory, get_engine

        engine = get_engine()
        session_factory = create_session_factory(engine)
        async with session_factory() as session:
            self.data = await self.async_get_data(None, session)

        return {
            "data": self.data,
            "applied_filters": [],
            "rejected_filters": [],
        }

    async def async_get_data(self, df: pd.DataFrame | None, session: Any) -> VizData:
        """Async version that can load sub-layer slices from DB."""
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        from superset.models.connectors import SqlaTable
        from superset.models.slice import Slice

        slice_ids = self.form_data.get("deck_slices")
        if not slice_ids:
            mapbox_api_key = self._get_setting("mapbox_api_key", "")
            return {"features": {}, "mapboxApiKey": mapbox_api_key, "slices": []}

        stmt = (
            select(Slice)
            .where(Slice.id.in_(slice_ids))
            .options(selectinload(Slice.owners))
        )
        result = await session.execute(stmt)
        slices = result.scalars().all()

        features: dict[str, list[Any]] = {}

        for layer_index, slc in enumerate(slices):
            slice_form_data = slc.form_data
            slice_form_data = self._apply_layer_filtering(slice_form_data, layer_index)

            viz_type_name = slice_form_data.get("viz_type")
            viz_class = viz_types.get(viz_type_name)
            if not viz_class:
                continue

            # Load datasource for this slice
            ds_stmt = (
                select(SqlaTable)
                .where(SqlaTable.id == slc.datasource_id)
                .options(
                    selectinload(SqlaTable.database),
                    selectinload(SqlaTable.columns),
                    selectinload(SqlaTable.metrics),
                )
            )
            ds_result = await session.execute(ds_stmt)
            ds = ds_result.scalars().one_or_none()
            if not ds:
                continue

            viz_instance = viz_class(
                datasource=ds,
                form_data=slice_form_data,
                settings=self.settings,
            )
            payload = await viz_instance.get_payload()

            if (
                payload
                and "data" in payload
                and payload["data"] is not None
                and "features" in payload["data"]
            ):
                vt_key: str = str(viz_type_name) if viz_type_name is not None else ""
                if vt_key not in features:
                    features[vt_key] = []
                features[vt_key].extend(payload["data"]["features"])

        mapbox_api_key = self._get_setting("mapbox_api_key", "")
        return {
            "features": features,
            "mapboxApiKey": mapbox_api_key,
            "slices": [slc.data for slc in slices if slc.data is not None],
        }


# ---------------------------------------------------------------------------
# BaseDeckGLViz
# ---------------------------------------------------------------------------
class BaseDeckGLViz(BaseViz):
    is_timeseries = False
    credits = '<a href="https://uber.github.io/deck.gl/">deck.gl</a>'
    spatial_control_keys: list[str] = []

    def __init__(
        self, datasource: SqlaTable, form_data: dict[str, Any], **kwargs: Any
    ) -> None:
        if self._should_apply_layer_filtering(form_data):
            form_data = self._apply_multilayer_filtering(form_data)
        super().__init__(datasource, form_data, **kwargs)

    def _should_apply_layer_filtering(self, form_data: dict[str, Any]) -> bool:
        return (
            "slice_id" in form_data
            and "adhoc_filters" in form_data
            and self._has_layer_scoped_filters(form_data)
        )

    def _has_layer_scoped_filters(self, form_data: dict[str, Any]) -> bool:
        for filter_item in form_data.get("adhoc_filters", []):
            if (
                isinstance(filter_item, dict)
                and filter_item.get("layerFilterScope") is not None
            ):
                return True
        return False

    def _apply_multilayer_filtering(self, form_data: dict[str, Any]) -> dict[str, Any]:
        slice_id = form_data.get("slice_id")
        deck_slices = self._get_deck_slices_from_filters(form_data)

        if not deck_slices or slice_id not in deck_slices:
            return form_data

        layer_index = deck_slices.index(slice_id)
        filtered_adhoc_filters = []

        for filter_item in form_data.get("adhoc_filters", []):
            layer_scope = self._get_filter_layer_scope(filter_item)
            if layer_scope is None or layer_index in layer_scope:
                filtered_adhoc_filters.append(filter_item)

        modified_form_data = form_data.copy()
        modified_form_data["adhoc_filters"] = filtered_adhoc_filters
        return modified_form_data

    def _get_deck_slices_from_filters(
        self, form_data: dict[str, Any]
    ) -> list[int] | None:
        for filter_item in form_data.get("adhoc_filters", []):
            if isinstance(filter_item, dict) and "deck_slices" in filter_item:
                return filter_item["deck_slices"]
        return None

    def _get_filter_layer_scope(self, filter_item: Any) -> list[int] | None:
        if isinstance(filter_item, dict):
            return filter_item.get("layerFilterScope")
        return getattr(filter_item, "layerFilterScope", None)

    def get_metrics(self) -> list[str]:
        self.metric = self.form_data.get("size")
        return [self.metric] if self.metric else []

    def process_spatial_query_obj(self, key: str, group_by: list[str]) -> None:
        group_by.extend(self.get_spatial_columns(key))

    def get_spatial_columns(self, key: str) -> list[str]:
        spatial = self.form_data.get(key)
        if spatial is None:
            raise ValueError("Bad spatial key")

        if spatial.get("type") == "latlong":
            return [spatial.get("lonCol"), spatial.get("latCol")]
        if spatial.get("type") == "delimited":
            return [spatial.get("lonlatCol")]
        if spatial.get("type") == "geohash":
            return [spatial.get("geohashCol")]
        return []

    @staticmethod
    def parse_coordinates(latlong: Any) -> tuple[float, float] | None:
        if not latlong:
            return None
        try:
            point = Point(latlong)
            return (point.latitude, point.longitude)
        except Exception as ex:
            raise SpatialException(
                f"Invalid spatial point encountered: {latlong}"
            ) from ex

    @staticmethod
    def reverse_geohash_decode(geohash_code: str) -> tuple[str, str]:
        lat, lng = geohash.decode(geohash_code)
        return (lng, lat)

    @staticmethod
    def reverse_latlong(df: pd.DataFrame, key: str) -> None:
        df[key] = [tuple(reversed(o)) for o in df[key] if isinstance(o, (list, tuple))]

    def process_spatial_data_obj(self, key: str, df: pd.DataFrame) -> pd.DataFrame:
        spatial = self.form_data.get(key)
        if spatial is None:
            raise ValueError("Bad spatial key")

        if spatial.get("type") == "latlong":
            df[key] = list(
                zip(
                    pd.to_numeric(df[spatial.get("lonCol")], errors="coerce"),
                    pd.to_numeric(df[spatial.get("latCol")], errors="coerce"),
                    strict=False,
                )
            )
        elif spatial.get("type") == "delimited":
            lon_lat_col = spatial.get("lonlatCol")
            df[key] = df[lon_lat_col].apply(self.parse_coordinates)
            del df[lon_lat_col]
        elif spatial.get("type") == "geohash":
            df[key] = df[spatial.get("geohashCol")].map(self.reverse_geohash_decode)
            del df[spatial.get("geohashCol")]

        if spatial.get("reverseCheckbox"):
            self.reverse_latlong(df, key)

        if df.get(key) is None:
            raise NullValueException(
                "Encountered invalid NULL spatial entry, "
                "please consider filtering those out"
            )
        return df

    def add_null_filters(self) -> None:
        spatial_columns: set[str] = set()
        for key in self.spatial_control_keys:
            for column in self.get_spatial_columns(key):
                spatial_columns.add(column)

        if self.form_data.get("adhoc_filters") is None:
            self.form_data["adhoc_filters"] = []

        if line_column := self.form_data.get("line_column"):
            spatial_columns.add(line_column)

        for column in sorted(spatial_columns):
            filter_ = simple_filter_to_adhoc(
                {"col": column, "op": "IS NOT NULL", "val": ""}
            )
            self.form_data["adhoc_filters"].append(filter_)

    def query_obj(self) -> QueryObjectDict:
        if self.form_data.get("filter_nulls", True):
            self.add_null_filters()

        query_obj = super().query_obj()
        group_by: list[str] = []

        for key in self.spatial_control_keys:
            self.process_spatial_query_obj(key, group_by)

        if self.form_data.get("dimension"):
            group_by += [self.form_data["dimension"]]

        if self.form_data.get("js_columns"):
            group_by += self.form_data.get("js_columns") or []

        group_by = sorted(set(group_by))
        if metrics := self.get_metrics():
            query_obj["groupby"] = group_by
            query_obj["metrics"] = metrics
            query_obj["columns"] = []
            first_metric = query_obj["metrics"][0]
            query_obj["orderby"] = [
                (first_metric, not self.form_data.get("order_desc", True))
            ]
        else:
            query_obj["columns"] = group_by
        return query_obj

    def get_js_columns(self, data: dict[str, Any]) -> dict[str, Any]:
        cols = self.form_data.get("js_columns") or []
        return {col: data.get(col) for col in cols}

    def get_data(self, df: pd.DataFrame | None) -> VizData:
        if df is None or df.empty:
            return None

        for key in self.spatial_control_keys:
            df = self.process_spatial_data_obj(key, df)

        features = []
        for data in df.to_dict(orient="records"):
            feature = self.get_properties(data)
            extra_props = self.get_js_columns(data)
            if extra_props:
                feature["extraProps"] = extra_props
            features.append(feature)

        mapbox_api_key = self._get_setting("mapbox_api_key", "")
        return {
            "features": features,
            "mapboxApiKey": mapbox_api_key,
            "metricLabels": self.metric_labels,
        }

    def get_properties(self, data: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError()


# ---------------------------------------------------------------------------
# DeckScatterViz
# ---------------------------------------------------------------------------
class DeckScatterViz(BaseDeckGLViz):
    viz_type = "deck_scatter"
    verbose_name = "Deck.gl - Scatter plot"
    spatial_control_keys = ["spatial"]
    is_timeseries = True

    def query_obj(self) -> QueryObjectDict:
        self.is_timeseries = bool(self.form_data.get("time_grain_sqla"))
        self.point_radius_fixed = self.form_data.get("point_radius_fixed") or {
            "type": "fix",
            "value": 500,
        }
        return super().query_obj()

    def get_metrics(self) -> list[str]:
        self.metric = None
        if self.point_radius_fixed.get("type") == "metric":
            self.metric = self.point_radius_fixed["value"]
            return [self.metric]
        return []

    def get_properties(self, data: dict[str, Any]) -> dict[str, Any]:
        return {
            "metric": data.get(self.metric_label) if self.metric_label else None,
            "radius": (
                self.fixed_value
                if self.fixed_value
                else data.get(self.metric_label)
                if self.metric_label
                else None
            ),
            "cat_color": data.get(self.dim) if self.dim else None,
            "position": data.get("spatial"),
            DTTM_ALIAS: data.get(DTTM_ALIAS),
        }

    def get_data(self, df: pd.DataFrame | None) -> VizData:
        if df is None or df.empty:
            return None
        self.metric_label = get_metric_name(self.metric) if self.metric else None
        self.point_radius_fixed = self.form_data.get("point_radius_fixed")
        self.fixed_value = None
        self.dim = self.form_data.get("dimension")
        if self.point_radius_fixed and self.point_radius_fixed.get("type") != "metric":
            self.fixed_value = self.point_radius_fixed.get("value")
        return super().get_data(df)


# ---------------------------------------------------------------------------
# DeckScreengrid
# ---------------------------------------------------------------------------
class DeckScreengrid(BaseDeckGLViz):
    viz_type = "deck_screengrid"
    verbose_name = "Deck.gl - Screen Grid"
    spatial_control_keys = ["spatial"]
    is_timeseries = True

    def query_obj(self) -> QueryObjectDict:
        self.is_timeseries = bool(self.form_data.get("time_grain_sqla"))
        return super().query_obj()

    def get_properties(self, data: dict[str, Any]) -> dict[str, Any]:
        return {
            "position": data.get("spatial"),
            "weight": (data.get(self.metric_label) if self.metric_label else None) or 1,
            "__timestamp": data.get(DTTM_ALIAS) or data.get("__time"),
        }

    def get_data(self, df: pd.DataFrame | None) -> VizData:
        if df is None or df.empty:
            return None
        self.metric_label = get_metric_name(self.metric) if self.metric else None
        return super().get_data(df)


# ---------------------------------------------------------------------------
# DeckGrid
# ---------------------------------------------------------------------------
class DeckGrid(BaseDeckGLViz):
    viz_type = "deck_grid"
    verbose_name = "Deck.gl - 3D Grid"
    spatial_control_keys = ["spatial"]

    def get_properties(self, data: dict[str, Any]) -> dict[str, Any]:
        return {
            "position": data.get("spatial"),
            "weight": (data.get(self.metric_label) if self.metric_label else None) or 1,
        }

    def get_data(self, df: pd.DataFrame | None) -> VizData:
        if df is None or df.empty:
            return None
        self.metric_label = get_metric_name(self.metric) if self.metric else None
        return super().get_data(df)


# ---------------------------------------------------------------------------
# geohash_to_json helper
# ---------------------------------------------------------------------------
def geohash_to_json(geohash_code: str) -> list[list[float]]:
    bbox = geohash.bbox(geohash_code)
    return [
        [bbox.get("w"), bbox.get("n")],
        [bbox.get("e"), bbox.get("n")],
        [bbox.get("e"), bbox.get("s")],
        [bbox.get("w"), bbox.get("s")],
        [bbox.get("w"), bbox.get("n")],
    ]


# ---------------------------------------------------------------------------
# DeckPathViz
# ---------------------------------------------------------------------------
class DeckPathViz(BaseDeckGLViz):
    viz_type = "deck_path"
    verbose_name = "Deck.gl - Paths"
    deck_viz_key = "path"
    is_timeseries = True
    deser_map: dict[str, Any] = {
        "json": stdlib_json.loads,
        "polyline": polyline.decode,
        "geohash": geohash_to_json,
    }

    def query_obj(self) -> QueryObjectDict:
        self.is_timeseries = bool(self.form_data.get("time_grain_sqla"))
        query_obj = super().query_obj()
        self.metric = self.form_data.get("metric")
        line_col = self.form_data.get("line_column")
        if query_obj["metrics"]:
            self.has_metrics = True
            query_obj["groupby"].append(line_col)
        else:
            self.has_metrics = False
            query_obj["columns"].append(line_col)
        return query_obj

    def get_properties(self, data: dict[str, Any]) -> dict[str, Any]:
        line_type = self.form_data["line_type"]
        deser = self.deser_map[line_type]
        line_column = self.form_data["line_column"]
        path = deser(data[line_column])
        if self.form_data.get("reverse_long_lat"):
            path = [(o[1], o[0]) for o in path]
        data[self.deck_viz_key] = path
        if line_type != "geohash":
            del data[line_column]
        data["__timestamp"] = data.get(DTTM_ALIAS) or data.get("__time")
        return data

    def get_data(self, df: pd.DataFrame | None) -> VizData:
        if df is None or df.empty:
            return None
        self.metric_label = get_metric_name(self.metric) if self.metric else None
        return super().get_data(df)


# ---------------------------------------------------------------------------
# DeckPolygon
# ---------------------------------------------------------------------------
class DeckPolygon(DeckPathViz):
    viz_type = "deck_polygon"
    deck_viz_key = "polygon"
    verbose_name = "Deck.gl - Polygon"

    def query_obj(self) -> QueryObjectDict:
        self.elevation = self.form_data.get("point_radius_fixed") or {
            "type": "fix",
            "value": 500,
        }
        return super().query_obj()

    def get_metrics(self) -> list[str]:
        metrics = [self.form_data.get("metric")]
        if self.elevation.get("type") == "metric":
            metrics.append(self.elevation.get("value"))
        return [metric for metric in metrics if metric]

    def get_properties(self, data: dict[str, Any]) -> dict[str, Any]:
        super().get_properties(data)
        elevation = self.form_data["point_radius_fixed"]["value"]
        type_ = self.form_data["point_radius_fixed"]["type"]
        data["elevation"] = (
            data.get(get_metric_name(elevation)) if type_ == "metric" else elevation
        )
        return data


# ---------------------------------------------------------------------------
# DeckHex
# ---------------------------------------------------------------------------
class DeckHex(BaseDeckGLViz):
    viz_type = "deck_hex"
    verbose_name = "Deck.gl - 3D HEX"
    spatial_control_keys = ["spatial"]

    def get_properties(self, data: dict[str, Any]) -> dict[str, Any]:
        return {
            "position": data.get("spatial"),
            "weight": (data.get(self.metric_label) if self.metric_label else None) or 1,
        }

    def get_data(self, df: pd.DataFrame | None) -> VizData:
        if df is None or df.empty:
            return None
        self.metric_label = get_metric_name(self.metric) if self.metric else None
        return super().get_data(df)


# ---------------------------------------------------------------------------
# DeckHeatmap
# ---------------------------------------------------------------------------
class DeckHeatmap(BaseDeckGLViz):
    viz_type = "deck_heatmap"
    verbose_name = "Deck.gl - Heatmap"
    spatial_control_keys = ["spatial"]

    def get_properties(self, data: dict[str, Any]) -> dict[str, Any]:
        return {
            "position": data.get("spatial"),
            "weight": (data.get(self.metric_label) if self.metric_label else None) or 1,
        }

    def get_data(self, df: pd.DataFrame | None) -> VizData:
        if df is None or df.empty:
            return None
        self.metric_label = get_metric_name(self.metric) if self.metric else None
        return super().get_data(df)


# ---------------------------------------------------------------------------
# DeckContour
# ---------------------------------------------------------------------------
class DeckContour(BaseDeckGLViz):
    viz_type = "deck_contour"
    verbose_name = "Deck.gl - Contour"
    spatial_control_keys = ["spatial"]

    def get_properties(self, data: dict[str, Any]) -> dict[str, Any]:
        return {
            "position": data.get("spatial"),
            "weight": (data.get(self.metric_label) if self.metric_label else None) or 1,
        }

    def get_data(self, df: pd.DataFrame | None) -> VizData:
        if df is None or df.empty:
            return None
        self.metric_label = get_metric_name(self.metric) if self.metric else None
        return super().get_data(df)


# ---------------------------------------------------------------------------
# DeckGeoJson
# ---------------------------------------------------------------------------
class DeckGeoJson(BaseDeckGLViz):
    viz_type = "deck_geojson"
    verbose_name = "Deck.gl - GeoJSON"

    def query_obj(self) -> QueryObjectDict:
        query_obj = super().query_obj()
        query_obj["columns"] += [self.form_data.get("geojson")]
        query_obj["metrics"] = []
        query_obj["groupby"] = []
        return query_obj

    def get_properties(self, data: dict[str, Any]) -> dict[str, Any]:
        geojson = data[get_column_name(self.form_data["geojson"])]
        return stdlib_json.loads(geojson)


# ---------------------------------------------------------------------------
# DeckArc
# ---------------------------------------------------------------------------
class DeckArc(BaseDeckGLViz):
    viz_type = "deck_arc"
    verbose_name = "Deck.gl - Arc"
    spatial_control_keys = ["start_spatial", "end_spatial"]
    is_timeseries = True

    def query_obj(self) -> QueryObjectDict:
        self.is_timeseries = bool(self.form_data.get("time_grain_sqla"))
        return super().query_obj()

    def get_properties(self, data: dict[str, Any]) -> dict[str, Any]:
        dim = self.form_data.get("dimension")
        return {
            "sourcePosition": data.get("start_spatial"),
            "targetPosition": data.get("end_spatial"),
            "cat_color": data.get(dim) if dim else None,
            DTTM_ALIAS: data.get(DTTM_ALIAS),
        }

    def get_data(self, df: pd.DataFrame | None) -> VizData:
        if df is None or df.empty:
            return None
        parent_data = super().get_data(df)
        if parent_data is None:
            return None
        mapbox_api_key = self._get_setting("mapbox_api_key", "")
        return {
            "features": parent_data["features"],
            "mapboxApiKey": mapbox_api_key,
        }


# ---------------------------------------------------------------------------
# EventFlowViz
# ---------------------------------------------------------------------------
class EventFlowViz(BaseViz):
    viz_type = "event_flow"
    verbose_name = "Event flow"
    is_timeseries = True

    def query_obj(self) -> QueryObjectDict:
        query = super().query_obj()
        form_data = self.form_data

        event_key = form_data["all_columns_x"]
        entity_key = form_data["entity"]
        meta_keys = [
            col
            for col in form_data["all_columns"] or []
            if col not in (event_key, entity_key)
        ]

        query["columns"] = [event_key, entity_key] + meta_keys

        if form_data["order_by_entity"]:
            query["orderby"] = [(entity_key, True)]

        return query

    def get_data(self, df: pd.DataFrame | None) -> VizData:
        if df is None or df.empty:
            return None
        return df.to_dict(orient="records")


# ---------------------------------------------------------------------------
# PairedTTestViz
# ---------------------------------------------------------------------------
class PairedTTestViz(BaseViz):
    viz_type = "paired_ttest"
    verbose_name = "Time Series - Paired t-test"
    sort_series = False
    is_timeseries = True

    def query_obj(self) -> QueryObjectDict:
        query_obj = super().query_obj()
        if sort_by := self.form_data.get("timeseries_limit_metric"):
            sort_by_label = get_metric_name(sort_by)
            if sort_by_label not in get_metric_names(query_obj["metrics"]):
                query_obj["metrics"].append(sort_by)
            if self.form_data.get("order_desc"):
                query_obj["orderby"] = [
                    (sort_by, not self.form_data.get("order_desc", True))
                ]
        return query_obj

    def get_data(self, df: pd.DataFrame | None) -> VizData:
        if df is None or df.empty:
            return None

        groups = get_column_names(self.form_data.get("groupby"))
        metrics = self.metric_labels
        df = df.pivot_table(index=DTTM_ALIAS, columns=groups, values=metrics)
        cols = []
        for col in df.columns:
            if col == "":
                cols.append("N/A")
            elif col is None:
                cols.append("NULL")
            else:
                cols.append(col)
        df.columns = cols
        data: dict[str, list[dict[str, Any]]] = {}
        series = df.to_dict("series")
        for name_set in df.columns:
            has_group = not isinstance(name_set, str)
            data_ = {
                "group": name_set[1:] if has_group else "All",
                "values": [
                    {
                        "x": t,
                        "y": series[name_set][t] if t in series[name_set] else None,
                    }
                    for t in df.index
                ],
            }
            key = name_set[0] if has_group else name_set
            if key in data:
                data[key].append(data_)
            else:
                data[key] = [data_]
        return data


# ---------------------------------------------------------------------------
# RoseViz
# ---------------------------------------------------------------------------
class RoseViz(NVD3TimeSeriesViz):
    viz_type = "rose"
    verbose_name = "Time Series - Nightingale Rose Chart"
    sort_series = False
    is_timeseries = True

    def get_data(self, df: pd.DataFrame | None) -> VizData:
        if df is None or df.empty:
            return None
        data = super().get_data(df)
        if data is None:
            return None
        result: dict[str, list[dict[str, Any]]] = {}
        for datum in data:
            key = datum["key"]
            for val in datum["values"]:
                timestamp = val["x"].value
                if not result.get(timestamp):
                    result[timestamp] = []
                value = 0 if math.isnan(val["y"]) else val["y"]
                result[timestamp].append(
                    {
                        "key": key,
                        "value": value,
                        "name": ", ".join(key) if isinstance(key, list) else key,
                        "time": val["x"],
                    }
                )
        return result


# ---------------------------------------------------------------------------
# PartitionViz
# ---------------------------------------------------------------------------
class PartitionViz(NVD3TimeSeriesViz):
    viz_type = "partition"
    verbose_name = "Partition Diagram"

    def query_obj(self) -> QueryObjectDict:
        query_obj = super().query_obj()
        time_op = self.form_data.get("time_series_option", "not_time")
        query_obj["is_timeseries"] = time_op != "not_time"
        return query_obj

    @staticmethod
    def levels_for(
        time_op: str, groups: list[str], df: pd.DataFrame
    ) -> dict[int, pd.Series]:
        levels = {}
        for i in range(0, len(groups) + 1):
            agg_df = df.groupby(groups[:i]) if i else df
            levels[i] = (
                agg_df.mean(numeric_only=True)
                if time_op == "agg_mean"
                else agg_df.sum(numeric_only=True)
            )
        return levels

    @staticmethod
    def levels_for_diff(
        time_op: str, groups: list[str], df: pd.DataFrame
    ) -> dict[int, pd.DataFrame]:
        times = list(set(df[DTTM_ALIAS]))
        times.sort()
        until = times[len(times) - 1]
        since = times[0]
        func = {
            "point_diff": [pd.Series.sub, lambda a, b, fill_value: a - b],
            "point_factor": [pd.Series.div, lambda a, b, fill_value: a / float(b)],
            "point_percent": [
                lambda a, b, fill_value=0: a.div(b, fill_value=fill_value) - 1,  # type: ignore[misc]
                lambda a, b, fill_value: a / float(b) - 1,
            ],
        }[time_op]
        agg_df = df.groupby(DTTM_ALIAS).sum(numeric_only=True)
        levels = {
            0: pd.Series(
                {
                    m: func[1](agg_df[m][until], agg_df[m][since], 0)
                    for m in agg_df.columns
                }
            )
        }
        for i in range(1, len(groups) + 1):
            agg_df = df.groupby([DTTM_ALIAS] + groups[:i]).sum(numeric_only=True)
            levels[i] = pd.DataFrame(
                {
                    m: func[0](agg_df[m][until], agg_df[m][since], fill_value=0)
                    for m in agg_df.columns
                }
            )
        return levels

    def levels_for_time(
        self, groups: list[str], df: pd.DataFrame
    ) -> dict[int, VizData]:
        procs = {}
        for i in range(0, len(groups) + 1):
            self.form_data["groupby"] = groups[:i]
            df_drop = df.drop(groups[i:], axis=1)
            procs[i] = self.process_data(df_drop, aggregate=True)
        self.form_data["groupby"] = groups
        return procs

    def nest_values(
        self,
        levels: dict[int, pd.DataFrame],
        level: int = 0,
        metric: str | None = None,
        dims: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        if dims is None:
            dims = []
        if not level:
            return [
                {
                    "name": m,
                    "val": levels[0][m],
                    "children": self.nest_values(levels, 1, m),
                }
                for m in levels[0].index
            ]
        if level == 1:
            metric_level = levels[1][metric]
            return [
                {
                    "name": i,
                    "val": metric_level[i],
                    "children": self.nest_values(levels, 2, metric, [i]),
                }
                for i in metric_level.index
            ]
        if level >= len(levels):
            return []

        dim_level = levels[level][metric]
        for d in dims:
            if d not in dim_level:
                return []
            dim_level = dim_level[d]

        return [
            {
                "name": [*dims, i],
                "val": dim_level[i],
                "children": self.nest_values(levels, level + 1, metric, dims + [i]),
            }
            for i in dim_level.index
        ]

    def nest_procs(
        self,
        procs: dict[int, pd.DataFrame],
        level: int = -1,
        dims: tuple[str, ...] | None = None,
        time: Any = None,
    ) -> list[dict[str, Any]]:
        if dims is None:
            dims = ()
        if level == -1:
            return [
                {"name": m, "children": self.nest_procs(procs, 0, (m,))}
                for m in procs[0].columns
            ]
        if not level:
            return [
                {
                    "name": t,
                    "val": procs[0][dims[0]][t],
                    "children": self.nest_procs(procs, 1, dims, t),
                }
                for t in procs[0].index
            ]
        if level >= len(procs):
            return []
        return [
            {
                "name": i,
                "val": procs[level][dims][i][time],
                "children": self.nest_procs(procs, level + 1, dims + (i,), time),
            }
            for i in procs[level][dims].columns
        ]

    def get_data(self, df: pd.DataFrame | None) -> VizData:
        if df is None or df.empty:
            return None
        groups = get_column_names(self.form_data.get("groupby"))
        time_op = self.form_data.get("time_series_option", "not_time")
        if not groups:
            raise ValueError("Please choose at least one groupby")
        if time_op == "not_time":
            levels = self.levels_for("agg_sum", groups, df)
        elif time_op in ["agg_sum", "agg_mean"]:
            levels = self.levels_for(time_op, groups, df)
        elif time_op in ["point_diff", "point_factor", "point_percent"]:
            levels = self.levels_for_diff(time_op, groups, df)
        elif time_op == "adv_anal":
            procs = self.levels_for_time(groups, df)
            return self.nest_procs(procs)
        else:
            levels = self.levels_for("agg_sum", [DTTM_ALIAS] + groups, df)
        return self.nest_values(levels)


# ---------------------------------------------------------------------------
# viz_types registry — built by collecting all BaseViz subclasses
# ---------------------------------------------------------------------------
def _get_subclasses(cls: type[BaseViz]) -> set[type[BaseViz]]:
    return set(cls.__subclasses__()).union(
        [sc for c in cls.__subclasses__() for sc in _get_subclasses(c)]
    )


# Filtered at import time; settings may not be available yet,
# so the denylist is applied lazily in get_viz().
viz_types: dict[str | None, type[BaseViz]] = {
    o.viz_type: o for o in _get_subclasses(BaseViz) if o.viz_type is not None
}
