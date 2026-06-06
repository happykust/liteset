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

from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from superset.common.query_context_processor import AsyncQueryContextProcessor
from superset.common.query_object import AsyncQueryObject


@pytest.fixture
def mock_settings():
    settings = MagicMock()
    settings.row_limit = 50000
    settings.cache_default_timeout = 300
    settings.csv_export = {}
    settings.excel_export = {}
    settings.data_cache_config = {}
    settings.samples_row_limit = 1000
    # Feature flags are a real (empty) dict — off by default, exactly as in
    # production. Leaving this as an auto-MagicMock would make
    # ``flags.get("CACHE_IMPERSONATION")`` truthy and spuriously enter the
    # per-user impersonation cache-key branch.
    settings.feature_flags = {}
    return settings


@pytest.fixture
def mock_security_manager():
    sm = AsyncMock()
    sm.get_rls_cache_key = AsyncMock(return_value="")
    sm.raise_for_access = AsyncMock()
    return sm


@pytest.fixture
def mock_datasource():
    ds = MagicMock()
    ds.uid = "table__1"
    ds.id = 1
    ds.type = "table"
    ds.changed_on = None
    ds.cache_timeout = None
    ds.column_names = ["col1", "col2"]
    ds.get_extra_cache_keys = MagicMock(return_value=[])
    # Prevent MagicMock auto-creating database.cache_timeout
    ds.database = MagicMock()
    ds.database.cache_timeout = None
    return ds


@pytest.fixture
def mock_user():
    user = MagicMock()
    user.username = "test_user"
    return user


@pytest.fixture
def processor(mock_settings, mock_security_manager, mock_datasource, mock_user):
    return AsyncQueryContextProcessor(
        datasource=mock_datasource,
        settings=mock_settings,
        security_manager=mock_security_manager,
        user=mock_user,
    )


async def test_processor_creation(processor):
    assert processor is not None
    assert processor._datasource is not None


async def test_processor_no_flask_dependency(processor):
    """Verify no Flask imports leaked into the processor."""
    assert not hasattr(processor, "_flask_app")


async def test_raise_for_access_delegates(processor, mock_security_manager, mock_user):
    await processor.raise_for_access()
    mock_security_manager.raise_for_access.assert_awaited_once_with(
        query_context=processor._query_context,
        user=mock_user,
    )


async def test_raise_for_access_passes_user_and_query_context(
    mock_settings, mock_security_manager, mock_datasource
):
    """raise_for_access passes both user and query_context to the security manager."""
    user = MagicMock()
    user.username = "admin"
    proc = AsyncQueryContextProcessor(
        datasource=mock_datasource,
        settings=mock_settings,
        security_manager=mock_security_manager,
        user=user,
    )
    await proc.raise_for_access()
    call_kwargs = mock_security_manager.raise_for_access.call_args.kwargs
    assert call_kwargs["user"] is user
    assert "query_context" in call_kwargs


async def test_get_cache_key(processor):
    qo = AsyncQueryObject(datasource={"type": "table", "id": 1})
    key = await processor._get_cache_key(qo)
    assert key is not None
    assert isinstance(key, str)
    assert len(key) > 0


async def test_get_cache_timeout_default(processor):
    timeout = processor._get_cache_timeout()
    assert timeout == 300


async def test_get_cache_timeout_from_data_config():
    settings = MagicMock()
    settings.data_cache_config = {"CACHE_DEFAULT_TIMEOUT": 600}
    settings.cache_default_timeout = 300
    ds = MagicMock()
    ds.cache_timeout = None
    ds.database = MagicMock()
    ds.database.cache_timeout = None
    proc = AsyncQueryContextProcessor(
        datasource=ds,
        settings=settings,
        security_manager=AsyncMock(),
    )
    assert proc._get_cache_timeout() == 600


async def test_get_annotation_data_empty(processor):
    qo = AsyncQueryObject(
        datasource={"type": "table", "id": 1},
        annotation_layers=[],
    )
    result = await processor.get_annotation_data(qo)
    assert result == {}


async def test_get_native_annotation_data_no_dao(processor):
    qo = AsyncQueryObject(
        datasource={"type": "table", "id": 1},
        annotation_layers=[{"sourceType": "NATIVE", "value": 1, "name": "test"}],
    )
    # No annotation DAO set, should return empty
    result = await processor.get_native_annotation_data(qo)
    assert result == {}


