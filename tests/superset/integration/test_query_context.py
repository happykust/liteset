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
"""Flask-free port of ``tests/integration_tests/query_context_tests.py``.

Exercises the async ``QueryContext`` pipeline (``AsyncQueryObject`` ->
``AsyncQueryContext`` -> ``AsyncQueryContextProcessor``) against the REAL
seeded Postgres backend via the ``db_session`` fixture.  The upstream test
drives ``ChartDataQueryContextSchema().load(payload)`` then
``query_context.get_payload()`` / ``query_cache_key()`` /
``get_df_payload()`` / ``get_query_result()`` / ``processing_time_offsets()``;
the Liteset port splits that surface across
``AsyncQueryObject.from_request`` + ``AsyncQueryContext`` +
``AsyncQueryContextProcessor`` (cache keys via ``processor._get_cache_key`` /
``_generate_context_cache_key``, result-type dispatch via
``processor.get_payload``).  The ``_load_query_context`` /
``_get_payload`` helpers below reproduce the schema-load + ``get_payload``
behaviour 1:1, including the upstream serialization of ``df`` ->
``data`` / ``colnames`` and the offset-SQL concatenation into ``query``.
"""

from __future__ import annotations

import copy
import re
import time
from typing import Any

import pandas as pd
import pytest
from pandas import DateOffset
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from superset.common.query_context import AsyncQueryContext
from superset.common.query_context_processor import AsyncQueryContextProcessor
from superset.common.query_object import AsyncQueryObject
from superset.common.query_status import QueryStatus
from superset.config import SupersetSettings
from superset.models.connectors import SqlaTable, SqlMetric
from superset.models.helpers import AdhocMetricExpressionType
from superset.security.manager import build_async_security_manager
from superset.typing import AdhocColumn
from superset.utils.feature_flags import feature_flag_manager
from superset.utils.pandas_postprocessing.utils import FLAT_COLUMN_SEPARATOR

pytestmark = pytest.mark.asyncio

# The adhoc-temporal probe bug (#11) is fixed (see test_date_adhoc_column). A
# separate, deeper downstream issue remains ONLY for date-range timeshifts: the
# offset DataFrame's temporal join column is normalized to float64 while the
# main frame is datetime64, so processing_time_offsets' merge raises. Distinct
# from the probe; tracked in docs/audit/port_bugs_from_test_porting.md.
_XFAIL_DATE_RANGE_OFFSET_DTYPE = pytest.mark.xfail(
    strict=True,
    reason="port bug: date-range time-offset DataFrame normalizes its temporal "
    "join column to float64 (main frame is datetime64), so the offset merge in "
    "processing_time_offsets fails. Separate from the now-fixed adhoc-dttm probe.",
)


# ---------------------------------------------------------------------------
# Query-context payload generator (inlined 1:1 from
# ``tests/common/query_context_generator.py`` so the test does not import the
# Flask-coupled ``tests.common`` / ``tests.integration_tests`` packages).  The
# only behavioural difference is ``datasource.id``: the upstream generator hard
# codes ``id=1`` and resolves the table by name at load time; here the resolved
# seeded-dataset id is threaded in by ``get_query_context``.
# ---------------------------------------------------------------------------

query_birth_names: dict[str, Any] = {
    "extras": {"where": "", "time_grain_sqla": "P1D"},
    "columns": ["name"],
    "metrics": [{"label": "sum__num"}],
    "orderby": [("sum__num", False)],
    "row_limit": 100,
    "granularity": "ds",
    "time_range": "100 years ago : now",
    "timeseries_limit": 0,
    "timeseries_limit_metric": None,
    "order_desc": True,
    "filters": [
        {"col": "gender", "op": "==", "val": "boy"},
        {"col": "num", "op": "IS NOT NULL"},
        {"col": "name", "op": "NOT IN", "val": ["<NULL>", '"abc"']},
    ],
    "having": "",
    "where": "",
}

QUERY_OBJECTS: dict[str, dict[str, object]] = {
    "birth_names": query_birth_names,
    "birth_names:only_orderby_has_metric": {
        "metrics": [],
    },
    "birth_names:orderby_dup_alias": {
        "metrics": [
            {
                "expressionType": "SIMPLE",
                "column": {"column_name": "num_girls", "type": "BIGINT(20)"},
                "aggregate": "SUM",
                "label": "num_girls",
            },
            {
                "expressionType": "SIMPLE",
                "column": {"column_name": "num_boys", "type": "BIGINT(20)"},
                "aggregate": "SUM",
                "label": "num_boys",
            },
        ],
        "orderby": [
            [
                {
                    "expressionType": "SIMPLE",
                    "column": {"column_name": "num_girls", "type": "BIGINT(20)"},
                    "aggregate": "SUM",
                    # the same underlying expression, but different label
                    "label": "SUM(num_girls)",
                },
                False,
            ],
            # reference the ambiguous alias in SIMPLE metric
            [
                {
                    "expressionType": "SIMPLE",
                    "column": {"column_name": "num_boys", "type": "BIGINT(20)"},
                    "aggregate": "AVG",
                    "label": "AVG(num_boys)",
                },
                False,
            ],
            # reference the ambiguous alias in CUSTOM SQL metric
            [
                {
                    "expressionType": "SQL",
                    "sqlExpression": "MAX(CASE WHEN num_boys > 0 THEN 1 ELSE 0 END)",
                    "label": "MAX(CASE WHEN...",
                },
                True,
            ],
        ],
    },
}

POSTPROCESSING_OPERATIONS: dict[str, list[dict[str, Any]]] = {
    "birth_names": [
        {
            "operation": "aggregate",
            "options": {
                "groupby": ["name"],
                "aggregates": {
                    "q1": {
                        "operator": "percentile",
                        "column": "sum__num",
                        "options": {"q": 25, "interpolation": "lower"},
                    },
                    "median": {
                        "operator": "median",
                        "column": "sum__num",
                    },
                },
            },
        },
        {
            "operation": "sort",
            "options": {"by": ["q1", "name"], "ascending": [False, True]},
        },
    ]
}


def _get_query_object(
    query_name: str,
    add_postprocessing_operations: bool,
    add_time_offsets: bool,
) -> dict[str, Any]:
    if query_name not in QUERY_OBJECTS:
        raise Exception(f"QueryObject fixture not defined for datasource: {query_name}")
    obj = QUERY_OBJECTS[query_name]

    # apply overrides
    if ":" in query_name:
        parent_query_name = query_name.split(":")[0]
        obj = {**QUERY_OBJECTS[parent_query_name], **obj}

    query_object = copy.deepcopy(obj)
    if add_postprocessing_operations:
        query_object["post_processing"] = copy.deepcopy(
            POSTPROCESSING_OPERATIONS[query_name.split(":")[0]]
        )
    if add_time_offsets:
        query_object["time_offsets"] = ["1 year ago"]
    return query_object


