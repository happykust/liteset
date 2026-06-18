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
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from superset.common.query_object import AsyncQueryObject, QueryObjectValidationError
from superset.exceptions import QueryClauseValidationException


def _make_datasource(engine: str = "postgresql") -> MagicMock:
    """Build a datasource mock exposing ``.database.db_engine_spec.engine``.

    Mirrors the ORM datasource model the real ``validate()`` receives: it must
    provide a ``database`` (consumed by ``get_template_processor``) and the
    ``database.db_engine_spec.engine`` dialect string passed to
    ``sanitize_clause``.
    """
    datasource = MagicMock()
    datasource.database.db_engine_spec.engine = engine
    return datasource


def test_query_object_defaults():
    qo = AsyncQueryObject(datasource={"type": "table", "id": 1})
    assert qo.datasource == {"type": "table", "id": 1}
    assert qo.columns == []
    assert qo.metrics == []
    assert qo.filters == []
    assert qo.row_limit is None
    assert qo.row_offset == 0
    assert qo.time_range is None
    assert qo.order_desc is True
    assert qo.post_processing == []


def test_query_object_with_all_fields():
    qo = AsyncQueryObject(
        datasource={"type": "table", "id": 1},
        columns=["col1", "col2"],
        metrics=["count"],
        filters=[{"col": "x", "op": "==", "val": 1}],
        row_limit=100,
        row_offset=10,
        time_range="Last 7 days",
        granularity="day",
        order_desc=False,
        extras={"time_grain_sqla": "P1D"},
        post_processing=[{"operation": "pivot"}],
    )
    assert qo.columns == ["col1", "col2"]
    assert qo.row_limit == 100
    assert qo.granularity == "day"
    assert len(qo.post_processing) == 1


def test_query_object_time_shift():
    qo = AsyncQueryObject(
        datasource={"type": "table", "id": 1},
        time_shift="1 week ago",
    )
    assert qo.time_shift == "1 week ago"


def test_query_object_to_dict():
    qo = AsyncQueryObject(
        datasource={"type": "table", "id": 1},
        columns=["col1"],
        row_limit=50,
        filters=[{"col": "x", "op": "==", "val": 1}],
    )
    d = qo.to_dict()
    assert "datasource" not in d
    assert d["columns"] == ["col1"]
    assert d["row_limit"] == 50
    # Superset uses "filter" not "filters"
    assert "filter" in d
    assert "filters" not in d
    assert d["filter"] == [{"col": "x", "op": "==", "val": 1}]


def test_empty_datasource_dict():
    """Empty datasource dict is accepted without error."""
    qo = AsyncQueryObject(datasource={})
    assert qo.datasource == {}
    assert qo.columns == []
    d = qo.to_dict()
    assert "datasource" not in d


def test_to_dict_round_trip():
    """to_dict() output can reconstruct an equivalent AsyncQueryObject."""
    original = AsyncQueryObject(
        datasource={"type": "table", "id": 42},
        columns=["col_a", "col_b"],
        metrics=["sum_x"],
        filters=[{"col": "status", "op": "==", "val": "active"}],
        row_limit=200,
        row_offset=5,
        time_range="Last 30 days",
        granularity="hour",
        order_desc=False,
        extras={"time_grain_sqla": "PT1H"},
        post_processing=[{"operation": "sort", "options": {"by": "col_a"}}],
        annotation_layers=[{"sourceType": "NATIVE", "value": 1, "name": "ann"}],
        series_columns=["region"],
        series_limit=10,
        is_timeseries=True,
    )
    d = original.to_dict()
    # to_dict() no longer includes "datasource" — pass it separately
    rebuilt = AsyncQueryObject.from_request(d, {"type": "table", "id": 42})
    assert rebuilt.to_dict() == original.to_dict()


def test_time_shift_various_formats():
    """Time shift accepts various string formats."""
    for shift in ["1 week ago", "1 year", "28 days", "P1M", "inherit"]:
        qo = AsyncQueryObject(datasource={"type": "table", "id": 1}, time_shift=shift)
        assert qo.time_shift == shift