async def test_get_time_grain_from_extras():
    qo = AsyncQueryObject(
        datasource={"type": "table", "id": 1},
        extras={"time_grain_sqla": "P1D"},
    )
    assert AsyncQueryContextProcessor.get_time_grain(qo) == "P1D"


async def test_get_time_grain_from_columns():
    qo = AsyncQueryObject(
        datasource={"type": "table", "id": 1},
        columns=[{"timeGrain": "P1W", "sqlExpression": "ds"}],
    )
    assert AsyncQueryContextProcessor.get_time_grain(qo) == "P1W"


async def test_processing_time_offsets_placeholder(processor):
    qo = AsyncQueryObject(datasource={"type": "table", "id": 1})
    df = pd.DataFrame({"col": [1, 2, 3]})
    result = await processor.processing_time_offsets(df, qo)
    assert "df" in result
    assert "queries" in result
    assert "cache_keys" in result


async def test_get_payload_multiple_query_objects(
    mock_settings, mock_security_manager, mock_datasource
):
    """get_payload processes multiple query objects and returns results for each."""
    proc = AsyncQueryContextProcessor(
        datasource=mock_datasource,
        settings=mock_settings,
        security_manager=mock_security_manager,
    )
    qo1 = AsyncQueryObject(datasource={"type": "table", "id": 1}, columns=["col1"])
    qo2 = AsyncQueryObject(datasource={"type": "table", "id": 1}, columns=["col2"])

    mock_result = {"df": pd.DataFrame({"x": [1, 2]}), "query": "SELECT x"}
    with patch.object(
        proc, "_get_query_result", new_callable=AsyncMock, return_value=mock_result
    ):
        payload = await proc.get_payload([qo1, qo2])

    assert "queries" in payload
    assert len(payload["queries"]) == 2
    assert payload["queries"][0]["status"] == "success"
    assert payload["queries"][1]["status"] == "success"
    assert payload["queries"][0]["rowcount"] == 2


async def test_cache_timeout_from_data_cache_config_override():
    """data_cache_config CACHE_DEFAULT_TIMEOUT takes precedence over
    cache_default_timeout.
    """
    settings = MagicMock()
    settings.data_cache_config = {"CACHE_DEFAULT_TIMEOUT": 999}
    settings.cache_default_timeout = 300
    ds = MagicMock()
    ds.cache_timeout = None
    ds.database = MagicMock()
    ds.database.cache_timeout = None
    proc = AsyncQueryContextProcessor(
        datasource=ds,
        settings=settings,
        security_manager=AsyncMock(),
    )
    assert proc._get_cache_timeout() == 999


async def test_raise_for_access_passes_query_context(
    mock_settings, mock_security_manager, mock_datasource
):
    """raise_for_access delegates with user and query_context."""
    mock_user = MagicMock()
    proc = AsyncQueryContextProcessor(
        datasource=mock_datasource,
        settings=mock_settings,
        security_manager=mock_security_manager,
        user=mock_user,
    )
    await proc.raise_for_access()
    call_kwargs = mock_security_manager.raise_for_access.call_args.kwargs
    assert call_kwargs["user"] is mock_user
    assert "query_context" in call_kwargs


async def test_get_data_json_format():
    """get_data with json format returns list of dicts."""
    df = pd.DataFrame({"a": [1, 2], "b": ["x", "y"]})
    result = AsyncQueryContextProcessor.get_data(df, result_format="json")
    assert isinstance(result, list)
    assert len(result) == 2
    assert result[0] == {"a": 1, "b": "x"}


async def test_get_data_csv_format():
    """get_data with csv format returns a CSV string."""
    df = pd.DataFrame({"a": [10, 20], "b": ["hello", "world"]})
    result = AsyncQueryContextProcessor.get_data(df, result_format="csv")
    assert isinstance(result, str)
    assert "a,b" in result
    assert "10,hello" in result
    assert "20,world" in result


async def test_exec_post_processing_empty_list():
    """_exec_post_processing with no operations returns the DataFrame unchanged."""
    df = pd.DataFrame({"col": [1, 2, 3]})
    qo = AsyncQueryObject(datasource={"type": "table", "id": 1}, post_processing=[])
    result = AsyncQueryContextProcessor._exec_post_processing(df, qo)
    pd.testing.assert_frame_equal(result, df)


