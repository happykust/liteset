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
"""Async QueryObject — describes a single query within a QueryContext."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import msgspec.structs

from superset.exceptions import QueryObjectValidationError
from superset.utils.feature_flags import feature_flag_manager
from superset.utils.jinja import get_template_processor
from superset.utils.sql import sanitize_clause

logger = logging.getLogger(__name__)


@dataclass
class AsyncQueryObject:
    """Describes a single query to execute against a datasource.

    Mirrors superset.common.query_object.QueryObject fields for
    API contract compatibility. Does not depend on Flask.
    """

    datasource: dict[str, Any]
    columns: list[Any] = field(default_factory=list)
    metrics: list[Any] = field(default_factory=list)
    orderby: list[tuple[Any, bool]] = field(default_factory=list)
    filters: list[dict[str, Any]] = field(default_factory=list)
    extras: dict[str, Any] = field(default_factory=dict)
    time_range: str | None = None
    time_shift: str | None = None
    granularity: str | None = None
    row_limit: int | None = None
    row_offset: int = 0
    from_dttm: datetime | str | None = None
    to_dttm: datetime | str | None = None
    inner_from_dttm: datetime | str | None = None
    inner_to_dttm: datetime | str | None = None
    order_desc: bool = True
    post_processing: list[dict[str, Any]] = field(default_factory=list)
    annotation_layers: list[dict[str, Any]] = field(default_factory=list)
    series_columns: list[str] = field(default_factory=list)
    series_limit: int = 0
    series_limit_metric: Any | None = None
    is_timeseries: bool = False
    result_type: str | None = None
    applied_time_extras: dict[str, str] = field(default_factory=dict)
    apply_fetch_values_predicate: bool = False
    is_rowcount: bool = False
    time_offsets: list[str] = field(default_factory=list)
    group_others_when_limit_reached: bool = False
    granularity_sqla: str | None = None

    def __post_init__(self) -> None:
        # P1-9: Deprecated field renaming — backward compatibility
        if self.granularity_sqla and not self.granularity:
            self.granularity = self.granularity_sqla

        # Metric normalization: {"label": "count"} → "count".
        # Preserve ``metrics is None`` (raw columns mode) — the
        # ``_build_sql`` logic uses the ``None`` vs ``[]`` distinction
        # to decide whether to aggregate, matching original
        # ``helpers.get_sqla_query:1731``: ``bool(metrics is not None
        # or groupby)``.
        if self.metrics is not None:
            normalized: list[Any] = []
            for m in self.metrics:
                if isinstance(m, dict) and set(m.keys()) == {"label"}:
                    normalized.append(m["label"])
                else:
                    normalized.append(m)
            self.metrics = normalized

        # P1-10: is_timeseries auto-detection
        if not self.is_timeseries:
            if "__timestamp" in (self.columns or []):
                self.is_timeseries = True

        # Formula annotation filtering (client-side only)
        if self.annotation_layers:
            self.annotation_layers = [
                a
                for a in self.annotation_layers
                if a.get("annotationType") != "FORMULA"
                and a.get("annotation_type") != "FORMULA"
            ]

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict matching Superset's QueryObject.to_dict().

        Excludes volatile fields (time_range, post_processing, annotation_layers,
        time_offsets, result_type, apply_fetch_values_predicate) — those are only
        included conditionally in cache_key().
        """
        return {
            "columns": self.columns,
            "metrics": self.metrics,
            "orderby": self.orderby,
            "filter": self.filters,
            "extras": self.extras,
            "time_shift": self.time_shift,
            "granularity": self.granularity,
            "row_limit": self.row_limit,
            "row_offset": self.row_offset,
            "from_dttm": self.from_dttm,
            "to_dttm": self.to_dttm,
            "inner_from_dttm": self.inner_from_dttm,
            "inner_to_dttm": self.inner_to_dttm,
            "order_desc": self.order_desc,
            "series_columns": self.series_columns,
            "series_limit": self.series_limit,
            "series_limit_metric": self.series_limit_metric,
            "is_timeseries": self.is_timeseries,
            "applied_time_extras": self.applied_time_extras,
            "apply_fetch_values_predicate": self.apply_fetch_values_predicate,
            "is_rowcount": self.is_rowcount,
            "group_others_when_limit_reached": self.group_others_when_limit_reached,
        }

    def cache_key(self) -> dict[str, Any]:
        """Return a dict suitable for cache-key computation.

        Matches Superset's QueryObject.cache_key() structure:
        - Starts from to_dict() (volatile fields already excluded)
        - Removes from_dttm, to_dttm, datasource
        - Conditionally removes apply_fetch_values_predicate when False
        - Conditionally adds result_type, time_range, post_processing,
          time_offsets when truthy
        - Includes filtered annotation_layers (9 specific fields per layer)
        """
        base = self.to_dict()
        # Remove volatile datetime bounds and datasource
        for key in ("from_dttm", "to_dttm", "datasource"):
            base.pop(key, None)
        # Conditionally remove apply_fetch_values_predicate when False
        if not self.apply_fetch_values_predicate:
            base.pop("apply_fetch_values_predicate", None)
        else:
            base["apply_fetch_values_predicate"] = True
        # Conditionally add fields when truthy
        if self.result_type:
            base["result_type"] = self.result_type
        if self.time_range:
            base["time_range"] = self.time_range
        if self.post_processing:
            base["post_processing"] = self.post_processing
        if self.time_offsets:
            base["time_offsets"] = self.time_offsets
        # Include filtered annotation_layers (specific fields only)
        _ANNOTATION_CACHE_FIELDS = {  # noqa: N806
            "annotationType",
            "descriptionColumns",
            "intervalEndColumn",
            "name",
            "overrides",
            "sourceType",
            "timeColumn",
            "titleColumn",
            "value",
        }
        if self.annotation_layers:
            base["annotation_layers"] = [
                {k: v for k, v in layer.items() if k in _ANNOTATION_CACHE_FIELDS}
                if isinstance(layer, dict)
                else layer
                for layer in self.annotation_layers
            ]
        return base

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate(self) -> None:
        """Run all sub-validators; raise *QueryObjectValidationError* on failure.

        Order matches Superset's QueryObject.validate().
        """
        self._validate_there_are_no_missing_series()
        self._validate_no_have_duplicate_labels()
        self._validate_time_offsets()
        self._sanitize_filters()

    def _sanitize_filters(self, datasource: Any | None = None) -> None:
        """Sanitize ``extras.where`` and ``extras.having`` via *sanitize_clause*.

        When a datasource is provided, processes Jinja templates and passes
        the engine to sanitize_clause for engine-specific escaping.
        """
        engine: str | None = None
        template_processor = None
        if datasource is not None:
            engine = getattr(getattr(datasource, "database", None), "backend", None)
            template_processor = get_template_processor(datasource)
        for clause_key in ("where", "having"):
            clause = self.extras.get(clause_key, "")
            if clause:
                try:
                    if template_processor is not None:
                        clause = template_processor.process_template(clause)
                    sanitize_clause(clause, engine=engine or "postgresql")
                    self.extras[clause_key] = clause
                except QueryObjectValidationError:
                    raise
                except Exception as ex:
                    raise QueryObjectValidationError(
                        f"Unsafe SQL in extras.{clause_key}: {ex}"
                    ) from ex

    def _validate_no_have_duplicate_labels(self) -> None:
        """Check that column / metric labels are unique."""
        labels: list[str] = []
        for col in self.columns:
            if isinstance(col, dict):
                label = col.get("label") or col.get("sqlExpression") or str(col)
            else:
                label = str(col)
            labels.append(label)
        for metric in self.metrics or []:
            if isinstance(metric, dict):
                label = metric.get("label") or str(metric)
            else:
                label = str(metric)
            labels.append(label)

        seen: set[str] = set()
        for label in labels:
            if label in seen:
                raise QueryObjectValidationError(f"Duplicate label found: {label}")
            seen.add(label)

    def _validate_time_offsets(self) -> None:
        """Validate that all *time_offsets* items are strings.

        Also checks the DATE_RANGE_TIMESHIFTS_ENABLED feature flag for
        date-range style offsets.
        """
        for offset in self.time_offsets:
            if not isinstance(offset, str):
                raise QueryObjectValidationError(
                    f"time_offsets must contain strings, got {type(offset).__name__}"
                )
            # A date range offset contains " : " as separator
            # (e.g. "2021-01-01 : 2021-12-31")
            if " : " in offset:
                if not feature_flag_manager.is_feature_enabled(
                    "DATE_RANGE_TIMESHIFTS_ENABLED"
                ):
                    raise QueryObjectValidationError(
                        "Date range time shifts are not enabled"
                    )

    def _validate_there_are_no_missing_series(self) -> None:
        """Check that every *series_columns* entry exists in *columns*."""
        if not self.series_columns:
            return
        col_labels: set[str] = set()
        for col in self.columns:
            if isinstance(col, dict):
                col_labels.add(col.get("label") or col.get("sqlExpression") or str(col))
            else:
                col_labels.add(str(col))
        for sc in self.series_columns:
            if sc not in col_labels:
                raise QueryObjectValidationError(
                    f"series_columns entry '{sc}' not found in columns"
                )

    @classmethod
    def from_request(cls, q: Any, datasource_ref: dict[str, Any]) -> AsyncQueryObject:
        """Create from a dict or ChartDataQueryObject schema struct."""
        if isinstance(q, dict):
            # P1-9: Deprecated field renaming fallbacks
            columns = q.get("columns", [])
            if not columns and q.get("groupby"):
                columns = q["groupby"]
            granularity = q.get("granularity")
            if not granularity and q.get("granularity_sqla"):
                granularity = q["granularity_sqla"]
            series_limit = q.get("series_limit", 0)
            if not series_limit and q.get("timeseries_limit"):
                series_limit = q["timeseries_limit"]
            series_limit_metric = q.get("series_limit_metric")
            if series_limit_metric is None and q.get("timeseries_limit_metric"):
                series_limit_metric = q["timeseries_limit_metric"]

            return cls(
                datasource=datasource_ref,
                columns=columns,
                metrics=q.get("metrics", []),
                filters=q.get("filters") or q.get("filter", []),
                extras=q.get("extras", {}),
                orderby=q.get("orderby", []),
                row_limit=q.get("row_limit"),
                row_offset=q.get("row_offset", 0),
                time_range=q.get("time_range"),
                time_shift=q.get("time_shift"),
                granularity=granularity,
                order_desc=q.get("order_desc", True),
                post_processing=q.get("post_processing", []),
                annotation_layers=q.get("annotation_layers", []),
                series_columns=q.get("series_columns", []),
                series_limit=series_limit,
                series_limit_metric=series_limit_metric,
                is_timeseries=q.get("is_timeseries", False),
                result_type=q.get("result_type"),
                applied_time_extras=q.get("applied_time_extras", {}),
                apply_fetch_values_predicate=q.get(
                    "apply_fetch_values_predicate", False
                ),
                is_rowcount=q.get("is_rowcount", False),
                time_offsets=q.get("time_offsets", []),
                group_others_when_limit_reached=q.get(
                    "group_others_when_limit_reached", False
                ),
                from_dttm=q.get("from_dttm"),
                to_dttm=q.get("to_dttm"),
                granularity_sqla=q.get("granularity_sqla"),
            )
        # ``q.metrics`` may be ``None`` (Table viz raw-mode) — preserve
        # it so ``_build_sql`` can skip aggregation.  ``[]`` (explicit
        # empty list) still means "aggregate with empty metric set".
        _q_metrics = getattr(q, "metrics", None)
        return cls(
            datasource=datasource_ref,
            columns=list(q.columns),
            metrics=(
                None
                if _q_metrics is None
                else [
                    m
                    if isinstance(m, str)
                    else msgspec.structs.asdict(m)
                    if isinstance(m, msgspec.Struct)
                    else vars(m)
                    if hasattr(m, "__dict__")
                    else m
                    for m in _q_metrics
                ]
            ),
            filters=(
                [{"col": f.col, "op": f.op, "val": f.val} for f in q.filters]
                if hasattr(q, "filters")
                else []
            ),
            extras=(
                msgspec.structs.asdict(q.extras)
                if hasattr(q, "extras")
                and q.extras is not None
                and isinstance(q.extras, msgspec.Struct)
                else vars(q.extras)
                if hasattr(q, "extras")
                and q.extras is not None
                and hasattr(q.extras, "__dict__")
                and not isinstance(q.extras, dict)
                else getattr(q, "extras", {}) or {}
            ),
            orderby=list(getattr(q, "orderby", [])),
            row_limit=q.row_limit,
            row_offset=getattr(q, "row_offset", 0),
            time_range=q.time_range,
            time_shift=getattr(q, "time_shift", None),
            granularity=q.granularity,
            order_desc=q.order_desc,
            post_processing=(
                [
                    {"operation": p.operation, "options": p.options}
                    for p in q.post_processing
                ]
                if hasattr(q, "post_processing")
                else []
            ),
            annotation_layers=(
                [
                    msgspec.structs.asdict(a) if isinstance(a, msgspec.Struct) else a
                    for a in getattr(q, "annotation_layers", [])
                ]
            ),
            series_columns=list(getattr(q, "series_columns", [])),
            series_limit=getattr(q, "series_limit", 0),
            series_limit_metric=getattr(q, "series_limit_metric", None),
            is_timeseries=getattr(q, "is_timeseries", False),
            result_type=getattr(q, "result_type", None),
            applied_time_extras=dict(getattr(q, "applied_time_extras", {})),
            apply_fetch_values_predicate=getattr(
                q, "apply_fetch_values_predicate", False
            ),
            is_rowcount=getattr(q, "is_rowcount", False),
            time_offsets=list(getattr(q, "time_offsets", [])),
            group_others_when_limit_reached=getattr(
                q, "group_others_when_limit_reached", False
            ),
            from_dttm=getattr(q, "from_dttm", None),
            to_dttm=getattr(q, "to_dttm", None),
            granularity_sqla=getattr(q, "granularity_sqla", None),
        )
