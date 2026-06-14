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

import collections
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import msgspec.structs
from jinja2.exceptions import TemplateError

from superset.exceptions import (
    QueryClauseValidationException,
    QueryObjectValidationError,
)
from superset.i18n import gettext as _
from superset.utils.feature_flags import feature_flag_manager
from superset.utils.sql import sanitize_clause

logger = logging.getLogger(__name__)


def _capped_row_limit(
    row_limit: int | None,
    result_type: str | None,
    server_pagination: bool | None = None,
) -> int:
    """Apply the configured row-limit cap to a request-supplied limit — 1:1
    with upstream ``QueryObjectFactory._process_row_limit``: an absent/zero
    limit falls back to ``SAMPLES_ROW_LIMIT`` (samples) or ``ROW_LIMIT``, and the
    result is capped at ``SQL_MAX_ROW`` (or ``TABLE_VIZ_MAX_ROW_SERVER`` when
    ``server_pagination`` is on — the Table viz pages server-side and so is
    allowed a higher ceiling). Without this, ``row_limit=0``/unset emitted an
    UNBOUNDED query and an oversized limit ran uncapped; without
    ``server_pagination`` threaded through, a server-paginated Table was capped
    at ``SQL_MAX_ROW`` instead of its higher ``TABLE_VIZ_MAX_ROW_SERVER``
    ceiling.
    """
    from superset import config as _config
    from superset.utils.core import apply_max_row_limit

    settings = None
    try:
        settings = _config.SupersetSettings()  # type: ignore[call-arg]
        default = (
            int(getattr(settings, "samples_row_limit", 1000))
            if result_type == "samples"
            else int(getattr(settings, "row_limit", 50000))
        )
    except Exception:  # noqa: BLE001
        default = 1000 if result_type == "samples" else 50000
    # Reuse the settings instance — building a second one inside
    # apply_max_row_limit doubled the pydantic construction per query object.
    return apply_max_row_limit(
        row_limit or default,
        server_pagination=server_pagination,
        settings=settings,
    )