async def test_processor_creation_all_optional_params():
    """Processor accepts all optional constructor parameters."""
    mock_cache = MagicMock()
    mock_ann_dao = MagicMock()
    mock_chart_dao = MagicMock()
    mock_qc = MagicMock()
    settings = MagicMock()
    settings.data_cache_config = {}
    settings.cache_default_timeout = 300
    proc = AsyncQueryContextProcessor(
        datasource=MagicMock(),
        settings=settings,
        security_manager=AsyncMock(),
        cache_manager=mock_cache,
        annotation_dao=mock_ann_dao,
        chart_dao=mock_chart_dao,
        query_context=mock_qc,
    )
    assert proc._cache_manager is mock_cache
    assert proc._annotation_dao is mock_ann_dao
    assert proc._chart_dao is mock_chart_dao
    assert proc._query_context is mock_qc


async def test_generate_context_cache_key_returns_string(processor):
    """_generate_context_cache_key returns a string starting with qc-."""
    key = processor._generate_context_cache_key()
    assert isinstance(key, str)
    assert key.startswith("qc-")
    assert len(key) > 3


async def test_force_cached_returns_error_when_not_cached(processor):
    """force_cached=True returns error when no cached data available."""
    qo = AsyncQueryObject(datasource={"type": "table", "id": 1})
    result = await processor.get_df_payload(qo, force_cached=True)
    assert result["status"] == "failed"
    assert "not available" in result["error"]


async def test_get_query_result_with_mock_datasource(
    mock_settings, mock_security_manager
):
    """_get_query_result calls datasource.query() and returns dict."""
    ds = MagicMock(spec=["uid", "query", "get_extra_cache_keys", "changed_on"])
    ds.uid = "table__1"
    ds.query = MagicMock(
        return_value=MagicMock(df=pd.DataFrame({"x": [1]}), query="SELECT x")
    )
    ds.get_extra_cache_keys = MagicMock(return_value=[])
    ds.changed_on = None

    proc = AsyncQueryContextProcessor(
        datasource=ds, settings=mock_settings, security_manager=mock_security_manager
    )
    qo = AsyncQueryObject(datasource={"type": "table", "id": 1})
    result = await proc._get_query_result(qo)
    assert "df" in result
    assert len(result["df"]) == 1


async def test_cache_hit_returns_cached_data(mock_settings, mock_security_manager):
    """When cache manager returns data, is_cached=True."""
    ds = MagicMock()
    ds.uid = "table__1"
    ds.get_extra_cache_keys = MagicMock(return_value=[])
    ds.changed_on = None

    cache = AsyncMock()
    cached_df = pd.DataFrame({"cached": [1, 2]})
    cache.get = AsyncMock(return_value=cached_df)

    proc = AsyncQueryContextProcessor(
        datasource=ds,
        settings=mock_settings,
        security_manager=mock_security_manager,
        cache_manager=cache,
    )
    qo = AsyncQueryObject(datasource={"type": "table", "id": 1})
    result = await proc.get_df_payload(qo)
    assert result["is_cached"] is True
    assert len(result["df"]) == 2


async def test_get_viz_annotation_data_handles_missing_method(processor):
    """If chart model has no get_query_context(), raise clear ValueError."""
    mock_chart = MagicMock(spec=[])  # spec=[] means no attributes
    mock_chart.id = 42
    processor._chart_dao = AsyncMock()
    processor._chart_dao.find_by_id = AsyncMock(return_value=mock_chart)

    with pytest.raises(ValueError, match="does not support get_query_context"):
        await processor.get_viz_annotation_data(
            {"name": "test_layer", "value": 42},
            force=False,
        )


async def test_get_data_xlsx_format():
    """get_data with xlsx format returns bytes."""
    df = pd.DataFrame({"col1": [1, 2], "col2": ["a", "b"]})
    result = AsyncQueryContextProcessor.get_data(df, result_format="xlsx")
    assert isinstance(result, bytes)
    assert len(result) > 0