def test_post_processing_chain_multiple_operations():
    """Multiple post-processing operations are preserved in order."""
    ops = [
        {"operation": "pivot", "options": {"index": ["ds"]}},
        {"operation": "flatten"},
        {"operation": "sort", "options": {"by": "metric", "ascending": False}},
    ]
    qo = AsyncQueryObject(datasource={"type": "table", "id": 1}, post_processing=ops)
    assert len(qo.post_processing) == 3
    assert qo.post_processing[0]["operation"] == "pivot"
    assert qo.post_processing[1]["operation"] == "flatten"
    assert qo.post_processing[2]["operation"] == "sort"
    # post_processing excluded from to_dict() (matches Superset), present in cache_key()
    d = qo.to_dict()
    assert "post_processing" not in d
    ck = qo.cache_key()
    assert ck["post_processing"] == ops


def test_annotation_layers_various_shapes():
    """Annotation layers with different config shapes are stored correctly."""
    layers = [
        {"sourceType": "NATIVE", "value": 1, "name": "native_layer"},
        {"sourceType": "line", "value": 5, "name": "line_layer", "color": "#ff0000"},
        {"sourceType": "table", "value": 3, "name": "table_layer", "extra": {"k": "v"}},
    ]
    qo = AsyncQueryObject(
        datasource={"type": "table", "id": 1}, annotation_layers=layers
    )
    assert len(qo.annotation_layers) == 3
    assert qo.annotation_layers[1]["color"] == "#ff0000"
    assert qo.annotation_layers[2]["extra"] == {"k": "v"}


def test_orderby_field():
    """orderby accepts list of (expression, ascending) tuples."""
    qo = AsyncQueryObject(
        datasource={"type": "table", "id": 1},
        orderby=[("col1", True), ("col2", False)],
    )
    assert len(qo.orderby) == 2
    assert qo.orderby[0] == ("col1", True)
    assert qo.orderby[1] == ("col2", False)
    d = qo.to_dict()
    # dataclasses.asdict() preserves tuples of primitives inside lists
    assert d["orderby"] == [("col1", True), ("col2", False)]


def test_series_columns_and_series_limit():
    """series_columns and series_limit are preserved."""
    qo = AsyncQueryObject(
        datasource={"type": "table", "id": 1},
        series_columns=["region", "country"],
        series_limit=25,
        series_limit_metric="count",
    )
    assert qo.series_columns == ["region", "country"]
    assert qo.series_limit == 25
    assert qo.series_limit_metric == "count"


def test_from_request_dict():
    qo = AsyncQueryObject.from_request(
        {"columns": ["a"], "metrics": ["count"], "row_limit": 10},
        {"type": "table", "id": 1},
    )
    assert qo.columns == ["a"]
    assert qo.row_limit == 10


def test_from_request_struct():
    # Test with a mock struct-like object
    from unittest.mock import MagicMock

    q = MagicMock()
    q.columns = ["b"]
    q.metrics = []
    q.filters = []
    q.row_limit = 5
    q.time_range = "Last 7 days"
    q.granularity = "day"
    q.order_desc = False
    q.post_processing = []
    q.extras = {}
    q.orderby = []
    q.time_shift = None
    q.annotation_layers = []
    q.series_columns = []
    q.series_limit = 0
    q.series_limit_metric = None
    q.is_timeseries = False
    q.result_type = None
    q.applied_time_extras = {}
    qo = AsyncQueryObject.from_request(q, {"type": "table", "id": 2})
    assert qo.columns == ["b"]
    assert qo.row_limit == 5


def test_from_request_struct_preserves_adhoc_metrics():
    """Adhoc metrics (struct objects) are converted to dicts, not label strings."""
    from types import SimpleNamespace

    adhoc = SimpleNamespace(
        expressionType="SIMPLE",
        column={"column_name": "revenue"},
        aggregate="SUM",
        label="SUM(revenue)",
    )
    q = SimpleNamespace(
        columns=["col"],
        metrics=["count", adhoc],
        filters=[],
        extras={},
        orderby=[],
        row_limit=100,
        row_offset=0,
        time_range=None,
        time_shift=None,
        granularity=None,
        order_desc=True,
        post_processing=[],
        annotation_layers=[],
        series_columns=[],
        series_limit=0,
        series_limit_metric=None,
        is_timeseries=False,
        result_type=None,
        applied_time_extras={},
        apply_fetch_values_predicate=False,
        is_rowcount=False,
        time_offsets=[],
        group_others_when_limit_reached=False,
    )
    qo = AsyncQueryObject.from_request(q, {"type": "table", "id": 1})
    assert qo.metrics[0] == "count"
    # Adhoc metric should be a dict, not a label string
    assert isinstance(qo.metrics[1], dict)
    assert qo.metrics[1]["aggregate"] == "SUM"
    assert qo.metrics[1]["label"] == "SUM(revenue)"