@dataclass
class AsyncQueryObject:
    """Describes a single query to execute against a datasource.

    Mirrors superset.common.query_object.QueryObject fields for
    API contract compatibility. Does not depend on the legacy WSGI stack.
    """

    datasource: dict[str, Any]
    columns: list[Any] = field(default_factory=list)
    metrics: list[Any] | None = field(default_factory=list)
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
    # ``None`` = not supplied → auto-detect in ``__post_init__``; an explicit
    # ``False`` from the caller must STAY False, exactly like the original
    # ``_set_is_timeseries`` (superset_old/common/query_object.py:180-185).
    is_timeseries: bool | None = None
    result_type: str | None = None
    applied_time_extras: dict[str, str] = field(default_factory=dict)
    apply_fetch_values_predicate: bool = False
    is_rowcount: bool = False
    time_offsets: list[str] = field(default_factory=list)
    group_others_when_limit_reached: bool = False
    granularity_sqla: str | None = None

    def __post_init__(self) -> None:  # noqa: C901  # complex business logic
        # Deprecated field renaming — a truthy deprecated value overrides the
        # new field (1:1 upstream ``_rename_deprecated_fields``).
        if self.granularity_sqla:
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

        # Filter out None entries from post_processing.
        # 1:1 with ``superset_old/common/query_object.py:_set_post_processing``
        # (line 203-204): the original silently drops null entries so that
        # ``None.get("operation")`` cannot 500 in exec_post_processing.
        self.post_processing = [p for p in self.post_processing if p]

        # Capture the caller-supplied is_timeseries value *before* auto-detection
        # so that series_columns population uses the same semantics as the original
        # ``_init_series_columns(series_columns, metrics, is_timeseries)`` call
        # (superset_old/common/query_object.py:155,206-217): the original passes
        # the caller-supplied is_timeseries (bool|None) directly; when None is
        # passed, ``elif is_timeseries and metrics`` is False and series_columns
        # stays [].  Using the auto-detected self.is_timeseries here would cause
        # spurious series breakdowns when is_timeseries was implicitly detected
        # rather than explicitly set.
        _explicit_is_timeseries = self.is_timeseries

        # is_timeseries: 1:1 with ``_set_is_timeseries``
        # (superset_old/common/query_object.py:180-185) —
        # ``is_timeseries if is_timeseries is not None else DTTM_ALIAS in
        # columns``: auto-detection runs ONLY when the caller did not supply
        # the flag; an explicit False is preserved.
        if self.is_timeseries is None:
            self.is_timeseries = "__timestamp" in (self.columns or [])

        # Auto-populate series_columns from columns when unset for a time-series
        # chart with metrics.
        # 1:1 with ``superset_old/common/query_object.py:_init_series_columns``
        # (lines 206-217): if series_columns is empty but is_timeseries=True and
        # metrics is non-empty, use columns as the series key so that time-series
        # charts have the correct series breakdown without requiring callers to
        # explicitly set series_columns.
        # NOTE: Use ``_explicit_is_timeseries`` (the caller-supplied value) not
        # ``self.is_timeseries`` (which may have been set by auto-detection above)
        # to preserve the original semantics — see comment above.
        if not self.series_columns and _explicit_is_timeseries and self.metrics:
            self.series_columns = list(self.columns)

        # Extract the effective time range for dttm resolution when not supplied
        # at the top level.
        # 1:1 with ``superset_old/common/query_object_factory.py:_process_time_range``
        # (lines 127-152): uses a LOCAL variable only — self.time_range stays None
        # when the caller passed None.  The original factory passes time_range=None
        # to QueryObject.__init__ (line 86: ``time_range=time_range``, not
        # ``processed_time_range``) so that _apply_filters() sees a falsy
        # time_range and leaves every TEMPORAL_RANGE filter's val intact.
        # Storing the extracted value in self.time_range would cause _apply_filters()
        # to overwrite all TEMPORAL_RANGE filter vals (including unrelated ones) with
        # the chosen val — silently wrong for charts with multiple temporal filters.
        _extracted_time_range: str | None = None
        if self.time_range is None:
            from superset.constants import NO_TIME_RANGE

            # 1:1 with _process_time_range (superset_old/common/
            # query_object_factory.py:128-152): default NO_TIME_RANGE, and the
            # matched filter's RAW val — a None/"" val is passed through
            # as-is (get_since_until(None) then resolves to (None, ~today)),
            # NOT coerced to NO_TIME_RANGE (which would yield (None, None)).
            _extracted_time_range = NO_TIME_RANGE
            temporal_flts = [
                flt for flt in self.filters if flt.get("op") == "TEMPORAL_RANGE"
            ]
            if temporal_flts:
                from superset.utils.column import get_x_axis_label

                x_axis_label = get_x_axis_label(self.columns)
                match_flt = [
                    flt for flt in temporal_flts if flt.get("col") == x_axis_label
                ]
                if match_flt:
                    _extracted_time_range = match_flt[0].get("val")
                else:
                    _extracted_time_range = temporal_flts[0].get("val")
            # NOTE: self.time_range remains None so _apply_filters() is a no-op,
            # preserving every filter's original val (matches original behavior).

        # Resolve from_dttm/to_dttm when not explicitly provided.
        # Mirrors original superset_old/common/query_object_factory.py:74-81,
        # which calls get_since_until_from_time_range(processed_time_range, …)
        # and stores the result as from_dttm/to_dttm on the QueryObject.
        # Uses _effective_time_range so that both explicit-time_range and
        # filter-only cases resolve dttm correctly.
        # ``is not None`` (not truthiness): a None/"" extracted val must reach
        # get_since_until_from_time_range like the original, which calls it
        # unconditionally (superset_old/common/query_object_factory.py:74-81).
        _effective_time_range = (
            self.time_range if self.time_range is not None else _extracted_time_range
        )
        if self.from_dttm is None or self.to_dttm is None:
            try:
                # 1:1 with upstream factory: use
                # ``get_since_until_from_time_range(time_range, time_shift, extras)``
                # — NOT bare ``get_since_until`` — so config-driven relative-time
                # anchors (``default_relative_start_time``/``...end_time``) and
                # per-request ``extras`` overrides (``relative_start``/
                # ``relative_end``/``instant_time_comparison_range``) are honored.
                from superset.utils.date import get_since_until_from_time_range

                parsed_from, parsed_to = get_since_until_from_time_range(
                    _effective_time_range,
                    self.time_shift,
                    self.extras,
                )
                if self.from_dttm is None:
                    self.from_dttm = parsed_from
                if self.to_dttm is None:
                    self.to_dttm = parsed_to
            except Exception:  # noqa: BLE001, S110
                # Malformed time_range — leave dttms None, emit un-filtered SQL
                pass

        # 1:1 with ``QueryContextFactory._apply_filters``
        # (``superset_old/common/query_context_factory.py:199-204``): when a
        # top-level ``time_range`` is set, every ``TEMPORAL_RANGE`` filter's
        # ``val`` is overwritten with it so the WHERE clause matches the
        # request's effective time range (the Explore UI sends the canonical
        # range as ``time_range`` while leaving stale ``val`` strings on the
        # adhoc temporal filters). Upstream's factory runs this on every query
        # object via ``_process_query_object``; here ``__post_init__`` is the
        # equivalent build hook.
        self._apply_filters()

        # Formula annotation filtering (client-side only)
        if self.annotation_layers:
            self.annotation_layers = [
                a
                for a in self.annotation_layers
                if a.get("annotationType") != "FORMULA"
                and a.get("annotation_type") != "FORMULA"
            ]

    def _apply_filters(self) -> None:
        """1:1 with ``QueryContextFactory._apply_filters``.

        When ``time_range`` is set, sync every ``TEMPORAL_RANGE`` filter's
        ``val`` to it.
        """
        if self.time_range:
            for filter_object in self.filters:
                if filter_object.get("op") == "TEMPORAL_RANGE":
                    filter_object["val"] = self.time_range

    def apply_granularity(  # noqa: C901
        self,
        form_data: dict[str, Any] | None,
        datasource: Any,
    ) -> None:
        """1:1 with ``QueryContextFactory._apply_granularity``.

        Replaces a temporal x-axis column's expression with the granularity and
        removes the now-redundant temporal filter (a fresh one keyed on the
        granularity is added later by the SQL build). Requires the request
        ``form_data`` (for ``x_axis``) and the resolved datasource model (for
        its temporal columns), so — unlike :meth:`_apply_filters` — it cannot
        run in ``__post_init__`` and must be invoked by the context builder once
        those are available. A no-op when there is no ``granularity``.
        """
        from superset.utils.column import is_adhoc_column

        granularity = self.granularity
        if not granularity:
            return

        temporal_columns = {
            column["column_name"] if isinstance(column, dict) else column.column_name
            for column in getattr(datasource, "columns", []) or []
            if (column["is_dttm"] if isinstance(column, dict) else column.is_dttm)
        }
        x_axis = form_data and form_data.get("x_axis")

        filter_to_remove = None
        if is_adhoc_column(x_axis):  # type: ignore[arg-type]
            x_axis = x_axis.get("sqlExpression")  # type: ignore[union-attr]
        if x_axis and x_axis in temporal_columns:
            filter_to_remove = x_axis
            x_axis_column = next(
                (
                    column
                    for column in self.columns
                    if column == x_axis
                    or (
                        isinstance(column, dict)
                        and column.get("sqlExpression") == x_axis
                    )
                ),
                None,
            )
            # Replace the x-axis column's values with the granularity.
            if x_axis_column:
                if isinstance(x_axis_column, dict):
                    x_axis_column["sqlExpression"] = granularity
                    x_axis_column["label"] = granularity
                else:
                    self.columns = [
                        granularity if column == x_axis_column else column
                        for column in self.columns
                    ]
                for post_processing in self.post_processing:
                    if post_processing.get("operation") == "pivot":
                        post_processing["options"]["index"] = [granularity]

        # If no temporal x-axis, pick the default temporal filter to remove.
        if not filter_to_remove:
            temporal_filters = [
                flt["col"] for flt in self.filters if flt.get("op") == "TEMPORAL_RANGE"
            ]
            if len(temporal_filters) > 0:
                if granularity in temporal_filters:
                    filter_to_remove = granularity
                else:
                    filter_to_remove = temporal_filters[0]

        # Remove the temporal filter (x-axis or other). A granularity-keyed
        # filter is re-added downstream — this replaces the prior default
        # temporal filter.
        if is_adhoc_column(filter_to_remove):  # type: ignore[arg-type]
            filter_to_remove = filter_to_remove.get("sqlExpression")  # type: ignore[union-attr]

        if filter_to_remove:
            self.filters = [
                flt for flt in self.filters if flt.get("col") != filter_to_remove
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

    def validate(self, datasource: Any | None = None) -> None:
        """Run all sub-validators; raise *QueryObjectValidationError* on failure.

        Order matches Superset's QueryObject.validate().

        Args:
            datasource: The resolved ORM datasource model (e.g.
                ``SqlaTable``).  When provided, ``_sanitize_filters`` uses its
                ``database`` to render Jinja templates in ``extras.where`` /
                ``extras.having`` and to select the correct SQL dialect for
                clause sanitization -- matching the original
                ``superset_old/common/query_object.py:_sanitize_filters``.
        """
        self._validate_there_are_no_missing_series()
        self._validate_no_have_duplicate_labels(datasource=datasource)
        self._validate_time_offsets()
        self._sanitize_filters(datasource=datasource)

    def _sanitize_filters(self, datasource: Any | None = None) -> None:
        """Sanitize ``extras.where`` and ``extras.having``.

        1:1 with ``superset_old/common/query_object.py::_sanitize_filters``:
        when a datasource is available the clause is first rendered through
        the sandboxed Jinja processor, then sanitized with the correct
        engine dialect via :func:`sanitize_clause`.
        """
        # Lazy import like upstream: jinja_context pulls in models/security.
        from superset.jinja_context import get_template_processor

        for param in ("where", "having"):
            clause = self.extras.get(param)
            if clause and datasource:
                try:
                    database = datasource.database
                    processor = get_template_processor(database=database)
                    try:
                        clause = processor.process_template(clause, force=True)
                    except TemplateError as ex:
                        raise QueryObjectValidationError(
                            _(
                                "Error in jinja expression in WHERE clause: %(msg)s",
                                msg=ex.message,
                            )
                        ) from ex
                    engine = database.db_engine_spec.engine
                    sanitized_clause = sanitize_clause(clause, engine)
                    if sanitized_clause != clause:
                        self.extras[param] = sanitized_clause
                except QueryClauseValidationException as ex:
                    raise QueryObjectValidationError(ex.message) from ex

    def _validate_no_have_duplicate_labels(self, datasource: Any | None = None) -> None:
        """Check that column / metric labels are unique.

        1:1 with
        ``superset_old/common/query_object.py:_validate_no_have_duplicate_labels``
        (lines 294-304): uses ``get_metric_names`` (which applies the datasource
        ``verbose_map`` to produce display labels) and ``get_column_names``, then
        reports ALL duplicate labels in a single i18n error message.
        """
        from superset.utils.column import get_column_names, get_metric_names

        # Retrieve datasource verbose_map if available — mirrors
        # ``QueryObject.metric_names`` property which passes it to
        # ``get_metric_names``.
        verbose_map: dict[str, Any] | None = None
        if datasource and hasattr(datasource, "verbose_map"):
            verbose_map = datasource.verbose_map

        all_labels = get_metric_names(
            self.metrics or [], verbose_map
        ) + get_column_names(self.columns)
        if len(set(all_labels)) < len(all_labels):
            dup_labels = [
                item
                for item, count in collections.Counter(all_labels).items()
                if count > 1
            ]
            raise QueryObjectValidationError(
                _(
                    "Duplicate column/metric labels: %(labels)s. Please make "
                    "sure all columns and metrics have a unique label.",
                    labels=", ".join(f'"{x}"' for x in dup_labels),
                )
            )

    @staticmethod
    def _is_valid_date_range(date_range: str) -> bool:
        """Return True if *date_range* is a valid YYYY-MM-DD:YYYY-MM-DD offset.

        Mirrors ``superset_old/common/query_object.py:_is_valid_date_range``
        (lines 323-335): split on ':', strip both sides, validate with strptime.
        No surrounding-space requirement — '2021-01-01:2021-12-31' is valid.
        """
        try:
            start_date, end_date = date_range.split(":")
            datetime.strptime(start_date.strip(), "%Y-%m-%d")
            datetime.strptime(end_date.strip(), "%Y-%m-%d")
            return True
        except ValueError:
            return False

    def _validate_time_offsets(self) -> None:
        """Validate date-range style offsets against the feature flag.

        Mirrors ``superset_old/common/query_object.py:_validate_time_offsets``
        (lines 306-321): uses ``_is_valid_date_range`` (strptime-based) instead
        of a simple substring check, and restores the original error message.
        NO isinstance(str) pre-check — the original lets a non-string offset
        crash with AttributeError inside ``.split(":")`` (→ 500), and adding a
        400 here would change the observable status for that input.
        """
        for offset in self.time_offsets:
            if self._is_valid_date_range(offset):
                if not feature_flag_manager.is_feature_enabled(
                    "DATE_RANGE_TIMESHIFTS_ENABLED"
                ):
                    raise QueryObjectValidationError(
                        "Date range timeshifts are not enabled. "
                        "Please contact your administrator to enable the "
                        "DATE_RANGE_TIMESHIFTS_ENABLED feature flag."
                    )

    def _validate_there_are_no_missing_series(self) -> None:
        """Check that every *series_columns* entry exists in *columns*.

        1:1 with ``superset_old/common/query_object.py:362-371``: uses list
        membership (``col not in self.columns``) so that adhoc-column dicts in
        series_columns are compared via ``__eq__`` rather than ``__hash__``.
        Building a ``set[str]`` of labels and checking dict membership in that
        set raises ``TypeError: unhashable type: 'dict'`` whenever
        series_columns was auto-populated from self.columns (which can contain
        adhoc column dicts), producing a spurious HTTP 500.
        """
        missing_series = [col for col in self.series_columns if col not in self.columns]
        if missing_series:
            raise QueryObjectValidationError(
                _(
                    "The following entries in `series_columns` are missing "
                    "in `columns`: %(columns)s. ",
                    columns=", ".join(f'"{x}"' for x in missing_series),
                )
            )

    @classmethod
    def from_request(cls, q: Any, datasource_ref: dict[str, Any]) -> AsyncQueryObject:
        """Create from a dict or ChartDataQueryObject schema struct."""
        if isinstance(q, dict):
            # Deprecated field renaming. 1:1 with upstream
            # ``QueryObject._rename_deprecated_fields``: a present, truthy
            # deprecated value OVERRIDES the new field (not just a fallback when
            # the new field is empty).
            columns = q.get("columns", [])
            if q.get("groupby"):
                columns = q["groupby"]
            granularity = q.get("granularity")
            if q.get("granularity_sqla"):
                granularity = q["granularity_sqla"]
            series_limit = q.get("series_limit", 0)
            if q.get("timeseries_limit"):
                series_limit = q["timeseries_limit"]
            series_limit_metric = q.get("series_limit_metric")
            if q.get("timeseries_limit_metric"):
                series_limit_metric = q["timeseries_limit_metric"]

            return cls(
                datasource=datasource_ref,
                columns=columns,
                metrics=q.get("metrics", []),
                filters=q.get("filters") or q.get("filter", []),
                extras=q.get("extras", {}),
                orderby=q.get("orderby", []),
                row_limit=_capped_row_limit(
                    q.get("row_limit"),
                    q.get("result_type"),
                    server_pagination=bool(q.get("server_pagination")),
                ),
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
                # ``None`` (not False) when absent → __post_init__ auto-detects
                # ``__timestamp in columns`` — 1:1 upstream _set_is_timeseries
                # (R11-15: a False default killed the auto-detection).
                is_timeseries=q.get("is_timeseries"),
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
            row_limit=_capped_row_limit(
                q.row_limit,
                getattr(q, "result_type", None),
                server_pagination=bool(getattr(q, "server_pagination", False)),
            ),
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
            # ``None`` default — see the dict-path note above (R11-15).
            is_timeseries=getattr(q, "is_timeseries", None),
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
