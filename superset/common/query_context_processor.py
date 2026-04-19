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
"""Async QueryContextProcessor — processes QueryContext payloads without Flask.

Mirrors superset.common.query_context_processor.QueryContextProcessor.
All Flask globals (current_app.config, security_manager, cache_manager,
AnnotationLayerDAO) are replaced by constructor-injected dependencies.

All utility imports use superset.utils modules — no superset dependency.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import logging
import re
from typing import Any, ClassVar, TYPE_CHECKING, TypedDict

import numpy as np
import pandas as pd

from superset.common.query_object import AsyncQueryObject
from superset.typing import DatasourceProtocol
from superset.utils import pandas_postprocessing

if TYPE_CHECKING:
    from superset.common.query_context import AsyncQueryContext
    from superset.config import SupersetSettings

logger = logging.getLogger(__name__)

OFFSET_JOIN_COLUMN_SUFFIX = "__offset_join_column_"
R_SUFFIX = "__right_suffix"
DTTM_ALIAS = "__timestamp"
_MAX_RECURSION_DEPTH = 2

# CSV formula injection prevention (matches superset_old/utils/csv.py)
_NEGATIVE_NUMBER_RE = re.compile(r"^-[0-9.]+$")
_PROBLEMATIC_CHARS_RE = re.compile(r'^(?:"{2}|\s{1,})(?=[\-@+|=%])|^[\-@+|=%]')


def _escape_csv_value(value: str) -> str:
    """Escape values that could trigger formula injection in spreadsheets."""
    needs_escaping = _PROBLEMATIC_CHARS_RE.match(value) is not None
    is_negative_number = _NEGATIVE_NUMBER_RE.match(value) is not None
    if needs_escaping and not is_negative_number:
        value = value.replace("|", "\\|")
        value = "'" + value
    return value


def _df_to_escaped_csv(df: pd.DataFrame, **kwargs: Any) -> str:
    """Convert DataFrame to CSV with formula injection escaping."""

    def _escape(v: Any) -> str | Any:
        return _escape_csv_value(v) if isinstance(v, str) else v

    df = df.rename(columns=_escape)
    for name, column in df.items():
        if column.dtype == np.dtype(object):
            for idx, value in enumerate(column.values):
                if isinstance(value, str):
                    df.at[idx, name] = _escape_csv_value(value)
    return df.to_csv(index=False, escapechar="\\", **kwargs)


class CachedTimeOffset(TypedDict):
    df: pd.DataFrame
    queries: list[str]
    cache_keys: list[str | None]


def _generate_cache_key(cache_dict: dict[str, Any], prefix: str = "") -> str:
    """Generate a deterministic cache key from a dict.

    Uses md5_sha_from_dict with json_int_dttm_ser (matches Superset's
    cache key generation).
    """
    from superset.utils.hashing import md5_sha_from_dict
    from superset.utils.json import json_int_dttm_ser

    hash_val = md5_sha_from_dict(cache_dict, default=json_int_dttm_ser, ignore_nan=True)
    return f"{prefix}{hash_val}"


class AsyncQueryContextProcessor:
    """Processes QueryContext payloads asynchronously.

    Replaces superset.common.query_context_processor.QueryContextProcessor.
    All Flask globals are replaced by constructor-injected dependencies:
      - current_app.config["ROW_LIMIT"] -> self._settings.row_limit
      - current_app.config["CACHE_DEFAULT_TIMEOUT"]
      - current_app.config["DATA_CACHE_CONFIG"] -> self._settings.data_cache_config
      - security_manager -> self._security_manager
      - cache_manager -> self._cache_manager
      - AnnotationLayerDAO -> self._annotation_dao
    """

    cache_type: ClassVar[str] = "df"
    enforce_numerical_metrics: ClassVar[bool] = True

    def __init__(
        self,
        datasource: DatasourceProtocol,
        settings: SupersetSettings,
        security_manager: Any,
        user: Any = None,
        cache_manager: Any | None = None,
        annotation_dao: Any | None = None,
        chart_dao: Any | None = None,
        query_context: AsyncQueryContext | None = None,
        _recursion_depth: int = 0,
    ) -> None:
        self._datasource = datasource
        self._settings = settings
        self._security_manager = security_manager
        self._user = user
        self._cache_manager = cache_manager
        self._annotation_dao = annotation_dao
        self._chart_dao = chart_dao
        self._query_context = query_context
        self._recursion_depth = _recursion_depth

    async def _ensure_totals_available(  # noqa: C901
        self, query_objects: list[AsyncQueryObject]
    ) -> None:
        """Find the totals query and inject computed totals into contribution ops.

        Matches Superset's _ensure_totals_available: finds the totals query
        (no columns, has metrics, no post-processing), executes it, and injects
        the totals dict into contribution operations' options.
        """
        # Find contribution operations that need totals
        contribution_ops: list[dict[str, Any]] = []
        for qo in query_objects:
            for pp in qo.post_processing:
                if pp.get("operation") == "contribution":
                    contribution_ops.append(pp)

        if not contribution_ops:
            return

        # Find the totals query (no columns, has metrics, no post-processing)
        totals_query: AsyncQueryObject | None = None
        for qo in query_objects:
            if not qo.columns and qo.metrics and not qo.post_processing:
                totals_query = qo
                break

        if totals_query is None:
            return

        # Remove row_limit on the totals query to get full sums
        totals_query.row_limit = None

        # Execute totals query to get actual column sums
        try:
            result = await self._get_query_result(totals_query)
            totals_df = result.get("df", pd.DataFrame())
            if not totals_df.empty:
                # Use column sums, not iloc[0] — totals query may return
                # multiple rows if there are no groupby columns
                totals_dict = totals_df.sum(numeric_only=True).to_dict()
                for pp in contribution_ops:
                    options = pp.get("options", {})
                    options["contribution_totals"] = totals_dict
                    pp["options"] = options
        except Exception:  # noqa: BLE001
            logger.warning("Failed to compute totals for contribution", exc_info=True)

    async def get_payload(
        self,
        query_objects: list[AsyncQueryObject],
        force: bool = False,
        cache_query_context: bool = False,
        force_cached: bool = False,
    ) -> dict[str, Any]:
        """Main entry point — processes all query objects, returns payload.

        Dispatches based on each query_object's result_type:
          - "query"   — build SQL but don't execute
          - "samples" — raw rows without metrics/filters
          - "results" — execute without post-processing
          - "full" (default) — execute, normalize, and post-process
        """
        await self._ensure_totals_available(query_objects)
        query_results = []
        for qo in query_objects:
            result_type = getattr(qo, "result_type", None) or "full"

            if result_type == "query":
                result = await self._get_query_only(qo)
            elif result_type == "samples":
                result = await self._get_samples(qo)
            elif result_type == "results":
                result = await self.get_df_payload(
                    qo,
                    force=force,
                    force_cached=force_cached,
                    skip_post_processing=True,
                )
            else:
                # "full" — default behavior
                result = await self.get_df_payload(
                    qo,
                    force=force,
                    force_cached=force_cached,
                )
            query_results.append(result)

        return_value: dict[str, Any] = {"queries": query_results}

        if cache_query_context and self._cache_manager is not None:
            cache_key = self._generate_context_cache_key()
            return_value["cache_key"] = cache_key

        return return_value

    async def _get_query_only(self, query_object: AsyncQueryObject) -> dict[str, Any]:
        """Build SQL without executing — returns the query string only."""
        datasource = self._datasource
        query_dict = query_object.to_dict()

        if hasattr(datasource, "get_query_str"):
            query_str = datasource.get_query_str(query_dict)
        elif hasattr(datasource, "get_query_str_extended"):
            result = datasource.get_query_str_extended(query_dict)
            query_str = getattr(result, "sql", str(result))
        else:
            query_str = str(query_dict)

        return {
            "query": query_str,
            "status": "success",
            "error": None,
            "df": pd.DataFrame(),
            "data": [],
            "rowcount": 0,
            "is_cached": False,
            "label_map": {},
            "applied_filters": [],
            "rejected_filters": [],
            "coltypes": [],
        }

    async def _get_samples(self, query_object: AsyncQueryObject) -> dict[str, Any]:
        """Execute a simplified query for raw sample rows.

        Strips metrics and filters to return raw LIMIT N rows from the
        datasource.
        """
        sample_qo = copy.deepcopy(query_object)
        sample_qo.metrics = []
        sample_qo.filters = []
        sample_qo.post_processing = []
        sample_qo.orderby = []
        if not sample_qo.row_limit:
            sample_qo.row_limit = getattr(self._settings, "row_limit", 1000)

        return await self.get_df_payload(sample_qo, skip_post_processing=True)

    async def get_df_payload(  # noqa: C901
        self,
        query_object: AsyncQueryObject,
        force: bool = False,
        force_cached: bool = False,
        skip_post_processing: bool = False,
    ) -> dict[str, Any]:
        """Execute a single query, return DataFrame + metadata."""
        # Validate query object (sanitize filters, check duplicates, etc.)
        query_object.validate()

        # Validate columns exist in datasource (skip if no column metadata)
        ds_columns = getattr(self._datasource, "column_names", None)
        if ds_columns:
            from superset.utils.column import (
                get_column_names_from_columns,
                get_column_names_from_metrics,
            )

            requested_cols = get_column_names_from_columns(query_object.columns)
            requested_cols += get_column_names_from_metrics(query_object.metrics or [])
            invalid = [
                col
                for col in requested_cols
                if col not in ds_columns and col != DTTM_ALIAS
            ]
            if invalid:
                from superset.exceptions import QueryObjectValidationError

                raise QueryObjectValidationError(
                    f"Columns missing in datasource: {invalid}"
                )

        cache_key = await self._get_cache_key(query_object)
        timeout = self._get_cache_timeout()

        # Check cache
        cached_df: pd.DataFrame | None = None
        cached_annotation_data: dict[str, Any] = {}
        is_cached = False
        if self._cache_manager is not None and cache_key and not force:
            cached_result = await self._cache_get(cache_key)
            if cached_result is not None:
                if isinstance(cached_result, dict) and "df" in cached_result:
                    cached_df = cached_result["df"]
                    cached_annotation_data = cached_result.get("annotation_data", {})
                elif isinstance(cached_result, pd.DataFrame):
                    cached_df = cached_result
                if cached_df is not None:
                    is_cached = True

        if force_cached and not is_cached:
            return {
                "cache_key": cache_key,
                "error": "Cached data not available",
                "status": "failed",
                "is_cached": False,
                "df": pd.DataFrame(),
                "data": [],
                "rowcount": 0,
                "label_map": {},
                "applied_filters": [],
                "rejected_filters": [],
                "coltypes": [],
            }

        error_message: str | None = None
        query_str = ""
        status = "success"
        df: pd.DataFrame

        annotation_data: dict[str, Any] = cached_annotation_data
        if cached_df is not None:
            df = cached_df
            # Annotation data is stored alongside df in cache — skip re-fetch
        else:
            try:
                result = await self._get_query_result(
                    query_object,
                    skip_post_processing=skip_post_processing,
                )
                df = result.get("df", pd.DataFrame())
                query_str = result.get("query", "")

                # Process time comparison offsets before post-processing so
                # that shifted metric columns (e.g. ``Births__28 days ago``)
                # exist when post-processing operations like ``pivot`` look
                # them up. Mirrors the original order in
                # ``superset_old/common/query_context_processor.py:292-302``.
                if query_object.time_offsets:
                    time_offset_result = await self.processing_time_offsets(
                        df, query_object
                    )
                    df = time_offset_result["df"]

                # Post-processing runs after time_offsets so it can operate
                # on the joined, shifted DataFrame. Skipped for ``results`` /
                # ``samples`` result types where the caller opted out.
                if not skip_post_processing and not df.empty:
                    df = await asyncio.to_thread(
                        self._exec_post_processing, df, query_object
                    )

                # Fetch annotation data only on cache miss
                annotation_data = await self.get_annotation_data(query_object)

                # Store df + annotation_data in cache together
                if self._cache_manager is not None and cache_key:
                    cache_payload = {
                        "df": df,
                        "annotation_data": annotation_data,
                    }
                    await self._cache_set(cache_key, cache_payload, timeout)
            except Exception as ex:
                logger.exception("Query execution failed")
                df = pd.DataFrame()
                error_message = str(ex)
                status = "failed"

        # Compute label_map from DataFrame columns
        label_map = {col: [col] for col in df.columns}

        # Compute coltypes from DataFrame dtypes
        coltypes = self._extract_coltypes(df)

        # Extract applied/rejected filters from query_object
        applied_filters = [
            {"column": f.get("col", ""), "op": f.get("op", "")}
            for f in (query_object.filters or [])
        ]
        rejected_filters: list[dict[str, str]] = []

        return {
            "cache_key": cache_key,
            "cached_dttm": None,
            "cache_timeout": timeout,
            "df": df,
            "applied_template_filters": [],
            "applied_filter_columns": [],
            "rejected_filter_columns": [],
            "annotation_data": annotation_data,
            "error": error_message,
            "is_cached": is_cached,
            "query": query_str,
            "status": status,
            "stacktrace": None,
            "rowcount": len(df.index),
            "sql_rowcount": len(df.index),
            "from_dttm": query_object.from_dttm,
            "to_dttm": query_object.to_dttm,
            "label_map": label_map,
            "coltypes": coltypes,
            "applied_filters": applied_filters,
            "rejected_filters": rejected_filters,
            # Match original Superset chart/data response shape
            # (superset_old/common/query_context_processor.py).
            "result_format": getattr(query_object, "result_format", None) or "json",
        }

    async def _get_cache_key(
        self,
        query_object: AsyncQueryObject,
    ) -> str:
        """Generate cache key from datasource.uid + RLS + query params."""
        datasource = self._datasource
        extra_cache_keys = (
            datasource.get_extra_cache_keys(query_object.to_dict())
            if hasattr(datasource, "get_extra_cache_keys")
            else []
        )
        rls_key = await self._security_manager.get_rls_cache_key(
            datasource, user=self._user
        )

        cache_dict = query_object.cache_key()
        cache_dict.update(
            {
                "datasource": getattr(datasource, "uid", str(id(datasource))),
                "extra_cache_keys": extra_cache_keys,
                "rls": rls_key,
                "changed_on": getattr(datasource, "changed_on", None),
            }
        )
        # Add impersonation key for per-user cache isolation
        if hasattr(self._settings, "feature_flags"):
            flags = self._settings.feature_flags or {}
            if flags.get("CACHE_IMPERSONATION") or flags.get("CACHE_QUERY_BY_USER"):
                if self._user is not None:
                    cache_dict["impersonation_key"] = getattr(
                        self._user, "username", str(getattr(self._user, "id", ""))
                    )

        return _generate_cache_key(cache_dict, "df-")

    async def _get_query_result(  # noqa: C901
        self,
        query_object: AsyncQueryObject,
        skip_post_processing: bool = False,
    ) -> dict[str, Any]:
        """Execute a query against the datasource, returning result dict."""
        datasource = self._datasource
        query_dict = query_object.to_dict()

        # Gather RLS filters from security manager and pass to query
        rls_clauses: list[Any] = []
        if hasattr(self._security_manager, "get_rls_filters"):
            try:
                rls_filters = await self._security_manager.get_rls_filters(
                    self._datasource, user=self._user
                )
                rls_clauses = [f.clause for f in rls_filters]
            except Exception:  # noqa: BLE001
                logger.warning("Failed to retrieve RLS filters", exc_info=True)

        # Check if datasource supports async query
        if hasattr(datasource, "async_query"):
            if rls_clauses:
                result = await datasource.async_query(
                    query_dict, rls_filters=rls_clauses
                )
            else:
                result = await datasource.async_query(query_dict)
        elif hasattr(datasource, "query"):
            if rls_clauses:
                result = await asyncio.to_thread(  # type: ignore[call-arg]
                    datasource.query, query_dict, rls_filters=rls_clauses
                )
            else:
                result = await asyncio.to_thread(datasource.query, query_dict)
        else:
            raise ValueError(
                f"Datasource {type(datasource).__name__} does not support querying"
            )

        # Normalize result to dict
        if hasattr(result, "df"):
            df = result.df
            query_str = getattr(result, "query", "")
        elif isinstance(result, dict):
            df = result.get("df", pd.DataFrame())
            query_str = result.get("query", "")
        else:
            df = pd.DataFrame()
            query_str = ""

        if not df.empty:
            # Always only *normalize* here. Post-processing is applied by
            # ``get_df_payload`` AFTER ``processing_time_offsets`` has had a
            # chance to join the time-shifted columns onto the DataFrame.
            # Mirrors the original
            # ``superset_old/common/query_context_processor.py:get_query_result``
            # which does ``normalize_df → processing_time_offsets → exec_post_processing``
            # in that order.
            df = await asyncio.to_thread(self._normalize_df, df, query_object)

        return {"df": df, "query": query_str}

    def _normalize_df(  # noqa: C901
        self, df: pd.DataFrame, query_object: AsyncQueryObject
    ) -> pd.DataFrame:
        """Replace inf/-inf with NaN and normalize datetime/metric columns.

        Uses get_base_axis_labels() + datasource.get_column() to build proper
        DateColumn objects with python_date_format, offset, time_shift.
        Handles DTTM_ALIAS via DateColumn.get_legacy_time_column().
        """
        from superset.utils.column import get_base_axis_labels, get_metric_names
        from superset.utils.dataframe import df_metrics_to_num, normalize_dttm_col
        from superset.utils.date import DateColumn

        df = df.replace([np.inf, -np.inf], np.nan)

        date_columns: list[DateColumn] = []

        # Handle DTTM_ALIAS (legacy time column)
        if DTTM_ALIAS in df.columns:
            date_columns.append(
                DateColumn.get_legacy_time_column(
                    timestamp_format=None,
                    offset=getattr(self._datasource, "offset", None),
                    time_shift=query_object.time_shift,
                )
            )

        # Build DateColumn list from base axis labels with proper metadata
        seen_labels = {DTTM_ALIAS}
        base_labels = get_base_axis_labels(query_object.columns)
        for label in base_labels:
            if label in df.columns and label not in seen_labels:
                seen_labels.add(label)
                timestamp_format: str | None = None
                if hasattr(self._datasource, "get_column"):
                    col_obj = self._datasource.get_column(label)
                    if col_obj:
                        timestamp_format = getattr(col_obj, "python_date_format", None)
                date_columns.append(
                    DateColumn(
                        col_label=label,
                        timestamp_format=timestamp_format,
                        offset=getattr(self._datasource, "offset", None),
                        time_shift=query_object.time_shift,
                    )
                )

        # Fallback: check column specs directly for dttm columns
        if not date_columns:
            for col_spec in query_object.columns:
                if isinstance(col_spec, dict) and col_spec.get("is_dttm"):
                    col_name = col_spec.get("label") or col_spec.get(
                        "sqlExpression", ""
                    )
                    if col_name in df.columns and col_name not in seen_labels:
                        seen_labels.add(col_name)
                        date_columns.append(
                            DateColumn(
                                col_label=col_name,
                                timestamp_format=None,
                                offset=getattr(self._datasource, "offset", None),
                                time_shift=query_object.time_shift,
                            )
                        )

        if date_columns:
            normalize_dttm_col(df, tuple(date_columns))

        if self.enforce_numerical_metrics and query_object.metrics:
            metric_names = get_metric_names(query_object.metrics)
            df_metrics_to_num(df, metric_names)

        return df

    def _normalize_and_postprocess(
        self, df: pd.DataFrame, query_object: AsyncQueryObject
    ) -> pd.DataFrame:
        """Normalize then apply post-processing. Runs in a worker thread."""
        df = self._normalize_df(df, query_object)
        df = self._exec_post_processing(df, query_object)
        return df

    @staticmethod
    def _extract_coltypes(df: pd.DataFrame) -> list[int]:
        """Map DataFrame column dtypes to GenericDataType integers."""
        from superset.typing import GenericDataType

        result: list[int] = []
        for col in df.columns:
            dtype = df[col].dtype
            if pd.api.types.is_bool_dtype(dtype):
                # Check bool before numeric since bool is a subtype of int
                result.append(GenericDataType.BOOLEAN)
            elif pd.api.types.is_datetime64_any_dtype(dtype):
                result.append(GenericDataType.TEMPORAL)
            elif pd.api.types.is_numeric_dtype(dtype):
                result.append(GenericDataType.NUMERIC)
            else:
                result.append(GenericDataType.STRING)
        return result

    @staticmethod
    def _exec_post_processing(
        df: pd.DataFrame,
        query_object: AsyncQueryObject,
    ) -> pd.DataFrame:
        """Apply post-processing operations on the DataFrame.

        This is a synchronous method (pandas operations are CPU-bound).
        Called via asyncio.to_thread() to avoid blocking the event loop.
        """
        if not query_object.post_processing:
            return df

        if pandas_postprocessing is None:
            raise ImportError(
                "superset.utils.pandas_postprocessing is not available; "
                "post-processing operations cannot be applied."
            )

        for operation in query_object.post_processing:
            op_name = operation.get("operation")
            op_options = operation.get("options", {})
            if op_name:
                if not hasattr(pandas_postprocessing, op_name):
                    raise ValueError(f"Unknown post-processing operation: {op_name}")
                func = getattr(pandas_postprocessing, op_name)
                df = func(df, **op_options)
        return df

    @staticmethod
    def get_time_grain(query_object: AsyncQueryObject) -> Any | None:
        if (
            query_object.columns
            and len(query_object.columns) > 0
            and isinstance(query_object.columns[0], dict)
        ):
            return query_object.columns[0].get("timeGrain")
        return query_object.extras.get("time_grain_sqla")

    def _get_cache_timeout(self) -> int:
        """Get cache timeout — priority chain matches Superset:

        1. custom_cache_timeout (from query context)
        2. slice_.cache_timeout (chart-level)
        3. datasource.cache_timeout (dataset-level)
        4. datasource.database.cache_timeout (database-level)
        5. data_cache_config["CACHE_DEFAULT_TIMEOUT"]
        6. settings.cache_default_timeout
        """
        if self._query_context is not None:
            custom = getattr(self._query_context, "custom_cache_timeout", None)
            if custom is not None:
                return custom
            slice_ = getattr(self._query_context, "slice_", None)
            if slice_ and getattr(slice_, "cache_timeout", None):
                return slice_.cache_timeout
        if (
            hasattr(self._datasource, "cache_timeout")
            and self._datasource.cache_timeout
        ):
            return self._datasource.cache_timeout
        if hasattr(self._datasource, "database"):
            db = self._datasource.database
            if hasattr(db, "cache_timeout") and db.cache_timeout:
                return db.cache_timeout
        data_cache_config = getattr(self._settings, "data_cache_config", {})
        if isinstance(data_cache_config, dict):
            timeout = data_cache_config.get("CACHE_DEFAULT_TIMEOUT")
            if timeout is not None:
                return timeout
        return getattr(self._settings, "cache_default_timeout", 300)

    def _generate_context_cache_key(self) -> str:
        cache_dict: dict[str, Any] = {
            "datasource": getattr(self._datasource, "uid", ""),
        }
        if self._query_context is not None:
            cache_dict["queries"] = [qo.to_dict() for qo in self._query_context.queries]
            cache_dict["form_data"] = self._query_context.form_data
        return _generate_cache_key(cache_dict, "qc-")

    async def _cache_get(self, key: str) -> Any | None:
        """Retrieve cached data (DataFrame or dict with df+annotation_data).

        Values are stored as pickle bytes by ``_cache_set``, so we
        deserialize them here before returning.
        """
        if self._cache_manager is None:
            return None
        try:
            if hasattr(self._cache_manager, "get"):
                getter = self._cache_manager.get(key)
                result = await getter if inspect.isawaitable(getter) else getter
                if result is not None:
                    try:
                        import pickle  # noqa: S403

                        return pickle.loads(result)  # noqa: S301
                    except (pickle.UnpicklingError, TypeError, EOFError):
                        logger.warning(
                            "Cache unpickle failed for key %s, returning raw value",
                            key,
                        )
                        return result
        except Exception:  # noqa: BLE001
            logger.warning("Cache get failed for key %s", key, exc_info=True)
        return None

    async def _cache_set(self, key: str, value: Any, timeout: int) -> None:
        """Store a value (DataFrame or dict) in cache.

        Serializes the value to bytes via pickle before passing to the cache
        manager, which expects ``bytes``.  Pickle is used here (rather than
        JSON) because the cached values may contain pandas DataFrames and
        NumPy arrays — the same approach the original Flask QueryContextProcessor
        takes via ``superset.extensions.cache_manager``.
        """
        if self._cache_manager is None:
            return
        try:
            import pickle  # noqa: S403

            serialized = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
            if hasattr(self._cache_manager, "set"):
                setter = self._cache_manager.set(key, serialized, timeout)
                if inspect.isawaitable(setter):
                    await setter
        except (TypeError, pickle.PicklingError):
            logger.warning("Cache serialization failed for key %s", key, exc_info=True)
        except Exception:
            logger.warning("Cache set failed for key %s", key, exc_info=True)

    async def get_annotation_data(
        self, query_object: AsyncQueryObject
    ) -> dict[str, Any]:
        """Orchestrator — dispatches to native or viz annotation fetchers."""
        annotation_data: dict[str, Any] = await self.get_native_annotation_data(
            query_object
        )
        for annotation_layer in [
            layer
            for layer in query_object.annotation_layers
            if layer.get("sourceType") in ("line", "table")
        ]:
            name = annotation_layer["name"]
            annotation_data[name] = await self.get_viz_annotation_data(
                annotation_layer,
                force=getattr(self._query_context, "force", False),
                _depth=self._recursion_depth,
            )
        return annotation_data

    async def get_native_annotation_data(
        self, query_object: AsyncQueryObject
    ) -> dict[str, Any]:
        """Fetch native annotations via AnnotationLayerDAO."""
        annotation_data: dict[str, Any] = {}
        annotation_layers = [
            layer
            for layer in query_object.annotation_layers
            if layer.get("sourceType") == "NATIVE"
        ]
        if not annotation_layers or self._annotation_dao is None:
            return annotation_data

        layer_ids = [layer["value"] for layer in annotation_layers]
        layer_objects_list = await self._annotation_dao.find_by_ids(layer_ids)
        layer_objects = {lo.id: lo for lo in layer_objects_list}

        for layer in annotation_layers:
            layer_id = layer["value"]
            layer_name = layer["name"]
            columns = [
                "start_dttm",
                "end_dttm",
                "short_descr",
                "long_descr",
                "json_metadata",
            ]
            layer_object = layer_objects.get(layer_id)
            if layer_object is None:
                continue
            records = [
                {column: getattr(annotation, column) for column in columns}
                for annotation in layer_object.annotation
            ]
            annotation_data[layer_name] = {"columns": columns, "records": records}
        return annotation_data

    async def get_viz_annotation_data(  # noqa: C901
        self,
        annotation_layer: dict[str, Any],
        force: bool,
        _depth: int = 0,
    ) -> dict[str, Any]:
        """Fetch annotation data from another chart (recursive).

        Depth limit prevents infinite recursion when charts reference each other.
        """
        if _depth >= _MAX_RECURSION_DEPTH:
            raise ValueError(
                f"Annotation recursion depth exceeded (max={_MAX_RECURSION_DEPTH})"
            )

        if self._chart_dao is None:
            raise ValueError("Chart DAO not available for annotations")

        chart = await self._chart_dao.find_by_id(annotation_layer["value"])
        if not chart:
            raise ValueError(
                f"Chart with ID {annotation_layer['value']} "
                f"(referenced by annotation layer '{annotation_layer['name']}') "
                f"was not found."
            )

        # Legacy viz_types not used in superset — skip legacy viz path
        viz_types: dict[str, Any] = {}
        if getattr(chart, "viz_type", None) in viz_types:
            pass  # pragma: no cover

        get_qc = getattr(chart, "get_query_context", None)
        if get_qc is None:
            raise ValueError(
                f"Chart ID {chart.id} does not support get_query_context(). "
                f"Annotation layer '{annotation_layer['name']}' cannot be resolved."
            )
        query_context = get_qc()
        if not query_context:
            raise ValueError(
                f"The query context for chart ID {chart.id} "
                f"(referenced by annotation layer '{annotation_layer['name']}') "
                f"was not found."
            )

        # Apply annotation overrides to query objects
        overrides = annotation_layer.get("overrides", {})
        if overrides and query_context.queries:
            for qo in query_context.queries:
                if "time_grain_sqla" in overrides:
                    qo.extras["time_grain_sqla"] = overrides["time_grain_sqla"]
                if "time_range" in overrides:
                    qo.time_range = overrides["time_range"]
                    # Resolve time_range to actual datetime bounds
                    from superset.utils.date import get_since_until

                    from_dttm, to_dttm = get_since_until(
                        time_range=overrides["time_range"]
                    )
                    qo.from_dttm = from_dttm
                    qo.to_dttm = to_dttm

        # Build a new processor scoped to the annotation chart's datasource.
        # Using self._datasource here would produce wrong cache keys and
        # access checks (the annotation source may be a different dataset).
        annotation_datasource = getattr(chart, "datasource", self._datasource)
        annotation_processor = AsyncQueryContextProcessor(
            datasource=annotation_datasource,
            settings=self._settings,
            security_manager=self._security_manager,
            user=self._user,
            cache_manager=self._cache_manager,
            annotation_dao=self._annotation_dao,
            chart_dao=self._chart_dao,
            _recursion_depth=_depth + 1,
        )
        payload = await annotation_processor.get_payload(
            query_objects=query_context.queries, force=force
        )
        return {"records": payload["queries"][0].get("data", [])}

    async def processing_time_offsets(  # noqa: C901
        self,
        df: pd.DataFrame,
        query_object: AsyncQueryObject,
    ) -> CachedTimeOffset:
        """Process time-shifted comparisons (e.g., '1 week ago', '1 year ago').

        For each time_offset, clones the query with shifted time range,
        executes it, renames metric columns with the offset suffix, and
        joins the offset DataFrames with the original on non-metric columns.
        """
        queries: list[str] = []
        cache_keys: list[str | None] = []

        if not query_object.time_offsets:
            return CachedTimeOffset(df=df, queries=queries, cache_keys=cache_keys)

        # Resolve outer time bounds from the query object.
        #
        # Mirrors the original
        # ``superset_old/common/utils/time_range_utils.py::get_since_until_from_query_object``:
        # ``query_object.time_range`` takes precedence, otherwise fall back to
        # scanning ``query_object.filters`` for a ``TEMPORAL_RANGE`` entry.
        # The Explore UI emits time filters as adhoc TEMPORAL_RANGE entries
        # rather than top-level ``time_range``.
        outer_from_dttm = query_object.from_dttm
        outer_to_dttm = query_object.to_dttm

        if not outer_from_dttm or not outer_to_dttm:
            from superset.utils.date import get_since_until

            resolved_time_range: str | None = query_object.time_range
            if not resolved_time_range:
                for flt in query_object.filters or []:
                    if (
                        isinstance(flt, dict)
                        and flt.get("op") == "TEMPORAL_RANGE"
                        and isinstance(flt.get("val"), str)
                    ):
                        resolved_time_range = flt["val"]
                        break

            since, until = get_since_until(
                time_range=resolved_time_range,
                time_shift=query_object.time_shift,
            )
            outer_from_dttm = outer_from_dttm or since
            outer_to_dttm = outer_to_dttm or until

        if not outer_from_dttm or not outer_to_dttm:
            from superset.exceptions import QueryObjectValidationError

            raise QueryObjectValidationError(
                "An enclosed time range (both start and end) must be specified "
                "when using a Time Comparison."
            )

        # Determine metric names to identify which columns to rename
        from superset.utils.column import get_metric_names

        metric_names = get_metric_names(query_object.metrics)

        # Non-metric columns serve as join keys
        join_keys = [col for col in df.columns if col not in metric_names]

        # Time comparison separator (matches Superset's TIME_COMPARISON = "__")
        time_comparison_sep = "__"

        offset_dfs: dict[str, pd.DataFrame] = {}

        for offset in query_object.time_offsets:
            try:
                query_object_clone = copy.deepcopy(query_object)

                # Shift from_dttm and to_dttm using the offset string
                from superset.utils.date import get_past_or_future

                query_object_clone.from_dttm = get_past_or_future(
                    offset,
                    outer_from_dttm,  # type: ignore[arg-type]
                )
                query_object_clone.to_dttm = get_past_or_future(
                    offset,
                    outer_to_dttm,  # type: ignore[arg-type]
                )

                query_object_clone.inner_from_dttm = query_object_clone.from_dttm
                query_object_clone.inner_to_dttm = query_object_clone.to_dttm

                # Set granularity if not already set
                from superset.utils.column import get_x_axis_label

                x_axis_label = get_x_axis_label(query_object.columns)
                query_object_clone.granularity = (
                    query_object_clone.granularity or x_axis_label
                )

                # Clear time_offsets and post_processing on the clone to avoid
                # recursion and ensure we get raw data
                query_object_clone.time_offsets = []
                query_object_clone.post_processing = []

                # Remove row_limit/offset on clone to prevent data inconsistency
                # during the join (matches Superset behaviour)
                query_object_clone.row_limit = None
                query_object_clone.row_offset = 0

                # Execute the shifted query
                result = await self._get_query_result(query_object_clone)
                offset_metrics_df = result.get("df", pd.DataFrame())
                query_str = result.get("query", "")

                queries.append(query_str)
                cache_keys.append(None)

                # Build metrics mapping: SUM(value) -> SUM(value)__1 year ago
                metrics_mapping = {
                    metric: time_comparison_sep.join([metric, offset])
                    for metric in metric_names
                }

                if offset_metrics_df.empty:
                    # Create a placeholder DataFrame with NaN values
                    offset_metrics_df = pd.DataFrame(
                        {
                            col: [np.nan]
                            for col in join_keys + list(metrics_mapping.values())
                        }
                    )
                else:
                    # Normalize the offset DataFrame
                    offset_metrics_df = self._normalize_df(
                        offset_metrics_df, query_object_clone
                    )
                    # Rename metric columns with offset suffix
                    offset_metrics_df = offset_metrics_df.rename(
                        columns=metrics_mapping
                    )

                offset_dfs[offset] = offset_metrics_df

            except Exception:  # noqa: BLE001
                logger.exception("Failed to process time offset '%s'", offset)
                queries.append("")
                cache_keys.append(None)

        # Join all offset DataFrames with the original
        if offset_dfs:
            df = self._join_offset_dfs(df, offset_dfs, join_keys)

        return CachedTimeOffset(df=df, queries=queries, cache_keys=cache_keys)

    @staticmethod
    def _join_offset_dfs(
        df: pd.DataFrame,
        offset_dfs: dict[str, pd.DataFrame],
        join_keys: list[str],
    ) -> pd.DataFrame:
        """Join offset DataFrames with the main DataFrame on non-metric columns.

        Uses a suffixed join column approach to handle duplicate column names,
        matching Superset's join_offset_dfs logic (simplified without
        TIME_GRAIN_JOIN_COLUMN_PRODUCERS which requires Flask config).
        """
        for _offset, offset_df in offset_dfs.items():
            # Find common join keys that exist in both DataFrames
            actual_join_keys = [k for k in join_keys if k in offset_df.columns]
            if not actual_join_keys:
                # No join keys — concatenate columns directly
                df = pd.concat([df, offset_df], axis=1)
                continue

            # Keep only join keys + new (renamed) metric columns from offset_df
            offset_cols = actual_join_keys + [
                c for c in offset_df.columns if c not in join_keys
            ]
            offset_df = offset_df[offset_cols]

            df = df.merge(
                offset_df,
                on=actual_join_keys,
                how="left",
                suffixes=("", R_SUFFIX),
            )

            # Drop any right-suffix columns created by duplicate non-key columns
            drop_cols = [c for c in df.columns if c.endswith(R_SUFFIX)]
            if drop_cols:
                df = df.drop(columns=drop_cols)

        return df

    async def raise_for_access(self) -> None:
        """Validate per-query and delegate to
        AsyncSecurityManager.raise_for_access().
        """
        # Validate each query object before access check
        if self._query_context is not None:
            for query in self._query_context.queries:
                query.validate()

        if getattr(self._datasource, "type", None) == "query":
            await self._security_manager.raise_for_access(
                query=self._datasource, user=self._user
            )
        else:
            await self._security_manager.raise_for_access(
                query_context=self._query_context, user=self._user
            )

    @staticmethod
    def get_data(df: pd.DataFrame, result_format: str = "json") -> Any:
        """Convert DataFrame to the requested result format."""
        if result_format == "csv":
            return _df_to_escaped_csv(df)
        if result_format == "xlsx":
            import io

            buf = io.BytesIO()
            df.to_excel(buf, index=False, engine="openpyxl")
            buf.seek(0)
            return buf.getvalue()

        # Serialize datetime columns as epoch milliseconds to match the
        # original Superset chart data API, which uses ``json_int_dttm_ser``
        # (see ``superset_old/charts/data/api.py``). Chart components on
        # the frontend expect numeric timestamps so they can format them
        # with ``smart_date`` / ``time_grain_sqla`` (e.g. "2008 Q1").
        if df.empty:
            return []

        df_out = df
        dttm_cols = [
            col
            for col in df.columns
            if pd.api.types.is_datetime64_any_dtype(df[col].dtype)
        ]
        if dttm_cols:
            df_out = df.copy()
            for col in dttm_cols:
                series = df_out[col]
                # nanoseconds since epoch → milliseconds (float)
                ms = series.astype("int64", copy=False).astype("float64") / 1_000_000
                # Preserve NaT as None
                mask = series.isna()
                if mask.any():
                    ms = ms.where(~mask, other=None)
                df_out[col] = ms
        return df_out.to_dict(orient="records")