def test_from_request_struct_converts_extras_to_dict():
    """Extras passed as a struct object are converted to a plain dict."""
    from types import SimpleNamespace

    extras_struct = SimpleNamespace(
        time_grain_sqla="P1D",
        having="",
        where="",
    )
    q = SimpleNamespace(
        columns=[],
        metrics=[],
        filters=[],
        extras=extras_struct,
        orderby=[],
        row_limit=None,
        row_offset=0,
        time_range=None,
        time_shift=None,
        granularity=None,
        order_desc=True,
        post_processing=[],
        annotation_layers=[],
        series_columns=[],
        series_limit=0,
        series_limit_metric=None,
        is_timeseries=False,
        result_type=None,
        applied_time_extras={},
        apply_fetch_values_predicate=False,
        is_rowcount=False,
        time_offsets=[],
        group_others_when_limit_reached=False,
    )
    qo = AsyncQueryObject.from_request(q, {"type": "table", "id": 1})
    assert isinstance(qo.extras, dict)
    assert qo.extras["time_grain_sqla"] == "P1D"


# ------------------------------------------------------------------
# to_dict / cache_key tests
# ------------------------------------------------------------------


def test_query_object_to_dict_filter_key():
    """to_dict() must use 'filter' (not 'filters') for Superset compat."""
    qo = AsyncQueryObject(
        datasource={"type": "table", "id": 1},
        filters=[{"col": "status", "op": "==", "val": "active"}],
    )
    d = qo.to_dict()
    assert "filter" in d
    assert "filters" not in d
    assert d["filter"] == [{"col": "status", "op": "==", "val": "active"}]


def test_cache_key_excludes_volatile_fields():
    """cache_key() must exclude from_dttm, to_dttm, datasource.

    time_range, annotation_layers, post_processing, time_offsets, result_type
    are conditionally included when truthy (matches Superset).
    """
    qo = AsyncQueryObject(
        datasource={"type": "table", "id": 1},
        columns=["col1"],
        from_dttm="2024-01-01",
        to_dttm="2024-12-31",
        time_range="Last year",
        annotation_layers=[{"name": "ann"}],
        post_processing=[{"operation": "pivot"}],
        time_offsets=["1 year ago"],
        result_type="full",
    )
    ck = qo.cache_key()
    # Always excluded
    for excluded in ("from_dttm", "to_dttm", "datasource"):
        assert excluded not in ck
    # Conditionally included when truthy
    assert ck["time_range"] == "Last year"
    assert ck["post_processing"] == [{"operation": "pivot"}]
    assert ck["time_offsets"] == ["1 year ago"]
    assert ck["result_type"] == "full"
    assert "annotation_layers" in ck
    # Retained fields should still be present
    assert ck["columns"] == ["col1"]
    assert "filter" in ck


def test_cache_key_excludes_when_falsy():
    """cache_key() excludes conditional fields when they are falsy."""
    qo = AsyncQueryObject(
        datasource={"type": "table", "id": 1},
        columns=["col1"],
    )
    ck = qo.cache_key()
    for excluded in (
        "from_dttm",
        "to_dttm",
        "datasource",
        "time_range",
        "annotation_layers",
        "post_processing",
        "time_offsets",
        "result_type",
    ):
        assert excluded not in ck


# ------------------------------------------------------------------
# validate() tests
# ------------------------------------------------------------------


def test_validate_sanitizes_where_clause():
    """validate() sanitizes extras.where via sanitize_clause(clause, engine).

    Sanitization runs only when both the clause and the datasource are present,
    ``sanitize_clause`` is called with the engine dialect, and a
    ``QueryClauseValidationException`` is converted into a
    ``QueryObjectValidationError``.
    """
    qo = AsyncQueryObject(
        datasource={"type": "table", "id": 1},
        extras={"where": "1=1"},
    )
    datasource = _make_datasource("postgresql")
    seen: dict[str, str] = {}

    def fake_sanitize(clause: str, engine: str) -> str:
        seen["clause"] = clause
        seen["engine"] = engine
        raise QueryClauseValidationException("Unsafe clause detected")

    processor = MagicMock()
    processor.process_template.side_effect = lambda clause, **kw: clause

    with (
        patch("superset.common.query_object.sanitize_clause", fake_sanitize),
        patch(
            "superset.jinja_context.get_template_processor",
            return_value=processor,
        ),
    ):
        with pytest.raises(QueryObjectValidationError, match="Unsafe clause detected"):
            qo.validate(datasource=datasource)

    assert seen == {"clause": "1=1", "engine": "postgresql"}


