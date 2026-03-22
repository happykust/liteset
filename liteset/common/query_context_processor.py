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

Superset utility imports are done lazily (inside methods) to avoid pulling
in the Flask initialisation chain at module-import time.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json  # noqa: TID251
import logging
from typing import Any, ClassVar, TYPE_CHECKING, TypedDict

import numpy as np
import pandas as pd

from liteset.common.query_object import AsyncQueryObject
from liteset.typing import DatasourceProtocol

try:
    from superset.utils import pandas_postprocessing
except ImportError:
    pandas_postprocessing = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from liteset.common.query_context import AsyncQueryContext
    from liteset.config import LitesetSettings

logger = logging.getLogger(__name__)

OFFSET_JOIN_COLUMN_SUFFIX = "__offset_join_column_"
R_SUFFIX = "__right_suffix"
DTTM_ALIAS = "__timestamp"
_MAX_RECURSION_DEPTH = 2


class CachedTimeOffset(TypedDict):
    df: pd.DataFrame
    queries: list[str]
    cache_keys: list[str | None]


def _generate_cache_key(cache_dict: dict[str, Any], prefix: str = "") -> str:
    """Generate a deterministic cache key from a dict.

    Uses md5_sha_from_dict with json_int_dttm_ser when available (matches
    Superset's cache key generation), falls back to json.dumps(default=str).
    """
    try:
        from superset.utils.cache import md5_sha_from_dict
        from superset.utils.core import json_int_dttm_ser

        return f"{prefix}{md5_sha_from_dict(cache_dict, default=json_int_dttm_ser, ignore_nan=True)}"
    except ImportError:
        serialized = json.dumps(cache_dict, sort_keys=True, default=str)
        md5 = hashlib.md5(serialized.encode()).hexdigest()  # noqa: S324
        return f"{prefix}{md5}"


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
        settings: LitesetSettings,
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

    async def _ensure_totals_available(
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
            # No dedicated totals query — just flag the options
            for pp in contribution_ops:
                options = pp.get("options", {})
                if "contribution_totals" not in options:
                    options["contribution_totals"] = True
                pp["options"] = options
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
            for pp in contribution_ops:
                options = pp.get("options", {})
                if "contribution_totals" not in options:
                    options["contribution_totals"] = True
                pp["options"] = options

    async def get_payload(
        self,
        query_objects: list[AsyncQueryObject],
        force: bool = False,
        cache_query_context: bool = False,
        force_cached: bool = False,
    ) -> dict[str, Any]:
        """Main entry point — processes all query objects, returns payload."""
        await self._ensure_totals_available(query_objects)
        query_results = []
        for qo in query_objects:
            result = await self.get_df_payload(
                qo, force=force, force_cached=force_cached
            )
            query_results.append(result)

        return_value: dict[str, Any] = {"queries": query_results}

        if cache_query_context and self._cache_manager is not None:
            cache_key = self._generate_context_cache_key()
            return_value["cache_key"] = cache_key

        return return_value

    async def get_df_payload(
        self,
        query_object: AsyncQueryObject,
        force: bool = False,
        force_cached: bool = False,
    ) -> dict[str, Any]:
        """Execute a single query, return DataFrame + metadata."""
        # Validate query object (sanitize filters, check duplicates, etc.)
        query_object.validate()

        # Validate columns exist in datasource
        if hasattr(self._datasource, "column_names"):
            try:
                from superset.utils.core import (
                    get_column_names_from_columns,
                    get_column_names_from_metrics,
                )

                requested_cols = get_column_names_from_columns(query_object.columns)
                requested_cols += get_column_names_from_metrics(
                    query_object.metrics or []
                )
                invalid = [
                    col
                    for col in requested_cols
                    if col not in self._datasource.column_names and col != DTTM_ALIAS
                ]
                if invalid:
                    from superset.exceptions import QueryObjectValidationError

                    raise QueryObjectValidationError(
                        f"Columns missing in datasource: {invalid}"
                    )
            except ImportError:
                pass

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
                result = await self._get_query_result(query_object)
                df = result.get("df", pd.DataFrame())
                query_str = result.get("query", "")

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
            "label_map": {},
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
        rls_key = await self._security_manager.get_rls_cache_key(datasource)

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

    async def _get_query_result(self, query_object: AsyncQueryObject) -> dict[str, Any]:
        """Execute a query against the datasource, returning result dict."""
        datasource = self._datasource
        query_dict = query_object.to_dict()

        # Check if datasource supports async query
        if hasattr(datasource, "async_query"):
            result = await datasource.async_query(query_dict)
        elif hasattr(datasource, "query"):
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
            # Both normalize and post-processing are CPU-bound — offload to thread
            df = await asyncio.to_thread(
                self._normalize_and_postprocess, df, query_object
            )

        return {"df": df, "query": query_str}

    def _normalize_df(
        self, df: pd.DataFrame, query_object: AsyncQueryObject
    ) -> pd.DataFrame:
        """Replace inf/-inf with NaN and normalize datetime/metric columns.

        Uses get_base_axis_labels() + datasource.get_column() to build proper
        DateColumn objects with python_date_format, offset, time_shift.
        Handles DTTM_ALIAS via DateColumn.get_legacy_time_column().
        """
        df = df.replace([np.inf, -np.inf], np.nan)
        try:
            from superset.common.utils.dataframe_utils import (
                df_metrics_to_num,
                normalize_dttm_col,
            )

            dttm_cols: list[str] = []

            # Handle DTTM_ALIAS (legacy time column)
            if DTTM_ALIAS in df.columns:
                dttm_cols.append(DTTM_ALIAS)

            # Build DateColumn list from base axis labels with proper metadata
            date_columns: list[Any] = []
            try:
                from superset.common.utils.query_analysis import (
                    get_base_axis_labels,
                )

                base_labels = get_base_axis_labels(query_object.columns)
                for label in base_labels:
                    if label in df.columns and label not in dttm_cols:
                        dttm_cols.append(label)
                        # Build DateColumn with python_date_format, offset, time_shift
                        if hasattr(self._datasource, "get_column"):
                            col_obj = self._datasource.get_column(label)
                            if col_obj:
                                try:
                                    from superset.common.utils.dataframe_utils import (
                                        DateColumn,
                                    )

                                    date_columns.append(
                                        DateColumn(
                                            timestamp_format=getattr(
                                                col_obj, "python_date_format", None
                                            ),
                                            offset=getattr(
                                                self._datasource, "offset", None
                                            ),
                                            time_shift=query_object.time_shift,
                                        )
                                    )
                                except ImportError:
                                    pass
            except ImportError:
                # Fallback: check column specs directly
                for col_spec in query_object.columns:
                    if isinstance(col_spec, dict) and col_spec.get("is_dttm"):
                        col_name = col_spec.get("label") or col_spec.get(
                            "sqlExpression", ""
                        )
                        if col_name in df.columns and col_name not in dttm_cols:
                            dttm_cols.append(col_name)

            if dttm_cols:
                if date_columns:
                    # Use DateColumn-aware normalization when available
                    try:
                        normalize_dttm_col(df, dttm_cols, date_columns)
                    except TypeError:
                        # Fallback if normalize_dttm_col doesn't accept date_columns
                        normalize_dttm_col(df, dttm_cols)
                else:
                    normalize_dttm_col(df, dttm_cols)

            if self.enforce_numerical_metrics and query_object.metrics:
                df_metrics_to_num(df, query_object)
        except ImportError:
            pass
        return df

    def _normalize_and_postprocess(
        self, df: pd.DataFrame, query_object: AsyncQueryObject
    ) -> pd.DataFrame:
        """Normalize then apply post-processing. Runs in a worker thread."""
        df = self._normalize_df(df, query_object)
        df = self._exec_post_processing(df, query_object)
        return df

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
        """Retrieve cached data (DataFrame or dict with df+annotation_data)."""
        if self._cache_manager is None:
            return None
        try:
            if hasattr(self._cache_manager, "get"):
                getter = self._cache_manager.get(key)
                result = await getter if inspect.isawaitable(getter) else getter
                if result is not None:
                    return result
        except Exception:  # noqa: BLE001
            logger.warning("Cache get failed for key %s", key, exc_info=True)
        return None

    async def _cache_set(self, key: str, value: Any, timeout: int) -> None:
        """Store a value (DataFrame or dict) in cache."""
        if self._cache_manager is None:
            return
        try:
            if hasattr(self._cache_manager, "set"):
                setter = self._cache_manager.set(key, value, timeout)
                if inspect.isawaitable(setter):
                    await setter
        except Exception:  # noqa: BLE001
            logger.warning("Failed to cache key %s", key, exc_info=True)

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

    async def get_viz_annotation_data(
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

        # Handle legacy viz_types (table, line, bar, etc.)
        try:
            from superset.viz import viz_types

            if getattr(chart, "viz_type", None) in viz_types:
                form_data = getattr(chart, "form_data", {}) or {}
                viz_obj = viz_types[chart.viz_type](
                    chart.datasource, form_data=form_data
                )
                payload = await asyncio.to_thread(viz_obj.get_payload)
                return payload.get("data", {})
        except ImportError:
            pass

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
                    try:
                        from superset.utils.date_parser import get_since_until

                        from_dttm, to_dttm = get_since_until(
                            time_range=overrides["time_range"]
                        )
                        qo.from_dttm = from_dttm
                        qo.to_dttm = to_dttm
                    except ImportError:
                        pass

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

    async def processing_time_offsets(
        self,
        df: pd.DataFrame,
        query_object: AsyncQueryObject,
    ) -> CachedTimeOffset:
        """Process time-shifted comparisons (e.g., '1 week ago', '1 year ago').

        For each time_offset, clones the query with shifted time range,
        executes it, and joins with the original DataFrame.

        TODO(liteset/remaining-api): Implement recursive ChartDataCommand
        for time-shifted comparisons (YoY, WoW). See plan Task 2 warnings.
        """
        if query_object.time_offsets:
            logger.warning(
                "Time offset processing not yet implemented (liteset/remaining-api). "
                "Time comparison charts will return unshifted data. Offsets: %s",
                query_object.time_offsets,
            )
        queries: list[str] = []
        cache_keys: list[str | None] = []
        return CachedTimeOffset(df=df, queries=queries, cache_keys=cache_keys)

    async def raise_for_access(self) -> None:
        """Validate per-query and delegate to AsyncSecurityManager.raise_for_access()."""
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
            return df.to_csv(index=False)
        if result_format == "xlsx":
            import io

            buf = io.BytesIO()
            df.to_excel(buf, index=False, engine="openpyxl")
            buf.seek(0)
            return buf.getvalue()
        return df.to_dict(orient="records")