async def test_viz_annotation_recursion_depth(mock_settings, mock_security_manager):
    """get_viz_annotation_data raises at max depth."""
    ds = MagicMock()
    ds.uid = "table__1"
    proc = AsyncQueryContextProcessor(
        datasource=ds,
        settings=mock_settings,
        security_manager=mock_security_manager,
        chart_dao=AsyncMock(),
    )
    with pytest.raises(ValueError, match="recursion depth"):
        await proc.get_viz_annotation_data(
            {"value": 1, "name": "test"}, force=False, _depth=2
        )


async def test_ensure_totals_injects_contribution_totals(processor):
    """_ensure_totals_available injects the computed totals DICT into
    contribution post-processing.

    1:1 port of upstream ``ensure_totals_available``: it locates a *separate*
    totals query (no columns, has metrics, no post-processing), executes it,
    and injects the column-sum dict (never ``True``) into each contribution
    op's options. A query that itself carries post-processing can never be the
    totals query, so a dedicated totals query object is required.
    """
    qo = AsyncQueryObject(
        datasource={"type": "table", "id": 1},
        columns=["col1"],
        metrics=["m"],
        post_processing=[
            {"operation": "contribution", "options": {}},
            {"operation": "pivot"},
        ],
    )
    # Separate totals query: no columns, has metrics, no post-processing.
    totals_qo = AsyncQueryObject(
        datasource={"type": "table", "id": 1},
        metrics=["m"],
    )
    totals_result = {
        "df": pd.DataFrame({"m": [3, 7]}),
        "status": "success",
    }
    with patch.object(
        processor,
        "_get_query_result",
        new_callable=AsyncMock,
        return_value=totals_result,
    ):
        await processor._ensure_totals_available([qo, totals_qo])
    # The contribution operation should have the totals DICT injected
    # (sum of the totals column: 3 + 7 == 10).
    assert qo.post_processing[0]["options"]["contribution_totals"] == {"m": 10}
    # The pivot operation should be untouched
    assert "options" not in qo.post_processing[
        1
    ] or "contribution_totals" not in qo.post_processing[1].get("options", {})


async def test_invalid_column_raises_validation_error(
    mock_settings, mock_security_manager
):
    """get_df_payload raises QueryObjectValidationError for columns not in
    datasource.
    """
    ds = MagicMock()
    ds.uid = "table__1"
    ds.type = "table"
    ds.column_names = ["col1", "col2"]
    ds.changed_on = None
    ds.get_extra_cache_keys = MagicMock(return_value=[])

    proc = AsyncQueryContextProcessor(
        datasource=ds,
        settings=mock_settings,
        security_manager=mock_security_manager,
    )
    qo = AsyncQueryObject(
        datasource={"type": "table", "id": 1},
        columns=["col1", "nonexistent_col"],
    )

    with patch(
        "superset.common.query_context_processor.AsyncQueryContextProcessor.get_df_payload",
        wraps=proc.get_df_payload,
    ):
        # We need the superset imports to be available for this test
        try:
            from superset.exceptions import QueryObjectValidationError
            from superset.utils.core import (
                get_column_names_from_columns,  # noqa: F401  # availability probe
            )
        except ImportError:
            pytest.skip("superset.utils.core not available")

        with pytest.raises(QueryObjectValidationError, match="nonexistent_col"):
            await proc.get_df_payload(qo)


def test_query_object_to_dict_uses_filter_key():
    """to_dict() must produce 'filter' key, not 'filters'."""
    qo = AsyncQueryObject(
        datasource={"type": "table", "id": 1},
        filters=[{"col": "x", "op": "==", "val": 1}],
    )
    d = qo.to_dict()
    assert "filter" in d
    assert "filters" not in d
    assert d["filter"] == [{"col": "x", "op": "==", "val": 1}]


def test_validate_sanitizes_where_clause():
    """validate() delegates extras.where to sanitize_clause."""
    from superset.common.query_object import QueryObjectValidationError

    qo = AsyncQueryObject(
        datasource={"type": "table", "id": 1},
        extras={"where": "1=1"},
    )

    def fake_sanitize(clause: str) -> str:
        raise Exception("Unsafe clause detected")

    with patch("superset.common.query_object.sanitize_clause", fake_sanitize):
        with pytest.raises(QueryObjectValidationError, match="Unsafe SQL"):
            qo.validate()