def test_validate_sanitizes_having_clause():
    """validate() sanitizes extras.having via sanitize_clause(clause, engine).

    Same contract as the WHERE case but for the HAVING clause.
    """
    qo = AsyncQueryObject(
        datasource={"type": "table", "id": 1},
        extras={"having": "1=1; DROP TABLE"},
    )
    datasource = _make_datasource("postgresql")
    seen: dict[str, str] = {}

    def fake_sanitize(clause: str, engine: str) -> str:
        seen["clause"] = clause
        seen["engine"] = engine
        raise QueryClauseValidationException("bad having")

    processor = MagicMock()
    processor.process_template.side_effect = lambda clause, **kw: clause

    with (
        patch("superset.common.query_object.sanitize_clause", fake_sanitize),
        patch(
            "superset.jinja_context.get_template_processor",
            return_value=processor,
        ),
    ):
        with pytest.raises(QueryObjectValidationError, match="bad having"):
            qo.validate(datasource=datasource)

    assert seen == {"clause": "1=1; DROP TABLE", "engine": "postgresql"}


def test_validate_passes_when_sanitize_clause_unavailable():
    """When sanitize_clause is None, _sanitize_filters skips sanitization."""
    qo = AsyncQueryObject(
        datasource={"type": "table", "id": 1},
        extras={"where": "anything"},
    )

    def noop_sanitize(clause: str, engine: str = "postgresql") -> str:
        return clause

    with patch("superset.common.query_object.sanitize_clause", noop_sanitize):
        # Should not raise when sanitize_clause is a no-op
        qo._sanitize_filters()


def test_validate_rejects_duplicate_labels():
    """validate() raises on duplicate column/metric labels."""
    qo = AsyncQueryObject(
        datasource={"type": "table", "id": 1},
        columns=["revenue", "revenue"],
    )
    with pytest.raises(
        QueryObjectValidationError, match="Duplicate column/metric labels.*revenue"
    ):
        qo.validate()


def test_validate_rejects_duplicate_metric_labels():
    """Duplicate labels across columns and metrics are rejected."""
    qo = AsyncQueryObject(
        datasource={"type": "table", "id": 1},
        columns=["col1"],
        metrics=[{"label": "col1", "expressionType": "SIMPLE"}],
    )
    with pytest.raises(
        QueryObjectValidationError, match="Duplicate column/metric labels.*col1"
    ):
        qo.validate()


def test_validate_time_offsets_non_string_raises_attribute_error():
    """A non-string offset crashes with AttributeError.

    ``_validate_time_offsets`` has no isinstance guard: the offset goes
    straight into ``date_range.split(":")`` and a non-string raises
    AttributeError → HTTP 500, NOT a 400 validation error.
    """
    qo = AsyncQueryObject(
        datasource={"type": "table", "id": 1},
        time_offsets=[123],  # type: ignore[list-item]
    )
    with pytest.raises(AttributeError):
        qo.validate()


def test_validate_missing_series_columns():
    """series_columns entries must exist in columns."""
    qo = AsyncQueryObject(
        datasource={"type": "table", "id": 1},
        columns=["col1", "col2"],
        series_columns=["col3"],
    )
    # Expected error message:
    # "The following entries in `series_columns` are missing in `columns`: "col3". "
    with pytest.raises(
        QueryObjectValidationError,
        match=r'series_columns.*are missing.*"col3"',
    ):
        qo.validate()


def test_validate_passes_for_valid_query_object():
    """A well-formed query object passes validation without error."""
    qo = AsyncQueryObject(
        datasource={"type": "table", "id": 1},
        columns=["col1", "col2"],
        metrics=["count"],
        series_columns=["col1"],
        time_offsets=["1 year ago"],
    )
    with patch("superset.common.query_object.sanitize_clause", None):
        qo.validate()  # should not raise


# ---------------------------------------------------------------------------
# 1:1 parity: _apply_filters syncs TEMPORAL_RANGE filter val to time_range
# ---------------------------------------------------------------------------