def get_query_context(
    query_name: str,
    datasource_id: int,
    add_postprocessing_operations: bool = False,
    add_time_offsets: bool = False,
    form_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a chart/data request payload for a seeded example dataset.

    1:1 with ``tests.integration_tests.fixtures.query_context.get_query_context``
    except the resolved seeded-dataset id is passed in explicitly (the upstream
    helper resolved it from the table name via ``SupersetTestCase.get_table``).
    """
    return {
        "datasource": {"id": datasource_id, "type": "table"},
        "queries": [
            _get_query_object(
                query_name,
                add_postprocessing_operations,
                add_time_offsets,
            )
        ],
        "result_type": "full",
        "form_data": form_data or {},
    }


# ---------------------------------------------------------------------------
# Real-backend helpers — replace ``ChartDataQueryContextSchema().load(payload)``
# and ``query_context.get_payload()`` with the equivalent Liteset object graph.
# ---------------------------------------------------------------------------


async def _load_datasource(session: AsyncSession, table_name: str) -> SqlaTable:
    """Load a seeded ``SqlaTable`` with database/columns/metrics eager-loaded.

    Eager loading is required because the sync SQL-build pipeline runs in a
    worker thread (``asyncio.to_thread``) and would otherwise trigger a lazy
    relationship load off the async session -> ``MissingGreenlet`` / sync-IO
    error.
    """
    return (
        await session.execute(
            select(SqlaTable)
            .where(SqlaTable.table_name == table_name)
            .options(
                selectinload(SqlaTable.columns),
                selectinload(SqlaTable.metrics),
                selectinload(SqlaTable.database),
            )
        )
    ).scalar_one()


def _make_processor(
    session: AsyncSession,
    datasource: SqlaTable,
    query_context: AsyncQueryContext,
    *,
    cache_manager: Any | None = None,
) -> AsyncQueryContextProcessor:
    settings = SupersetSettings()  # type: ignore[call-arg]
    security_manager = build_async_security_manager(session, settings)
    return AsyncQueryContextProcessor(
        datasource=datasource,
        settings=settings,
        security_manager=security_manager,
        user=None,
        cache_manager=cache_manager,
        query_context=query_context,
    )


async def _load_query_context(
    session: AsyncSession,
    payload: dict[str, Any],
    *,
    cache_manager: Any | None = None,
) -> tuple[AsyncQueryContext, AsyncQueryContextProcessor, SqlaTable]:
    """Liteset equivalent of ``ChartDataQueryContextSchema().load(payload)``.

    Returns the ``(query_context, processor, datasource)`` triple — the
    upstream ``query_context`` object's behaviour is split between the
    dataclass (state) and the processor (``get_payload`` / cache keys).
    """
    ds_ref = payload["datasource"]
    datasource = await _load_datasource_by_id(session, ds_ref["id"])
    ds_dict = {"id": ds_ref["id"], "type": ds_ref.get("type", "table")}
    queries = [
        AsyncQueryObject.from_request(q, ds_dict) for q in payload.get("queries", [])
    ]
    # Upstream's ``cache_query_context`` writes the full serialized query-context
    # *form* (which carries a nested ``form_data`` key + datasource/queries) so
    # the cached entry can be rehydrated by ``data_from_cache``. The port caches
    # ``query_context.form_data`` verbatim, so thread the whole payload through
    # as ``form_data`` here — that makes the cached form both contain
    # ``form_data`` and round-trip back through ``_load_query_context``. The
    # ``apply_granularity`` hook only reads the ``x_axis`` key off form_data, so
    # the extra datasource/queries keys are inert. ``force`` is excluded from the
    # cached form so a rehydrated context defaults to ``force=False`` (1:1 with
    # upstream, where the cached form never carried ``force``).
    cache_form = {k: v for k, v in payload.items() if k != "force"}
    query_context = AsyncQueryContext(
        datasource=datasource,
        queries=queries,
        force=payload.get("force", False),
        result_type=payload.get("result_type"),
        result_format=payload.get("result_format"),
        form_data=cache_form,
    )
    processor = _make_processor(
        session, datasource, query_context, cache_manager=cache_manager
    )
    return query_context, processor, datasource


async def _load_datasource_by_id(session: AsyncSession, ds_id: int) -> SqlaTable:
    return (
        await session.execute(
            select(SqlaTable)
            .where(SqlaTable.id == ds_id)
            .options(
                selectinload(SqlaTable.columns),
                selectinload(SqlaTable.metrics),
                selectinload(SqlaTable.database),
            )
        )
    ).scalar_one()


def _render_query_result(
    df_payload: dict[str, Any],
    *,
    result_format: str = "json",
) -> dict[str, Any]:
    """Serialize one ``get_df_payload`` dict into the upstream response shape.

    Mirrors the chart-data controller's ``_render_chart_data_payload`` (df ->
    ``data`` / ``colnames``). The time-offset subquery SQL is already
    concatenated into the ``query`` field by ``get_df_payload`` itself (1:1 with
    upstream ``QueryContextProcessor.get_df_payload``:
    ``query += ";\\n\\n".join(queries)``), so the ``query`` is passed through
    untouched here.
    """
    result = dict(df_payload)
    df: pd.DataFrame = result.get("df", pd.DataFrame())
    result["colnames"] = list(df.columns)
    result["data"] = AsyncQueryContextProcessor.get_data(df, result_format)
    result.pop("df", None)
    return result


async def _get_payload(
    query_context: AsyncQueryContext,
    processor: AsyncQueryContextProcessor,
    *,
    cache_query_context: bool = False,
) -> dict[str, Any]:
    """Liteset equivalent of upstream ``query_context.get_payload()``.

    Runs the processor's result-type dispatch per query, surfacing per-query
    validation errors into the payload (1:1 with upstream
    ``query_actions.get_query_results``, which catches
    ``QueryObjectValidationError`` per query and returns ``{"error": ...}``);
    the port's processor instead lets that error propagate out of
    ``get_payload`` for the command to turn into a 400 — see suspected-bug
    note). Each query's df is then serialized into the upstream
    ``{data, colnames, query, status, cache_key, ...}`` shape; the time-offset
    subquery SQL is already concatenated into ``query`` by ``get_df_payload``.
    """
    from superset.exceptions import SupersetException

    result_format = query_context.result_format or "json"
    rendered_queries: list[dict[str, Any]] = []
    cache_key: str | None = None
    for idx, qo in enumerate(query_context.queries):
        # Cache the query-context form on the FIRST query only (matches the
        # processor, which writes one ``qc-`` form per ``get_payload`` call).
        do_cache = cache_query_context and idx == 0
        try:
            raw = await processor.get_payload(
                [qo],
                force=query_context.force,
                cache_query_context=do_cache,
            )
        except SupersetException as ex:
            rendered_queries.append({"error": getattr(ex, "message", str(ex))})
            continue
        if do_cache and "cache_key" in raw:
            cache_key = raw["cache_key"]
        q = raw["queries"][0]
        # The "query" result-type branch already returns the serialized shape
        # (``language`` + ``query``); leave it untouched. Otherwise serialize
        # the df into data/colnames like the chart-data controller.
        if isinstance(q, dict) and isinstance(q.get("df"), pd.DataFrame):
            rendered_queries.append(
                _render_query_result(q, result_format=result_format)
            )
        else:
            rendered_queries.append(q)
    out: dict[str, Any] = {"queries": rendered_queries}
    if cache_key is not None:
        out["cache_key"] = cache_key
    return out


async def get_sql_text(
    session: AsyncSession, payload: dict[str, Any]
) -> str:
    """1:1 with the upstream module-level ``get_sql_text`` helper."""
    payload["result_type"] = "query"
    query_context, processor, _ = await _load_query_context(session, payload)
    responses = await _get_payload(query_context, processor)
    assert len(responses) == 1
    response = responses["queries"][0]
    assert len(response) == 2
    assert response["language"] == "sql"
    return response["query"]


# ---------------------------------------------------------------------------
# TestQueryContext
# ---------------------------------------------------------------------------


class TestQueryContext:
    @pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
    async def test_schema_deserialization(self, db_session: AsyncSession) -> None:
        """Ensure the deserialized QueryContext contains all required fields."""
        ds = await _load_datasource(db_session, "birth_names")
        payload = get_query_context(
            "birth_names", ds.id, add_postprocessing_operations=True
        )
        query_context, _processor, _ = await _load_query_context(db_session, payload)
        assert len(query_context.queries) == len(payload["queries"])

        for query_idx, query in enumerate(query_context.queries):
            payload_query = payload["queries"][query_idx]

            # check basic properties
            assert query.extras == payload_query["extras"]
            assert query.filters == payload_query["filters"]
            assert query.columns == payload_query["columns"]

            # metrics are mutated during creation
            for metric_idx, metric in enumerate(query.metrics):
                payload_metric = payload_query["metrics"][metric_idx]
                payload_metric = (
                    payload_metric
                    if "expressionType" in payload_metric
                    else payload_metric["label"]
                )
                assert metric == payload_metric

            assert query.orderby == payload_query["orderby"]
            assert query.time_range == payload_query["time_range"]

            # check post processing operation properties
            for post_proc_idx, post_proc in enumerate(query.post_processing):
                payload_post_proc = payload_query["post_processing"][post_proc_idx]
                assert post_proc["operation"] == payload_post_proc["operation"]
                assert post_proc["options"] == payload_post_proc["options"]

    @pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
    async def test_cache(self, db_session: AsyncSession) -> None:
        ds = await _load_datasource(db_session, "birth_names")
        payload = get_query_context(
            "birth_names", ds.id, add_postprocessing_operations=True
        )
        payload["force"] = True

        cache: dict[str, Any] = _DictCache()
        query_context, processor, _ = await _load_query_context(
            db_session, payload, cache_manager=cache
        )
        query_object = query_context.queries[0]
        query_cache_key = await processor._get_cache_key(query_object)  # noqa: SLF001

        response = await _get_payload(
            query_context, processor, cache_query_context=True
        )
        # MUST BE a successful query
        query_dump = response["queries"][0]
        assert query_dump["status"] == QueryStatus.SUCCESS

        cache_key = response["cache_key"]
        assert cache_key is not None

        cached = cache.get_obj(cache_key)
        assert cached is not None
        assert "form_data" in cached["data"]

        # Rehydrate the query context from the cached form (1:1 with upstream
        # ``ChartDataQueryContextSchema().load(cached["data"])``).
        rehydrated_qc, rehydrated_proc, _ = await _load_query_context(
            db_session, cached["data"], cache_manager=cache
        )
        rehydrated_qo = rehydrated_qc.queries[0]
        rehydrated_query_cache_key = await rehydrated_proc._get_cache_key(  # noqa: SLF001
            rehydrated_qo
        )

        assert rehydrated_qc.datasource.id == query_context.datasource.id
        assert len(rehydrated_qc.queries) == 1
        assert query_cache_key == rehydrated_query_cache_key
        assert rehydrated_qc.result_type == query_context.result_type
        assert rehydrated_qc.result_format == query_context.result_format
        assert not rehydrated_qc.force

    async def test_query_cache_key_changes_when_datasource_is_updated(
        self, db_session: AsyncSession
    ) -> None:
        ds = await _load_datasource(db_session, "birth_names")
        payload = get_query_context("birth_names", ds.id)

        # construct baseline query_cache_key
        query_context, processor, datasource = await _load_query_context(
            db_session, payload
        )
        query_object = query_context.queries[0]
        cache_key_original = await processor._get_cache_key(query_object)  # noqa: SLF001

        # make a temporary change and revert it to refresh changed_on
        description_original = datasource.description
        datasource.description = "temporary description"
        await db_session.flush()
        time.sleep(0.01)
        datasource.description = description_original
        await db_session.flush()
        time.sleep(0.01)
        await db_session.refresh(datasource)

        # new QueryContext with unchanged attributes -> new query_cache_key
        query_context, processor, _ = await _load_query_context(db_session, payload)
        query_object = query_context.queries[0]
        cache_key_new = await processor._get_cache_key(query_object)  # noqa: SLF001

        # the new cache_key should be different due to updated datasource
        assert cache_key_original != cache_key_new

    async def test_query_cache_key_changes_when_metric_is_updated(
        self, db_session: AsyncSession
    ) -> None:
        """Test that the query cache key changes when a metric is updated."""
        ds = await _load_datasource(db_session, "birth_names")
        payload = get_query_context("birth_names", ds.id)

        # Add a metric (the upstream test used ``DatasetDAO.update`` with a
        # metrics payload, which both inserts the SqlMetric row AND bumps the
        # dataset ``changed_on``). Mirror that here: append the metric and touch
        # the dataset so its ``changed_on`` (onupdate) advances on flush.
        new_metric = SqlMetric(metric_name="foo", expression="select 1;")
        ds.metrics.append(new_metric)
        ds.description = (ds.description or "") + " "
        await db_session.flush()
        await db_session.refresh(ds)

        # construct baseline query_cache_key
        query_context, processor, datasource = await _load_query_context(
            db_session, payload
        )
        query_object = query_context.queries[0]
        cache_key_original = await processor._get_cache_key(query_object)  # noqa: SLF001

        time.sleep(0.01)

        new_metric.expression = "select 2;"
        datasource.description = (datasource.description or "") + " "
        await db_session.flush()
        await db_session.refresh(datasource)

        # new QueryContext with unchanged attributes -> new query_cache_key
        query_context, processor, _ = await _load_query_context(db_session, payload)
        query_object = query_context.queries[0]
        cache_key_new = await processor._get_cache_key(query_object)  # noqa: SLF001

        # the new cache_key should be different due to updated datasource
        assert cache_key_original != cache_key_new

    async def test_query_cache_key_does_not_change_for_non_existent_or_null(
        self, db_session: AsyncSession
    ) -> None:
        ds = await _load_datasource(db_session, "birth_names")
        payload = get_query_context(
            "birth_names", ds.id, add_postprocessing_operations=True
        )
        del payload["queries"][0]["granularity"]

        query_context, processor, _ = await _load_query_context(db_session, payload)
        query_object = query_context.queries[0]
        cache_key_original = await processor._get_cache_key(query_object)  # noqa: SLF001

        payload["queries"][0]["granularity"] = None
        query_context, processor, _ = await _load_query_context(db_session, payload)
        query_object = query_context.queries[0]

        assert (
            await processor._get_cache_key(query_object)  # noqa: SLF001
            == cache_key_original
        )

    async def test_query_cache_key_changes_when_post_processing_is_updated(
        self, db_session: AsyncSession
    ) -> None:
        ds = await _load_datasource(db_session, "birth_names")
        payload = get_query_context(
            "birth_names", ds.id, add_postprocessing_operations=True
        )

        query_context, processor, _ = await _load_query_context(db_session, payload)
        query_object = query_context.queries[0]
        cache_key_original = await processor._get_cache_key(query_object)  # noqa: SLF001

        # ensure added None post_processing operation doesn't change cache key
        payload["queries"][0]["post_processing"].append(None)
        query_context, processor, _ = await _load_query_context(db_session, payload)
        query_object = query_context.queries[0]
        cache_key = await processor._get_cache_key(query_object)  # noqa: SLF001
        assert cache_key_original == cache_key

        # ensure query without post processing operation is different
        payload["queries"][0].pop("post_processing")
        query_context, processor, _ = await _load_query_context(db_session, payload)
        query_object = query_context.queries[0]
        cache_key = await processor._get_cache_key(query_object)  # noqa: SLF001
        assert cache_key_original != cache_key

    async def test_query_cache_key_changes_when_time_offsets_is_updated(
        self, db_session: AsyncSession
    ) -> None:
        ds = await _load_datasource(db_session, "birth_names")
        payload = get_query_context("birth_names", ds.id, add_time_offsets=True)

        query_context, processor, _ = await _load_query_context(db_session, payload)
        query_object = query_context.queries[0]
        cache_key_original = await processor._get_cache_key(query_object)  # noqa: SLF001

        payload["queries"][0]["time_offsets"].pop()
        query_context, processor, _ = await _load_query_context(db_session, payload)
        query_object = query_context.queries[0]
        cache_key = await processor._get_cache_key(query_object)  # noqa: SLF001
        assert cache_key_original != cache_key

    @pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
    async def test_query_cache_key_consistent_with_different_sql_formatting(
        self, db_session: AsyncSession
    ) -> None:
        """Cache keys are consistent regardless of SQL clause formatting."""
        ds = await _load_datasource(db_session, "birth_names")

        # Create payload with compact WHERE clause
        payload1 = get_query_context("birth_names", ds.id)
        payload1["queries"][0]["extras"] = {"where": "(name = 'Amy')"}
        qc1, proc1, _ = await _load_query_context(db_session, payload1)
        result1 = await proc1.get_df_payload(qc1.queries[0], force_cached=False)
        cache_key1 = result1.get("cache_key")

        # Same payload but with a pretty-formatted WHERE clause (with newlines)
        payload2 = get_query_context("birth_names", ds.id)
        payload2["queries"][0]["extras"] = {"where": "(\n  name = 'Amy'\n)"}
        qc2, proc2, _ = await _load_query_context(db_session, payload2)
        result2 = await proc2.get_df_payload(qc2.queries[0], force_cached=False)
        cache_key2 = result2.get("cache_key")

        # Cache keys should be identical after sanitization
        assert cache_key1 == cache_key2

        # Also verify with HAVING clause
        payload3 = get_query_context("birth_names", ds.id)
        payload3["queries"][0]["extras"] = {"having": "(sum__num > 100)"}
        qc3, proc3, _ = await _load_query_context(db_session, payload3)
        result3 = await proc3.get_df_payload(qc3.queries[0], force_cached=False)
        cache_key3 = result3.get("cache_key")

        payload4 = get_query_context("birth_names", ds.id)
        payload4["queries"][0]["extras"] = {"having": "(\n  sum__num > 100\n)"}
        qc4, proc4, _ = await _load_query_context(db_session, payload4)
        result4 = await proc4.get_df_payload(qc4.queries[0], force_cached=False)
        cache_key4 = result4.get("cache_key")

        # Cache keys should be identical after sanitization
        assert cache_key3 == cache_key4

    async def test_handle_metrics_field(self, db_session: AsyncSession) -> None:
        """Should support both predefined and adhoc metrics."""
        ds = await _load_datasource(db_session, "birth_names")
        adhoc_metric = {
            "expressionType": "SIMPLE",
            "column": {"column_name": "num_boys", "type": "BIGINT(20)"},
            "aggregate": "SUM",
            "label": "Boys",
            "optionName": "metric_11",
        }
        payload = get_query_context("birth_names", ds.id)
        payload["queries"][0]["metrics"] = ["sum__num", {"label": "abc"}, adhoc_metric]
        query_context, _processor, _ = await _load_query_context(db_session, payload)
        query_object = query_context.queries[0]
        assert query_object.metrics == ["sum__num", "abc", adhoc_metric]

    async def test_convert_deprecated_fields(self, db_session: AsyncSession) -> None:
        """Ensure deprecated fields are converted correctly."""
        ds = await _load_datasource(db_session, "birth_names")
        payload = get_query_context("birth_names", ds.id)
        columns = payload["queries"][0]["columns"]
        payload["queries"][0]["groupby"] = columns
        payload["queries"][0]["timeseries_limit"] = 99
        payload["queries"][0]["timeseries_limit_metric"] = "sum__num"
        del payload["queries"][0]["columns"]
        payload["queries"][0]["granularity_sqla"] = "timecol"
        payload["queries"][0]["having_filters"] = [{"col": "a", "op": "==", "val": "b"}]
        query_context, _processor, _ = await _load_query_context(db_session, payload)
        assert len(query_context.queries) == 1
        query_object = query_context.queries[0]
        assert query_object.granularity == "timecol"
        assert query_object.columns == columns
        assert query_object.series_limit == 99
        assert query_object.series_limit_metric == "sum__num"

    @pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
    async def test_csv_response_format(self, db_session: AsyncSession) -> None:
        """Ensure that CSV result format works."""
        ds = await _load_datasource(db_session, "birth_names")
        payload = get_query_context("birth_names", ds.id)
        payload["result_format"] = "csv"
        payload["queries"][0]["row_limit"] = 10
        query_context, processor, _ = await _load_query_context(db_session, payload)
        responses = await _get_payload(query_context, processor)
        assert len(responses) == 1
        data = responses["queries"][0]["data"]
        assert "name,sum__num\n" in data
        assert len(data.split("\n")) == 12

    async def test_sql_injection_via_groupby(self, db_session: AsyncSession) -> None:
        """Ensure that calling invalid column names in groupby are caught."""
        ds = await _load_datasource(db_session, "birth_names")
        payload = get_query_context("birth_names", ds.id)
        payload["queries"][0]["groupby"] = ["currentDatabase()"]
        query_context, processor, _ = await _load_query_context(db_session, payload)
        query_payload = await _get_payload(query_context, processor)
        assert query_payload["queries"][0].get("error") is not None

    async def test_sql_injection_via_columns(self, db_session: AsyncSession) -> None:
        """Ensure that calling invalid column names in columns are caught."""
        ds = await _load_datasource(db_session, "birth_names")
        payload = get_query_context("birth_names", ds.id)
        payload["queries"][0]["groupby"] = []
        payload["queries"][0]["metrics"] = []
        payload["queries"][0]["columns"] = ["*, 'extra'"]
        query_context, processor, _ = await _load_query_context(db_session, payload)
        query_payload = await _get_payload(query_context, processor)
        assert query_payload["queries"][0].get("error") is not None

    async def test_sql_injection_via_metrics(self, db_session: AsyncSession) -> None:
        """Ensure that calling invalid column names in filters are caught."""
        ds = await _load_datasource(db_session, "birth_names")
        payload = get_query_context("birth_names", ds.id)
        payload["queries"][0]["groupby"] = ["name"]
        payload["queries"][0]["metrics"] = [
            {
                "expressionType": AdhocMetricExpressionType.SIMPLE.value,
                "column": {"column_name": "invalid_col"},
                "aggregate": "SUM",
                "label": "My Simple Label",
            }
        ]
        query_context, processor, _ = await _load_query_context(db_session, payload)
        query_payload = await _get_payload(query_context, processor)
        assert query_payload["queries"][0].get("error") is not None

    @pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
    async def test_samples_response_type(self, db_session: AsyncSession) -> None:
        """Ensure that samples result type works."""
        ds = await _load_datasource(db_session, "birth_names")
        payload = get_query_context("birth_names", ds.id)
        payload["result_type"] = "samples"
        payload["queries"][0]["row_limit"] = 5
        query_context, processor, _ = await _load_query_context(db_session, payload)
        responses = await _get_payload(query_context, processor)
        assert len(responses) == 1
        data = responses["queries"][0]["data"]
        assert isinstance(data, list)
        assert len(data) == 5
        assert "sum__num" not in data[0]

    @pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
    async def test_query_response_type(self, db_session: AsyncSession) -> None:
        """Ensure that query result type works."""
        ds = await _load_datasource(db_session, "birth_names")
        payload = get_query_context("birth_names", ds.id)
        sql_text = await get_sql_text(db_session, payload)

        assert "SELECT" in sql_text
        assert re.search(r'[`"\[]?num[`"\]]? IS NOT NULL', sql_text)
        assert re.search(
            r"""NOT \([\s\n]*[`"\[]?name[`"\]]? IS NULL[\s\n]* """
            r"""OR [`"\[]?name[`"\]]? IN \('"abc"'\)[\s\n]*\)""",
            sql_text,
        )

    @pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
    async def test_handle_sort_by_metrics(self, db_session: AsyncSession) -> None:
        """Should properly handle sort by metrics in various scenarios."""
        ds = await _load_datasource(db_session, "birth_names")

        sql_text = await get_sql_text(
            db_session, get_query_context("birth_names", ds.id)
        )
        # Postgres backend
        assert re.search(r'ORDER BY[\s\n]* [`"\[]?sum__num[`"\]]? DESC', sql_text)

        sql_text = await get_sql_text(
            db_session,
            get_query_context("birth_names:only_orderby_has_metric", ds.id),
        )
        assert re.search(
            r'ORDER BY[\s\n]* SUM\([`"\[]?num[`"\]]?\) DESC',
            sql_text,
            re.IGNORECASE,
        )

        sql_text = await get_sql_text(
            db_session, get_query_context("birth_names:orderby_dup_alias", ds.id)
        )

        # Check SELECT clauses
        assert re.search(
            r'SUM\([`"\[]?num_boys[`"\]]?\) AS [`\"\[]?num_boys[`"\]]?',
            sql_text,
            re.IGNORECASE,
        )

        # Check ORDER BY clauses — should reference the adhoc metric by alias
        assert re.search(
            r'ORDER BY[\s\n]* [`"\[]?num_girls[`"\]]? DESC',
            sql_text,
            re.IGNORECASE,
        )

        # ORDER BY only columns should always be expressions
        assert re.search(
            r'AVG\([`"\[]?num_boys[`"\]]?\) DESC',
            sql_text,
            re.IGNORECASE,
        )
        assert re.search(r"MAX\(CASE.*END\) ASC", sql_text, re.IGNORECASE | re.DOTALL)

    @pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
    async def test_fetch_values_predicate(self, db_session: AsyncSession) -> None:
        """Ensure that fetch values predicate is added to query if needed."""
        ds = await _load_datasource(db_session, "birth_names")
        # Upstream's birth_names example dataset ships with
        # ``fetch_values_predicate = "123 = 123"``; the Liteset seed loader does
        # not populate it, so set it here to exercise the same predicate path.
        ds.fetch_values_predicate = "123 = 123"
        await db_session.flush()

        payload = get_query_context("birth_names", ds.id)
        sql_text = await get_sql_text(db_session, payload)
        assert "123 = 123" not in sql_text

        payload["queries"][0]["apply_fetch_values_predicate"] = True
        sql_text = await get_sql_text(db_session, payload)
        assert "123 = 123" in sql_text

    async def test_query_object_unknown_fields(
        self, db_session: AsyncSession
    ) -> None:
        """Query objects with unknown fields don't raise and keep the cache key."""
        ds = await _load_datasource(db_session, "birth_names")
        payload = get_query_context("birth_names", ds.id)
        query_context, processor, _ = await _load_query_context(db_session, payload)
        responses = await _get_payload(query_context, processor)
        orig_cache_key = responses["queries"][0]["cache_key"]
        payload["queries"][0]["foo"] = "bar"
        query_context, processor, _ = await _load_query_context(db_session, payload)
        responses = await _get_payload(query_context, processor)
        new_cache_key = responses["queries"][0]["cache_key"]
        assert orig_cache_key == new_cache_key

    @pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
    async def test_time_offsets_in_query_object(
        self, db_session: AsyncSession
    ) -> None:
        """Ensure that time_offsets can generate the correct query."""
        ds = await _load_datasource(db_session, "birth_names")
        payload = get_query_context("birth_names", ds.id)
        payload["queries"][0]["metrics"] = ["sum__num"]
        payload["queries"][0]["groupby"] = ["name"]
        payload["queries"][0]["is_timeseries"] = True
        payload["queries"][0]["timeseries_limit"] = 5
        payload["queries"][0]["time_offsets"] = ["1 year ago", "1 year later"]
        payload["queries"][0]["time_range"] = "1990 : 1991"
        query_context, processor, _ = await _load_query_context(db_session, payload)
        responses = await _get_payload(query_context, processor)
        assert responses["queries"][0]["colnames"] == [
            "__timestamp",
            "name",
            "sum__num",
            "sum__num__1 year ago",
            "sum__num__1 year later",
        ]

        sqls = [
            sql for sql in responses["queries"][0]["query"].split(";") if sql.strip()
        ]
        assert len(sqls) == 3
        # 1 year ago - should only contain the shifted range
        assert re.search(r"1989-01-01.+1990-01-01", sqls[1], re.S)
        # 1 year later - should only contain the shifted range
        assert re.search(r"1991-01-01.+1992-01-01", sqls[2], re.S)

    @pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
    async def test_processing_time_offsets_cache(
        self, db_session: AsyncSession
    ) -> None:
        """Ensure that time_offsets can generate the correct query."""
        ds = await _load_datasource(db_session, "birth_names")
        payload = get_query_context("birth_names", ds.id)
        payload["queries"][0]["metrics"] = ["sum__num"]
        # should process empty dataframe correctly
        # due to "name" being random generated, each time_offset slice is empty
        payload["queries"][0]["groupby"] = ["name"]
        payload["queries"][0]["is_timeseries"] = True
        payload["queries"][0]["timeseries_limit"] = 5
        payload["queries"][0]["time_offsets"] = []
        payload["queries"][0]["time_range"] = "1990 : 1991"
        payload["queries"][0]["granularity"] = "ds"
        payload["queries"][0]["extras"]["time_grain_sqla"] = "P1Y"
        cache: _DictCache = _DictCache()
        query_context, processor, _ = await _load_query_context(
            db_session, payload, cache_manager=cache
        )
        query_object = query_context.queries[0]
        query_result = await processor._get_query_result(query_object)  # noqa: SLF001
        # get main query dataframe
        df = query_result["df"]

        payload["queries"][0]["time_offsets"] = ["1 year ago", "1 year later"]
        query_context, processor, _ = await _load_query_context(
            db_session, payload, cache_manager=cache
        )
        query_object = query_context.queries[0]
        # query without cache
        await processor.processing_time_offsets(df.copy(), query_object)
        # query with cache
        rv = await processor.processing_time_offsets(df.copy(), query_object)
        cache_keys = rv["cache_keys"]
        cache_keys__1_year_ago = cache_keys[0]
        cache_keys__1_year_later = cache_keys[1]
        assert cache_keys__1_year_ago is not None
        assert cache_keys__1_year_later is not None
        assert cache_keys__1_year_ago != cache_keys__1_year_later

        # swap offsets
        payload["queries"][0]["time_offsets"] = ["1 year later", "1 year ago"]
        query_context, processor, _ = await _load_query_context(
            db_session, payload, cache_manager=cache
        )
        query_object = query_context.queries[0]
        rv = await processor.processing_time_offsets(df.copy(), query_object)
        cache_keys = rv["cache_keys"]
        assert cache_keys__1_year_ago == cache_keys[1]
        assert cache_keys__1_year_later == cache_keys[0]

        # remove all offsets
        payload["queries"][0]["time_offsets"] = []
        query_context, processor, _ = await _load_query_context(
            db_session, payload, cache_manager=cache
        )
        query_object = query_context.queries[0]
        rv = await processor.processing_time_offsets(df.copy(), query_object)

        assert rv["df"].shape == df.shape
        assert rv["queries"] == []
        assert rv["cache_keys"] == []

    @pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
    async def test_time_offsets_sql(self, db_session: AsyncSession) -> None:
        ds = await _load_datasource(db_session, "birth_names")
        payload = get_query_context("birth_names", ds.id)
        payload["queries"][0]["metrics"] = ["sum__num"]
        payload["queries"][0]["groupby"] = ["state"]
        payload["queries"][0]["is_timeseries"] = True
        payload["queries"][0]["timeseries_limit"] = 5
        payload["queries"][0]["time_offsets"] = []
        payload["queries"][0]["time_range"] = "1980 : 1991"
        payload["queries"][0]["granularity"] = "ds"
        payload["queries"][0]["extras"]["time_grain_sqla"] = "P1Y"
        query_context, processor, _ = await _load_query_context(db_session, payload)
        query_object = query_context.queries[0]
        query_result = await processor._get_query_result(query_object)  # noqa: SLF001
        df = query_result["df"]

        # set time_offsets to query_object
        payload["queries"][0]["time_offsets"] = ["3 years ago", "3 years later"]
        query_context, processor, _ = await _load_query_context(db_session, payload)
        query_object = query_context.queries[0]
        time_offsets_obj = await processor.processing_time_offsets(df, query_object)
        query_from_1977_to_1988 = time_offsets_obj["queries"][0]
        query_from_1983_to_1994 = time_offsets_obj["queries"][1]

        # should generate expected date range in sql
        assert "1977-01-01" in query_from_1977_to_1988
        assert "1988-01-01" in query_from_1977_to_1988
        assert "1983-01-01" in query_from_1983_to_1994
        assert "1994-01-01" in query_from_1983_to_1994

    @pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
    async def test_time_offsets_accuracy(self, db_session: AsyncSession) -> None:
        ds = await _load_datasource(db_session, "birth_names")
        payload = get_query_context("birth_names", ds.id)
        payload["queries"][0]["metrics"] = ["sum__num"]
        payload["queries"][0]["groupby"] = ["state"]
        payload["queries"][0]["is_timeseries"] = True
        payload["queries"][0]["timeseries_limit"] = 5
        payload["queries"][0]["time_offsets"] = []
        payload["queries"][0]["time_range"] = "1980 : 1991"
        payload["queries"][0]["granularity"] = "ds"
        payload["queries"][0]["extras"]["time_grain_sqla"] = "P1Y"
        query_context, processor, _ = await _load_query_context(db_session, payload)
        query_object = query_context.queries[0]
        query_result = await processor._get_query_result(query_object)  # noqa: SLF001
        df = query_result["df"]

        # set time_offsets to query_object
        payload["queries"][0]["time_offsets"] = ["3 years ago", "3 years later"]
        query_context, processor, _ = await _load_query_context(db_session, payload)
        query_object = query_context.queries[0]
        time_offsets_obj = await processor.processing_time_offsets(df, query_object)
        df_with_offsets = time_offsets_obj["df"]
        df_with_offsets = df_with_offsets.set_index(["__timestamp", "state"])

        # should get correct data when applying "3 years ago"
        payload["queries"][0]["time_offsets"] = []
        payload["queries"][0]["time_range"] = "1977 : 1988"
        query_context, processor, _ = await _load_query_context(db_session, payload)
        query_object = query_context.queries[0]
        query_result = await processor._get_query_result(query_object)  # noqa: SLF001
        df_3_years_ago = query_result["df"]
        df_3_years_ago["__timestamp"] = df_3_years_ago["__timestamp"] + DateOffset(
            years=3
        )
        df_3_years_ago = df_3_years_ago.set_index(["__timestamp", "state"])
        for index, row in df_with_offsets.iterrows():
            if index in df_3_years_ago.index:
                assert (
                    row["sum__num__3 years ago"]
                    == df_3_years_ago.loc[index]["sum__num"]
                )

        # should get correct data when applying "3 years later"
        payload["queries"][0]["time_offsets"] = []
        payload["queries"][0]["time_range"] = "1983 : 1994"
        query_context, processor, _ = await _load_query_context(db_session, payload)
        query_object = query_context.queries[0]
        query_result = await processor._get_query_result(query_object)  # noqa: SLF001
        df_3_years_later = query_result["df"]
        df_3_years_later["__timestamp"] = df_3_years_later["__timestamp"] - DateOffset(
            years=3
        )
        df_3_years_later = df_3_years_later.set_index(["__timestamp", "state"])
        for index, row in df_with_offsets.iterrows():
            if index in df_3_years_later.index:
                assert (
                    row["sum__num__3 years later"]
                    == df_3_years_later.loc[index]["sum__num"]
                )

    @pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
    async def test_time_offsets_in_query_object_no_limit(
        self, db_session: AsyncSession
    ) -> None:
        """Time_offsets queries don't reuse row_limit/row_offset from the
        original query object.

        Upstream mocked ``get_query_result`` to feed a fixed main df and then
        asserted on the offset SQL — but the port builds the offset subquery SQL
        *inside* ``processing_time_offsets`` via the (real) executor, so mocking
        ``_get_query_result`` would also blank the offset SQL. Instead this runs
        the real main query and asserts on the genuine offset SQL the port
        generates, which is the same behaviour the upstream assertion targets.
        """
        ds = await _load_datasource(db_session, "birth_names")
        payload = get_query_context("birth_names", ds.id)
        payload["queries"][0]["columns"] = [
            {
                "timeGrain": "P1D",
                "columnType": "BASE_AXIS",
                "sqlExpression": "ds",
                "label": "ds",
                "expressionType": "SQL",
            }
        ]
        payload["queries"][0]["metrics"] = ["sum__num"]
        payload["queries"][0]["groupby"] = ["name"]
        payload["queries"][0]["is_timeseries"] = True
        payload["queries"][0]["row_limit"] = 100
        payload["queries"][0]["row_offset"] = 10
        payload["queries"][0]["time_range"] = "1990 : 1991"

        query_context, processor, _ = await _load_query_context(db_session, payload)
        query_object = query_context.queries[0]
        query_result = await processor._get_query_result(query_object)  # noqa: SLF001
        df = query_result["df"]

        # Setup the payload for time offsets
        payload["queries"][0]["time_offsets"] = ["1 year ago", "1 year later"]
        query_context, processor, _ = await _load_query_context(db_session, payload)
        query_object = query_context.queries[0]
        time_offsets_obj = await processor.processing_time_offsets(df, query_object)
        sqls = time_offsets_obj["queries"]
        row_limit_value = SupersetSettings().row_limit  # type: ignore[call-arg]
        row_limit_pattern_with_config_value = r"LIMIT " + re.escape(
            str(row_limit_value)
        )
        assert len(sqls) == 2
        # 1 year ago
        assert re.search(r"1989-01-01.+1990-01-01", sqls[0], re.S)
        assert not re.search(r"LIMIT 100", sqls[0], re.S)
        assert not re.search(r"OFFSET 10", sqls[0], re.S)
        assert re.search(row_limit_pattern_with_config_value, sqls[0], re.S)
        # 1 year later
        assert re.search(r"1991-01-01.+1992-01-01", sqls[1], re.S)
        assert not re.search(r"LIMIT 100", sqls[1], re.S)
        assert not re.search(r"OFFSET 10", sqls[1], re.S)
        assert re.search(row_limit_pattern_with_config_value, sqls[1], re.S)


# ---------------------------------------------------------------------------
# Module-level virtual / physical dataset tests.
#
# These replace the upstream ``virtual_dataset_*`` / ``physical_dataset``
# fixtures (which created example-database tables + datasets) with factory rows
# built on the seeded Postgres backend.  Each builds a real ``SqlaTable`` whose
# ``sql`` (virtual) or backing physical table runs against the seeded warehouse
# (the example ``Database``), then drives ``QueryContextFactory().create`` ->
# ``get_df_payload`` exactly like upstream.
# ---------------------------------------------------------------------------


async def _example_database_id(session: AsyncSession) -> int:
    """Resolve the seeded example ``Database`` id (birth_names lives on it)."""
    birth = await _load_datasource(session, "birth_names")
    return birth.database_id


async def _create_query_context(
    session: AsyncSession,
    datasource: SqlaTable,
    queries: list[dict[str, Any]],
    *,
    result_type: str = "full",
) -> tuple[AsyncQueryContext, AsyncQueryContextProcessor]:
    """Liteset equivalent of ``QueryContextFactory().create(...)``."""
    ds_dict = {"id": datasource.id, "type": "table"}
    query_objects = [AsyncQueryObject.from_request(q, ds_dict) for q in queries]
    query_context = AsyncQueryContext(
        datasource=datasource,
        queries=query_objects,
        result_type=result_type,
        force=True,
        form_data={},
    )
    processor = _make_processor(session, datasource, query_context)
    return query_context, processor


async def _make_virtual_dataset(
    session: AsyncSession,
    *,
    table_name: str,
    sql: str,
    columns: list[tuple[str, str]],
) -> SqlaTable:
    """Persist a virtual ``SqlaTable`` (with ``sql``) on the example database."""
    database_id = await _example_database_id(session)
    dataset = SqlaTable(table_name=table_name, sql=sql, database_id=database_id)
    session.add(dataset)
    await session.flush()
    from superset.models.connectors import SqlMetric as _SqlMetric, TableColumn

    for col_name, col_type in columns:
        session.add(
            TableColumn(table_id=dataset.id, column_name=col_name, type=col_type)
        )
    session.add(
        _SqlMetric(table_id=dataset.id, metric_name="count", expression="count(*)")
    )
    await session.flush()
    return await _load_datasource_by_id(session, dataset.id)


async def _make_physical_dataset(session: AsyncSession) -> SqlaTable:
    """Create the ``physical_dataset`` table + dataset on the example database.

    1:1 with the upstream ``physical_dataset`` fixture (10 rows, 7 columns,
    3 temporal). The physical table is created in the seeded warehouse via the
    dataset's database engine.
    """
    database_id = await _example_database_id(session)
    from superset.models.connectors import SqlMetric as _SqlMetric, TableColumn
    from superset.models.core import Database

    database = (
        await session.execute(select(Database).where(Database.id == database_id))
    ).scalar_one()

    def _build_table() -> None:
        from superset.connectors.sqla.utils import get_identifier_quoter

        with database.get_sqla_engine() as engine:
            quoter = get_identifier_quoter(engine.name)
            with engine.begin() as conn:
                conn.exec_driver_sql("DROP TABLE IF EXISTS physical_dataset")
                conn.exec_driver_sql(
                    f"""
                    CREATE TABLE physical_dataset(
                    col1 INTEGER,
                    col2 VARCHAR(255),
                    col3 DECIMAL(4,2),
                    col4 VARCHAR(255),
                    col5 TIMESTAMP DEFAULT '1970-01-01 00:00:01',
                    col6 TIMESTAMP DEFAULT '1970-01-01 00:00:01',
                    {quoter("time column with spaces")} TIMESTAMP
                        DEFAULT '1970-01-01 00:00:01'
                    );
                    """
                )
                conn.exec_driver_sql(
                    """
                    INSERT INTO physical_dataset values
                    (0, 'a', 1.0, NULL, '2000-01-01 00:00:00', '2002-01-03 00:00:00', '2002-01-03 00:00:00'),
                    (1, 'b', 1.1, NULL, '2000-01-02 00:00:00', '2002-02-04 00:00:00', '2002-02-04 00:00:00'),
                    (2, 'c', 1.2, NULL, '2000-01-03 00:00:00', '2002-03-07 00:00:00', '2002-03-07 00:00:00'),
                    (3, 'd', 1.3, NULL, '2000-01-04 00:00:00', '2002-04-12 00:00:00', '2002-04-12 00:00:00'),
                    (4, 'e', 1.4, NULL, '2000-01-05 00:00:00', '2002-05-11 00:00:00', '2002-05-11 00:00:00'),
                    (5, 'f', 1.5, NULL, '2000-01-06 00:00:00', '2002-06-13 00:00:00', '2002-06-13 00:00:00'),
                    (6, 'g', 1.6, NULL, '2000-01-07 00:00:00', '2002-07-15 00:00:00', '2002-07-15 00:00:00'),
                    (7, 'h', 1.7, NULL, '2000-01-08 00:00:00', '2002-08-18 00:00:00', '2002-08-18 00:00:00'),
                    (8, 'i', 1.8, NULL, '2000-01-09 00:00:00', '2002-09-20 00:00:00', '2002-09-20 00:00:00'),
                    (9, 'j', 1.9, NULL, '2000-01-10 00:00:00', '2002-10-22 00:00:00', '2002-10-22 00:00:00');
                    """  # noqa: E501
                )

    import asyncio as _asyncio

    await _asyncio.to_thread(_build_table)

    dataset = SqlaTable(table_name="physical_dataset", database_id=database_id)
    session.add(dataset)
    await session.flush()
    session.add_all(
        [
            TableColumn(table_id=dataset.id, column_name="col1", type="INTEGER"),
            TableColumn(table_id=dataset.id, column_name="col2", type="VARCHAR(255)"),
            TableColumn(table_id=dataset.id, column_name="col3", type="DECIMAL(4,2)"),
            TableColumn(table_id=dataset.id, column_name="col4", type="VARCHAR(255)"),
            TableColumn(
                table_id=dataset.id, column_name="col5", type="TIMESTAMP", is_dttm=True
            ),
            TableColumn(
                table_id=dataset.id, column_name="col6", type="TIMESTAMP", is_dttm=True
            ),
            TableColumn(
                table_id=dataset.id,
                column_name="time column with spaces",
                type="TIMESTAMP",
                is_dttm=True,
            ),
            _SqlMetric(
                table_id=dataset.id, metric_name="count", expression="count(*)"
            ),
        ]
    )
    await session.flush()
    return await _load_datasource_by_id(session, dataset.id)


async def _drop_physical_dataset(session: AsyncSession) -> None:
    database_id = await _example_database_id(session)
    from superset.models.core import Database

    database = (
        await session.execute(select(Database).where(Database.id == database_id))
    ).scalar_one()

    def _drop() -> None:
        with database.get_sqla_engine() as engine, engine.begin() as conn:
            conn.exec_driver_sql("DROP TABLE IF EXISTS physical_dataset")

    import asyncio as _asyncio

    await _asyncio.to_thread(_drop)


async def test_get_label_map(db_session: AsyncSession) -> None:
    datasource = await _make_virtual_dataset(
        db_session,
        table_name="virtual_dataset",
        sql=(
            "SELECT 'col1,row1' as col1, 'col2, row1' as col2 "
            "UNION ALL "
            "SELECT 'col1,row2' as col1, 'col2, row2' as col2 "
            "UNION ALL "
            "SELECT 'col1,row3' as col1, 'col2, row3' as col2 "
        ),
        columns=[("col1", "VARCHAR(255)"), ("col2", "VARCHAR(255)")],
    )
    _qc, processor = await _create_query_context(
        db_session,
        datasource,
        [
            {
                "columns": ["col1", "col2"],
                "metrics": ["count"],
                "post_processing": [
                    {
                        "operation": "pivot",
                        "options": {
                            "aggregates": {"count": {"operator": "mean"}},
                            "columns": ["col2"],
                            "index": ["col1"],
                        },
                    },
                    {"operation": "flatten"},
                ],
            }
        ],
    )
    query_object = _qc.queries[0]
    payload = await processor.get_df_payload(query_object)
    df = payload["df"]
    label_map = payload["label_map"]
    assert list(df.columns.values) == [
        "col1",
        "count" + FLAT_COLUMN_SEPARATOR + "col2, row1",
        "count" + FLAT_COLUMN_SEPARATOR + "col2, row2",
        "count" + FLAT_COLUMN_SEPARATOR + "col2, row3",
    ]
    assert label_map == {
        "col1": ["col1"],
        "count, col2, row1": ["count", "col2, row1"],
        "count, col2, row2": ["count", "col2, row2"],
        "count, col2, row3": ["count", "col2, row3"],
        "col2": ["col2"],
        "count": ["count"],
    }


async def test_time_column_with_time_grain(db_session: AsyncSession) -> None:
    datasource = await _make_physical_dataset(db_session)
    try:
        column_on_axis: AdhocColumn = {
            "label": "I_AM_AN_ORIGINAL_COLUMN",
            "sqlExpression": "col5",
            "timeGrain": "P1Y",
            "isColumnReference": True,
        }
        adhoc_column: AdhocColumn = {
            "label": "I_AM_A_TRUNC_COLUMN",
            "sqlExpression": "col6",
            "columnType": "BASE_AXIS",
            "timeGrain": "P1Y",
            "isColumnReference": True,
        }
        _qc, processor = await _create_query_context(
            db_session,
            datasource,
            [
                {
                    "columns": ["col1", column_on_axis, adhoc_column],
                    "metrics": ["count"],
                    "orderby": [["col1", True]],
                }
            ],
        )
        query_object = _qc.queries[0]
        df = (await processor.get_df_payload(query_object))["df"]
        # Postgres returns datetime values
        assert df["I_AM_AN_ORIGINAL_COLUMN"][0].strftime("%Y-%m-%d") == "2000-01-01"
        assert df["I_AM_AN_ORIGINAL_COLUMN"][1].strftime("%Y-%m-%d") == "2000-01-02"
        assert df["I_AM_A_TRUNC_COLUMN"][0].strftime("%Y-%m-%d") == "2002-01-01"
        assert df["I_AM_A_TRUNC_COLUMN"][1].strftime("%Y-%m-%d") == "2002-01-01"
    finally:
        await _drop_physical_dataset(db_session)


async def test_non_time_column_with_time_grain(db_session: AsyncSession) -> None:
    datasource = await _make_physical_dataset(db_session)
    try:
        _qc, processor = await _create_query_context(
            db_session,
            datasource,
            [
                {
                    "columns": [
                        "col1",
                        {
                            "label": "COL2 ALIAS",
                            "sqlExpression": "col2",
                            "columnType": "BASE_AXIS",
                            "timeGrain": "P1Y",
                        },
                    ],
                    "metrics": ["count"],
                    "orderby": [["col1", True]],
                    "row_limit": 1,
                }
            ],
        )
        query_object = _qc.queries[0]
        df = (await processor.get_df_payload(query_object))["df"]
        assert df["COL2 ALIAS"][0] == "a"
    finally:
        await _drop_physical_dataset(db_session)


async def test_special_chars_in_column_name(db_session: AsyncSession) -> None:
    datasource = await _make_physical_dataset(db_session)
    saved = dict(feature_flag_manager._feature_flags)  # noqa: SLF001
    feature_flag_manager._feature_flags["ALLOW_ADHOC_SUBQUERY"] = True  # noqa: SLF001
    try:
        _qc, processor = await _create_query_context(
            db_session,
            datasource,
            [
                {
                    "columns": [
                        "col1",
                        "time column with spaces",
                    ],
                    "metrics": ["count"],
                    "orderby": [["col1", True]],
                    "row_limit": 1,
                }
            ],
        )
        query_object = _qc.queries[0]
        df = (await processor.get_df_payload(query_object))["df"]
        # Postgres returns datetime values
        assert df["time column with spaces"][0].strftime("%Y-%m-%d") == "2002-01-03"
    finally:
        feature_flag_manager._feature_flags = saved  # noqa: SLF001
        await _drop_physical_dataset(db_session)


async def test_date_adhoc_column(db_session: AsyncSession) -> None:
    datasource = await _make_physical_dataset(db_session)
    try:
        # sql expression returns date type
        column_on_axis: AdhocColumn = {
            "label": "ADHOC COLUMN",
            "sqlExpression": "col6 + interval '20 year'",
            "columnType": "BASE_AXIS",
            "timeGrain": "P1Y",
        }
        _qc, processor = await _create_query_context(
            db_session,
            datasource,
            [
                {
                    "columns": [column_on_axis],
                    "metrics": ["count"],
                }
            ],
        )
        query_object = _qc.queries[0]
        df = (await processor.get_df_payload(query_object))["df"]
        #   ADHOC COLUMN  count
        # 0   2022-01-01     10
        assert df["ADHOC COLUMN"][0].strftime("%Y-%m-%d") == "2022-01-01"
        assert df["count"][0] == 10
    finally:
        await _drop_physical_dataset(db_session)


async def test_non_date_adhoc_column(db_session: AsyncSession) -> None:
    datasource = await _make_physical_dataset(db_session)
    try:
        # sql expression returns non-date type
        column_on_axis: AdhocColumn = {
            "label": "ADHOC COLUMN",
            "sqlExpression": "col1 * 10",
            "columnType": "BASE_AXIS",
            "timeGrain": "P1Y",
        }
        _qc, processor = await _create_query_context(
            db_session,
            datasource,
            [
                {
                    "columns": [column_on_axis],
                    "metrics": ["count"],
                    "orderby": [
                        [
                            {
                                "expressionType": "SQL",
                                "sqlExpression": '"ADHOC COLUMN"',
                            },
                            True,
                        ]
                    ],
                }
            ],
        )
        query_object = _qc.queries[0]
        df = (await processor.get_df_payload(query_object))["df"]
        assert df["ADHOC COLUMN"][0] == 0
        assert df["ADHOC COLUMN"][1] == 10
    finally:
        await _drop_physical_dataset(db_session)


@pytest.mark.skip(
    reason="only_sqlite: upstream asserts sqlite-specific DATETIME() truncation "
    "SQL + dtypes; the seeded backend is Postgres only"
)
async def test_time_grain_and_time_offset_with_base_axis() -> None:
    pass


@pytest.mark.skip(
    reason="only_sqlite: upstream asserts sqlite-specific DATETIME() truncation "
    "SQL + dtypes; the seeded backend is Postgres only"
)
async def test_time_grain_and_time_offset_on_legacy_query() -> None:
    pass


async def test_time_offset_with_temporal_range_filter(
    db_session: AsyncSession,
) -> None:
    datasource = await _make_physical_dataset(db_session)
    try:
        _qc, processor = await _create_query_context(
            db_session,
            datasource,
            [
                {
                    "columns": [
                        {
                            "label": "col6",
                            "sqlExpression": "col6",
                            "columnType": "BASE_AXIS",
                            "timeGrain": "P3M",
                            "isColumnReference": True,
                        }
                    ],
                    "metrics": [
                        {
                            "label": "SUM(col1)",
                            "expressionType": "SQL",
                            "sqlExpression": "SUM(col1)",
                        }
                    ],
                    "time_offsets": ["3 month ago"],
                    "filters": [
                        {
                            "col": "col6",
                            "op": "TEMPORAL_RANGE",
                            "val": "2002-01 : 2003-01",
                        }
                    ],
                }
            ],
        )
        query_payload = await processor.get_df_payload(_qc.queries[0])
        df = query_payload["df"]
        assert df["SUM(col1)"].to_list() == [3, 12, 21, 9]
        # "SUM(col1)__3 month ago" dtype is object -> convert to float first
        assert df["SUM(col1)__3 month ago"].astype("float").astype(
            "Int64"
        ).to_list() == [
            pd.NA,
            3,
            12,
            21,
        ]

        sqls = query_payload["query"].split(";")
        assert (
            re.search(r"WHERE col6 >= .*2002-01-01", sqls[0])
            and re.search(r"AND col6 < .*2003-01-01", sqls[0])
        ) is not None
        assert (
            re.search(r"WHERE col6 >= .*2001-10-01", sqls[1])
            and re.search(r"AND col6 < .*2002-10-01", sqls[1])
        ) is not None
    finally:
        await _drop_physical_dataset(db_session)


@_XFAIL_DATE_RANGE_OFFSET_DTYPE
async def test_date_range_timeshift_enabled(db_session: AsyncSession) -> None:
    """Date range timeshift functionality when the feature flag is enabled."""
    datasource = await _make_physical_dataset(db_session)
    saved = dict(feature_flag_manager._feature_flags)  # noqa: SLF001
    feature_flag_manager._feature_flags["DATE_RANGE_TIMESHIFTS_ENABLED"] = True  # noqa: SLF001
    try:
        _qc, processor = await _create_query_context(
            db_session,
            datasource,
            [
                {
                    "columns": [
                        {
                            "label": "col6",
                            "sqlExpression": "col6",
                            "columnType": "BASE_AXIS",
                            "timeGrain": "P1M",
                        }
                    ],
                    "metrics": [
                        {
                            "label": "SUM(col1)",
                            "expressionType": "SQL",
                            "sqlExpression": "SUM(col1)",
                        }
                    ],
                    "time_offsets": ["2001-01-01 : 2001-12-31"],
                    "filters": [
                        {
                            "col": "col6",
                            "op": "TEMPORAL_RANGE",
                            "val": "2002-01-01 : 2002-12-31",
                        }
                    ],
                }
            ],
        )
        query_payload = await processor.get_df_payload(_qc.queries[0])
        df = query_payload["df"]

        # Should have both main metrics and offset metrics columns
        assert "SUM(col1)" in df.columns
        assert "SUM(col1)__2001-01-01 : 2001-12-31" in df.columns

        sqls = query_payload["query"].split(";")
        assert len(sqls) >= 2  # Main query + offset query

        # Main query should filter for 2002 data
        main_sql = sqls[0]
        assert "2002-01-01" in main_sql
        assert "2002-12-31" in main_sql or "2003-01-01" in main_sql

        # Offset query should filter for 2001 data
        offset_sql = sqls[1]
        assert "2001-01-01" in offset_sql
        assert "2001-12-31" in offset_sql or "2002-01-01" in offset_sql
    finally:
        feature_flag_manager._feature_flags = saved  # noqa: SLF001
        await _drop_physical_dataset(db_session)


async def test_date_range_timeshift_disabled(db_session: AsyncSession) -> None:
    """Date range timeshift raises an error when the feature flag is disabled."""
    datasource = await _make_physical_dataset(db_session)
    saved = dict(feature_flag_manager._feature_flags)  # noqa: SLF001
    feature_flag_manager._feature_flags["DATE_RANGE_TIMESHIFTS_ENABLED"] = False  # noqa: SLF001
    try:
        _qc, processor = await _create_query_context(
            db_session,
            datasource,
            [
                {
                    "columns": [
                        {
                            "label": "col6",
                            "sqlExpression": "col6",
                            "columnType": "BASE_AXIS",
                            "timeGrain": "P1M",
                        }
                    ],
                    "metrics": [
                        {
                            "label": "SUM(col1)",
                            "expressionType": "SQL",
                            "sqlExpression": "SUM(col1)",
                        }
                    ],
                    "time_offsets": ["2001-01-01 : 2001-12-31"],
                    "filters": [
                        {
                            "col": "col6",
                            "op": "TEMPORAL_RANGE",
                            "val": "2002-01-01 : 2002-12-31",
                        }
                    ],
                }
            ],
        )
        from superset.exceptions import QueryObjectValidationError

        with pytest.raises(
            QueryObjectValidationError, match="Date range timeshifts are not enabled"
        ):
            await processor.get_df_payload(_qc.queries[0])
    finally:
        feature_flag_manager._feature_flags = saved  # noqa: SLF001
        await _drop_physical_dataset(db_session)


@_XFAIL_DATE_RANGE_OFFSET_DTYPE
async def test_date_range_timeshift_multiple_periods(
    db_session: AsyncSession,
) -> None:
    """Date range timeshift with multiple comparison periods."""
    datasource = await _make_physical_dataset(db_session)
    saved = dict(feature_flag_manager._feature_flags)  # noqa: SLF001
    feature_flag_manager._feature_flags["DATE_RANGE_TIMESHIFTS_ENABLED"] = True  # noqa: SLF001
    try:
        _qc, processor = await _create_query_context(
            db_session,
            datasource,
            [
                {
                    "columns": [
                        {
                            "label": "col6",
                            "sqlExpression": "col6",
                            "columnType": "BASE_AXIS",
                            "timeGrain": "P1M",
                        }
                    ],
                    "metrics": [
                        {
                            "label": "SUM(col1)",
                            "expressionType": "SQL",
                            "sqlExpression": "SUM(col1)",
                        }
                    ],
                    "time_offsets": [
                        "2001-01-01 : 2001-12-31",
                        "2000-01-01 : 2000-12-31",
                    ],
                    "filters": [
                        {
                            "col": "col6",
                            "op": "TEMPORAL_RANGE",
                            "val": "2002-01-01 : 2002-12-31",
                        }
                    ],
                }
            ],
        )
        query_payload = await processor.get_df_payload(_qc.queries[0])
        df = query_payload["df"]

        assert "SUM(col1)" in df.columns
        assert "SUM(col1)__2001-01-01 : 2001-12-31" in df.columns
        assert "SUM(col1)__2000-01-01 : 2000-12-31" in df.columns

        # Check that all queries were generated
        sqls = query_payload["query"].split(";")
        assert len(sqls) >= 3  # Main query + 2 offset queries
    finally:
        feature_flag_manager._feature_flags = saved  # noqa: SLF001
        await _drop_physical_dataset(db_session)


async def test_date_range_timeshift_invalid_format(
    db_session: AsyncSession,
) -> None:
    """Invalid date range format raises an appropriate error."""
    datasource = await _make_physical_dataset(db_session)
    saved = dict(feature_flag_manager._feature_flags)  # noqa: SLF001
    feature_flag_manager._feature_flags["DATE_RANGE_TIMESHIFTS_ENABLED"] = True  # noqa: SLF001
    try:
        _qc, processor = await _create_query_context(
            db_session,
            datasource,
            [
                {
                    "columns": [
                        {
                            "label": "col6",
                            "sqlExpression": "col6",
                            "columnType": "BASE_AXIS",
                            "timeGrain": "P1M",
                        }
                    ],
                    "metrics": [
                        {
                            "label": "SUM(col1)",
                            "expressionType": "SQL",
                            "sqlExpression": "SUM(col1)",
                        }
                    ],
                    "time_offsets": ["invalid-date-range"],
                    "filters": [
                        {
                            "col": "col6",
                            "op": "TEMPORAL_RANGE",
                            "val": "2002-01-01 : 2002-12-31",
                        }
                    ],
                }
            ],
        )
        # Upstream imported ``TimeDeltaAmbiguousError`` from
        # ``superset.commands.chart.exceptions``; the port homes it in
        # ``superset.utils.date`` (same class, same message).
        from superset.utils.date import TimeDeltaAmbiguousError

        with pytest.raises(TimeDeltaAmbiguousError):
            await processor.get_df_payload(_qc.queries[0])
    finally:
        feature_flag_manager._feature_flags = saved  # noqa: SLF001
        await _drop_physical_dataset(db_session)


@_XFAIL_DATE_RANGE_OFFSET_DTYPE
async def test_date_range_timeshift_mixed_with_relative_offsets(
    db_session: AsyncSession,
) -> None:
    """Mixing date range timeshifts with traditional relative offsets."""
    datasource = await _make_physical_dataset(db_session)
    saved = dict(feature_flag_manager._feature_flags)  # noqa: SLF001
    feature_flag_manager._feature_flags["DATE_RANGE_TIMESHIFTS_ENABLED"] = True  # noqa: SLF001
    try:
        _qc, processor = await _create_query_context(
            db_session,
            datasource,
            [
                {
                    "columns": [
                        {
                            "label": "col6",
                            "sqlExpression": "col6",
                            "columnType": "BASE_AXIS",
                            "timeGrain": "P1M",
                        }
                    ],
                    "metrics": [
                        {
                            "label": "SUM(col1)",
                            "expressionType": "SQL",
                            "sqlExpression": "SUM(col1)",
                        }
                    ],
                    "time_offsets": [
                        "2001-01-01 : 2001-12-31",
                        "1 year ago",
                    ],
                    "filters": [
                        {
                            "col": "col6",
                            "op": "TEMPORAL_RANGE",
                            "val": "2002-01-01 : 2002-12-31",
                        }
                    ],
                }
            ],
        )
        query_payload = await processor.get_df_payload(_qc.queries[0])
        df = query_payload["df"]

        assert "SUM(col1)" in df.columns
        assert "SUM(col1)__2001-01-01 : 2001-12-31" in df.columns
        assert "SUM(col1)__1 year ago" in df.columns

        # Check that all queries were generated
        sqls = query_payload["query"].split(";")
        assert len(sqls) >= 3  # Main query + 2 offset queries
    finally:
        feature_flag_manager._feature_flags = saved  # noqa: SLF001
        await _drop_physical_dataset(db_session)


async def test_virtual_dataset_with_comments(db_session: AsyncSession) -> None:
    datasource = await _make_virtual_dataset(
        db_session,
        table_name="virtual_dataset_with_comments",
        sql=(
            "--COMMENT\n"
            "/*COMMENT*/\n"
            "WITH cte as (--COMMENT\n"
            "    SELECT 2 as col1, /*COMMENT*/'j' as col2, 1.9, NULL, "
            "'2000-01-10 00:00:00', 10\n"
            ")\n"
            "SELECT 0 as col1, 'a' as col2, 1.0 as col3, NULL as col4, "
            "'2000-01-01 00:00:00' as col5, 1 as col6\n"
            "\n /*  COMMENT */ \n"
            "UNION ALL/*COMMENT*/\n"
            "SELECT 1 as col1, 'f' as col2, 1.5, NULL, "
            "'2000-01-06 00:00:00', 6 --COMMENT\n"
            "UNION ALL--COMMENT\n"
            "SELECT * FROM cte --COMMENT"
        ),
        columns=[
            ("col1", "INTEGER"),
            ("col2", "VARCHAR(255)"),
            ("col3", "DECIMAL(4,2)"),
            ("col4", "VARCHAR(255)"),
            ("col5", "VARCHAR(255)"),
            ("col6", "INTEGER"),
        ],
    )
    _qc, processor = await _create_query_context(
        db_session,
        datasource,
        [
            {
                "columns": ["col1", "col2"],
                "metrics": ["count"],
                "post_processing": [
                    {
                        "operation": "pivot",
                        "options": {
                            "aggregates": {"count": {"operator": "mean"}},
                            "columns": ["col2"],
                            "index": ["col1"],
                        },
                    },
                    {"operation": "flatten"},
                ],
            }
        ],
    )
    query_object = _qc.queries[0]
    df = (await processor.get_df_payload(query_object))["df"]
    assert len(df) == 3


# ---------------------------------------------------------------------------
# Minimal in-process cache manager for the cache-key tests (replaces the
# Flask ``cache_manager.cache`` SimpleCache). Stores values exactly the way
# ``AsyncQueryContextProcessor._cache_get`` / ``_cache_set`` expect: pickle
# bytes keyed by string.
# ---------------------------------------------------------------------------


class _DictCache(dict):
    """Sync dict-backed cache exposing the get/set surface the processor uses."""

    def get(self, key: str) -> Any:  # type: ignore[override]
        return dict.get(self, key, None)

    def set(self, key: str, value: Any, timeout: int | None = None) -> None:
        self[key] = value

    def get_obj(self, key: str) -> Any:
        """Return the unpickled value stored under ``key`` (test convenience)."""
        import pickle  # noqa: S403

        raw = dict.get(self, key, None)
        if raw is None:
            return None
        if isinstance(raw, (bytes, bytearray)):
            # pickle is the same serialization the processor's _cache_set uses;
            # data here is produced in-process by the test, never untrusted.
            return pickle.loads(raw)  # noqa: S301
        return raw