def test_validate_rejects_duplicate_labels():
    """validate() raises on duplicate column/metric labels."""
    from superset.common.query_object import QueryObjectValidationError

    qo = AsyncQueryObject(
        datasource={"type": "table", "id": 1},
        columns=["revenue", "revenue"],
    )
    with pytest.raises(QueryObjectValidationError, match="Duplicate label.*revenue"):
        qo.validate()


# ---------------------------------------------------------------------------
# NEW-T9: Cache exception paths in QueryContextProcessor
# ---------------------------------------------------------------------------


async def test_cache_get_exception_returns_none(mock_settings, mock_security_manager):
    """_cache_get returns None when cache.get raises."""
    ds = MagicMock()
    ds.uid = "table__1"

    cache = MagicMock()
    cache.get = MagicMock(side_effect=RuntimeError("redis down"))

    proc = AsyncQueryContextProcessor(
        datasource=ds,
        settings=mock_settings,
        security_manager=mock_security_manager,
        cache_manager=cache,
    )
    result = await proc._cache_get("some-key")
    assert result is None


async def test_cache_set_exception_does_not_propagate(
    mock_settings, mock_security_manager
):
    """_cache_set silently catches exceptions from cache.set."""
    ds = MagicMock()
    ds.uid = "table__1"

    cache = MagicMock()
    cache.set = MagicMock(side_effect=RuntimeError("redis down"))

    proc = AsyncQueryContextProcessor(
        datasource=ds,
        settings=mock_settings,
        security_manager=mock_security_manager,
        cache_manager=cache,
    )
    # Should not raise
    await proc._cache_set("some-key", {"data": [1]}, 300)


async def test_get_df_payload_propagates_failed_query(
    mock_settings, mock_security_manager, mock_datasource
):
    """A failed query result (status=error / error_message) surfaces as a
    failed payload instead of a silent empty success — so the chart-data
    command can turn it into a 400.
    """
    proc = AsyncQueryContextProcessor(
        datasource=mock_datasource,
        settings=mock_settings,
        security_manager=mock_security_manager,
    )
    qo = AsyncQueryObject(datasource={"type": "table", "id": 1}, columns=["col1"])
    err_result = {
        "df": pd.DataFrame(),
        "query": "",
        "status": "error",
        "error_message": "boom",
    }
    with patch.object(
        proc, "_get_query_result", new_callable=AsyncMock, return_value=err_result
    ):
        payload = await proc.get_payload([qo])

    q = payload["queries"][0]
    assert q["status"] == "failed"
    assert "boom" in (q["error"] or "")


async def test_ensure_totals_propagates_failed_query(
    mock_settings, mock_security_manager, mock_datasource
):
    """A failed contribution-totals query propagates (1:1 with the original
    ``ensure_totals_available``, which has no try/except) rather than being
    swallowed into empty totals (→ wrong contribution %).
    """
    from superset.exceptions import QueryObjectValidationError

    proc = AsyncQueryContextProcessor(
        datasource=mock_datasource,
        settings=mock_settings,
        security_manager=mock_security_manager,
    )
    contrib_qo = AsyncQueryObject(
        datasource={"type": "table", "id": 1},
        columns=["col1"],
        metrics=["m"],
        post_processing=[{"operation": "contribution", "options": {}}],
    )
    # totals query: no columns, has metrics, no post-processing
    totals_qo = AsyncQueryObject(
        datasource={"type": "table", "id": 1},
        metrics=["m"],
    )
    err_result = {
        "df": pd.DataFrame(),
        "status": "error",
        "error_message": "totals boom",
    }
    with patch.object(
        proc, "_get_query_result", new_callable=AsyncMock, return_value=err_result
    ):
        with pytest.raises(QueryObjectValidationError, match="totals boom"):
            await proc._ensure_totals_available([contrib_qo, totals_qo])


# --- per-offset cache (time comparison) ---------------------------------------