def test_apply_filters_syncs_temporal_range_to_time_range():
    qo = AsyncQueryObject(
        datasource={"type": "table", "id": 1},
        time_range="2020-01-01 : 2021-01-01",
        filters=[
            {"col": "ds", "op": "TEMPORAL_RANGE", "val": "Last week"},
            {"col": "name", "op": "==", "val": "foo"},
        ],
    )
    # __post_init__ runs _apply_filters automatically.
    temporal = next(f for f in qo.filters if f["op"] == "TEMPORAL_RANGE")
    assert temporal["val"] == "2020-01-01 : 2021-01-01"
    # Non-temporal filter is untouched.
    other = next(f for f in qo.filters if f["op"] == "==")
    assert other["val"] == "foo"


def test_apply_filters_noop_without_time_range():
    """When time_range=None, _apply_filters must be a no-op — filter vals unchanged.

    The guard ``if query_object.time_range:`` is False when time_range=None,
    so no filter val is modified.
    """
    qo = AsyncQueryObject(
        datasource={"type": "table", "id": 1},
        filters=[{"col": "ds", "op": "TEMPORAL_RANGE", "val": "Last week"}],
    )
    assert qo.filters[0]["val"] == "Last week"
    # self.time_range must remain None (not extracted from filter).
    assert qo.time_range is None


def test_apply_filters_noop_preserves_multiple_temporal_filters():
    """When time_range=None, multiple TEMPORAL_RANGE filters must keep their own vals.

    Regression: the previous code set self.time_range from the first/x-axis filter,
    then _apply_filters() overwrote ALL TEMPORAL_RANGE filter vals with that single
    val — silently corrupting comparison charts that use two time periods.

    ``QueryObjectFactory`` always passes ``time_range=time_range`` (the caller's
    None) to QueryObject, so each filter retains its own val.
    """
    qo = AsyncQueryObject(
        datasource={"type": "table", "id": 1},
        time_range=None,
        filters=[
            {"col": "ds1", "op": "TEMPORAL_RANGE", "val": "Last week"},
            {"col": "ds2", "op": "TEMPORAL_RANGE", "val": "Last month"},
        ],
    )
    # Neither filter val must be overwritten.
    assert qo.filters[0]["val"] == "Last week"
    assert qo.filters[1]["val"] == "Last month"
    # self.time_range must stay None.
    assert qo.time_range is None


def test_cache_key_excludes_time_range_when_none():
    """cache_key() must omit time_range when it was not supplied by the caller.

    Regression: previous code set self.time_range from a TEMPORAL_RANGE filter,
    causing cache_key() to include time_range even when the original caller passed
    None — producing cache keys that differ from the original QueryObject.cache_key().
    """
    qo = AsyncQueryObject(
        datasource={"type": "table", "id": 1},
        time_range=None,
        filters=[{"col": "ds", "op": "TEMPORAL_RANGE", "val": "Last week"}],
    )
    ck = qo.cache_key()
    assert "time_range" not in ck


def test_dttm_resolved_from_temporal_filter_when_time_range_none():
    """from_dttm/to_dttm are still computed from TEMPORAL_RANGE filter val.

    Even though self.time_range stays None, the effective time range (extracted
    from the filter) must be used to resolve from_dttm/to_dttm.  This mirrors
    original factory behavior: processed_time_range drives dttm resolution
    while time_range=None is stored on QueryObject.
    """
    qo = AsyncQueryObject(
        datasource={"type": "table", "id": 1},
        time_range=None,
        filters=[
            {"col": "ds", "op": "TEMPORAL_RANGE", "val": "2020-01-01 : 2020-12-31"}
        ],
    )
    # Dttm must be resolved from the filter val even though time_range is None.
    assert qo.from_dttm is not None
    assert qo.to_dttm is not None
    assert qo.time_range is None


# ---------------------------------------------------------------------------
# 1:1 parity: _capped_row_limit honors server_pagination ceiling
# ---------------------------------------------------------------------------


def test_capped_row_limit_server_pagination_uses_higher_ceiling():
    from superset.common.query_object import _capped_row_limit

    with patch("superset.utils.core.apply_max_row_limit") as mock_cap:
        mock_cap.return_value = 999
        _capped_row_limit(500, None, server_pagination=True)
        # server_pagination must be threaded through to apply_max_row_limit.
        _, kwargs = mock_cap.call_args
        assert kwargs.get("server_pagination") is True


def test_from_request_threads_server_pagination():
    with patch("superset.common.query_object._capped_row_limit") as mock_cap:
        mock_cap.return_value = 100
        AsyncQueryObject.from_request(
            {"row_limit": 10, "server_pagination": True},
            {"type": "table", "id": 1},
        )
        _, kwargs = mock_cap.call_args
        assert kwargs.get("server_pagination") is True