async def test_get_cache_key_offset_distinct(processor):
    """Different time offsets must yield DISTINCT cache keys (no collision →
    no stale time-comparison data); the same offset is stable; and any offset
    differs from the offset-less base key."""
    qo = AsyncQueryObject(datasource={"type": "table", "id": 1})
    base = await processor._get_cache_key(qo)
    k_1y = await processor._get_cache_key(qo, time_offset="1 year ago|P1D")
    k_1w = await processor._get_cache_key(qo, time_offset="1 week ago|P1D")
    k_1y_again = await processor._get_cache_key(qo, time_offset="1 year ago|P1D")

    assert k_1y != k_1w  # distinct offsets → distinct keys
    assert k_1y != base  # offset key differs from base
    assert k_1y == k_1y_again  # same offset → stable key


async def test_offset_cache_roundtrip(mock_settings, mock_security_manager,
                                      mock_datasource, mock_user):
    """The per-offset payload ({df, query}) survives the pickle store/retrieve
    via the cache manager."""

    class _DictCache:
        def __init__(self):
            self.store = {}

        async def get(self, key):
            return self.store.get(key)

        async def set(self, key, value, timeout):
            self.store[key] = value

    proc = AsyncQueryContextProcessor(
        datasource=mock_datasource,
        settings=mock_settings,
        security_manager=mock_security_manager,
        user=mock_user,
        cache_manager=_DictCache(),
    )
    df = pd.DataFrame({"col1": [1, 2], "SUM(x)__1 year ago": [10, 20]})
    payload = {"df": df, "query": "SELECT ... -- 1 year ago"}
    await proc._cache_set("off-key", payload, 300)
    out = await proc._cache_get("off-key")
    assert isinstance(out, dict) and "df" in out
    assert out["query"] == payload["query"]
    pd.testing.assert_frame_equal(out["df"], df)


# ---------------------------------------------------------------------------
# 1:1 parity: get_data CSV/XLSX (verbose_map rename + coltypes + config)
# ---------------------------------------------------------------------------


async def test_get_data_csv_applies_verbose_map():
    """CSV export renames columns via the verbose_map (1:1 with upstream)."""
    df = pd.DataFrame({"col_a": [1], "col_b": ["x"]})
    out = AsyncQueryContextProcessor.get_data(
        df, result_format="csv", verbose_map={"col_a": "Column A"}
    )
    assert isinstance(out, str)
    assert "Column A" in out
    assert "col_b" in out  # unmapped column keeps its name


async def test_get_data_xlsx_applies_column_types_and_verbose_map():
    """XLSX export applies coltypes + verbose_map and returns bytes."""
    df = pd.DataFrame({"n": ["1", "2"], "s": ["a", "b"]})
    out = AsyncQueryContextProcessor.get_data(
        df,
        result_format="xlsx",
        coltypes=[0, 1],  # NUMERIC, STRING
        verbose_map={"n": "Number"},
    )
    assert isinstance(out, bytes)
    assert len(out) > 0


# ---------------------------------------------------------------------------
# 1:1 parity: result_type=query carries `language` + graceful error-in-payload
# ---------------------------------------------------------------------------


async def test_get_query_only_includes_language(
    mock_settings, mock_security_manager
):
    """``result_type=query`` payload carries the datasource dialect language."""
    ds = MagicMock()
    ds.query_language = "sql"
    ds._build_sql = MagicMock(return_value=("SELECT 1", None, None))
    proc = AsyncQueryContextProcessor(
        datasource=ds,
        settings=mock_settings,
        security_manager=mock_security_manager,
    )
    qo = AsyncQueryObject(datasource={"type": "table", "id": 1})
    result = await proc._get_query_only(qo)
    assert result["language"] == "sql"
    assert result["query"] == "SELECT 1"
    assert result["error"] is None


async def test_get_query_only_validation_error_in_payload(
    mock_settings, mock_security_manager
):
    """A QueryObjectValidationError during build surfaces in ``error``, not raised."""
    from superset.exceptions import QueryObjectValidationError

    ds = MagicMock()
    ds.query_language = "sql"
    ds._build_sql = MagicMock(
        side_effect=QueryObjectValidationError("Empty query?")
    )
    proc = AsyncQueryContextProcessor(
        datasource=ds,
        settings=mock_settings,
        security_manager=mock_security_manager,
    )
    qo = AsyncQueryObject(datasource={"type": "table", "id": 1})
    result = await proc._get_query_only(qo)
    assert result["error"] == "Empty query?"
    assert "query" not in result or result.get("query") is None
    assert result["language"] == "sql"
