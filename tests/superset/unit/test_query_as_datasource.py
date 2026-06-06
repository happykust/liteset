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
"""Unit tests for the "Query as datasource" feature (datasource_type="query").

Covers:
- the behaviour-preserving ``AsyncQueryExecutionMixin`` extraction on
  ``SqlaTable``;
- the SQL Lab ``Query`` datasource interface (columns synthesized from
  ``extra["columns"]``, perm strings, dttm resolution, etc.);
- async-safety of the synthesized transient ``TableColumn`` objects.
"""

from __future__ import annotations

import json

from superset.models.connectors import AsyncQueryExecutionMixin, SqlaTable, TableColumn
from superset.models.helpers import ExploreMixin
from superset.models.sql_lab import Query


# ---------------------------------------------------------------------------
# Step 1 — behaviour-preserving refactor on SqlaTable
# ---------------------------------------------------------------------------


def test_sqlatable_mixes_in_async_query_execution_mixin() -> None:
    assert issubclass(SqlaTable, AsyncQueryExecutionMixin)
    # The extracted async-execution methods still resolve on SqlaTable.
    for name in (
        "async_query",
        "_build_sql",
        "_get_sqla_query_with_rls",
        "_adapt_query_dict_for_get_sqla_query",
        "_build_from_ast",
        "_execute_sql",
    ):
        assert hasattr(SqlaTable, name), name
    # async_values_for_column (which calls the extracted helpers) is intact.
    assert hasattr(SqlaTable, "async_values_for_column")


def test_async_query_methods_live_on_the_mixin_not_sqlatable() -> None:
    # The methods were moved off SqlaTable's own __dict__ into the mixin.
    for name in (
        "async_query",
        "_build_sql",
        "_build_from_ast",
        "_execute_sql",
        "_get_sqla_query_with_rls",
        "_adapt_query_dict_for_get_sqla_query",
    ):
        assert name in AsyncQueryExecutionMixin.__dict__, name
        assert name not in SqlaTable.__dict__, name


# ---------------------------------------------------------------------------
# Step 2 — Query datasource interface
# ---------------------------------------------------------------------------


def _make_query(**kwargs) -> Query:
    """Build a transient Query with synthesized result columns in ``extra``."""
    extra = {
        "columns": [
            {
                "column_name": "gender",
                "type": "STRING",
                "type_generic": 1,
                "is_dttm": False,
            },
            {
                "column_name": "ds",
                "type": "TIMESTAMP",
                "type_generic": 2,
                "is_dttm": True,
            },
            {
                "column_name": "num",
                "type": "LONGINTEGER",
                "type_generic": 0,
                "is_dttm": False,
            },
        ]
    }
    q = Query(
        id=123,
        client_id="abc",
        database_id=7,
        schema="public",
        catalog=None,
        tab_name="my tab",
        sql="SELECT gender, ds, num FROM birth_names LIMIT 100",
        extra_json=json.dumps(extra),
    )
    for k, v in kwargs.items():
        setattr(q, k, v)
    return q


def test_query_is_explore_and_async_capable() -> None:
    assert issubclass(Query, ExploreMixin)
    assert issubclass(Query, AsyncQueryExecutionMixin)
    assert Query.type == "query"
    # Inherits the full chart-data interface.
    for name in (
        "get_sqla_query",
        "get_query_str_extended",
        "async_query",
        "_build_sql",
        "make_sqla_column_compatible",
        "convert_tbl_column_to_sqla_col",
        "get_from_clause",
    ):
        assert hasattr(Query, name), name


def test_query_columns_synthesized_from_extra() -> None:
    q = _make_query()
    cols = q.columns
    assert len(cols) == 3
    assert all(isinstance(c, TableColumn) for c in cols)
    assert [c.column_name for c in cols] == ["gender", "ds", "num"]
    assert [c.type for c in cols] == ["STRING", "TIMESTAMP", "LONGINTEGER"]
    assert [c.is_dttm for c in cols] == [False, True, False]
    # Synthesized columns are filterable / groupby (1:1 with upstream).
    assert all(c.filterable and c.groupby for c in cols)


def test_query_columns_are_transient_and_async_safe() -> None:
    """Synthesized TableColumns must not trigger a lazy-load on access.

    The port's ``TableColumn.database`` is a derived property reading
    ``self.table``; for a transient column ``table`` is ``None`` so
    ``database`` returns ``None`` without a sync SELECT (which would raise
    MissingGreenlet under asyncpg).
    """
    col = _make_query().columns[0]
    # Not attached to any session.
    from sqlalchemy import inspect as sa_inspect

    assert sa_inspect(col).transient is True
    # No lazy-load / no exception.
    assert col.table is None
    assert col.database is None


def test_query_column_helpers() -> None:
    q = _make_query()
    assert q.column_names == ["gender", "ds", "num"]
    assert q.get_column("num").column_name == "num"
    assert q.get_column("nope") is None
    assert q.get_column(None) is None
    assert q.dttm_cols == ["ds"]
    assert q.main_dttm_col == "ds"


def test_query_main_dttm_col_none_when_no_temporal() -> None:
    extra = {"columns": [{"column_name": "g", "type": "STRING", "is_dttm": False}]}
    q = Query(id=1, client_id="c", database_id=1, extra_json=json.dumps(extra))
    assert q.main_dttm_col is None
    assert q.dttm_cols == []


def test_query_datasource_scalar_props() -> None:
    q = _make_query()
    assert q.uid == "123__query"
    assert q.is_rls_supported is False
    assert q.cache_timeout == 0
    assert q.offset == 0
    assert q.default_endpoint == ""
    assert q.db_extra is None
    assert q.get_extra_cache_keys({}) == []
    assert q.owners_data == []


def test_query_perm_strings() -> None:
    class _DB:
        database_name = "examples"

    q = _make_query()
    q.database = _DB()  # type: ignore[assignment]
    assert q.schema_perm == "examples.public"
    assert q.perm == "[examples].[my tab](id:123)"


def test_query_columns_tolerate_missing_keys() -> None:
    # A query whose stored extra predates a column-metadata field must not 500.
    extra = {"columns": [{"column_name": "only_name"}]}
    q = Query(id=2, client_id="c2", database_id=1, extra_json=json.dumps(extra))
    cols = q.columns
    assert len(cols) == 1
    assert cols[0].column_name == "only_name"
    assert cols[0].is_dttm is False
    assert cols[0].type is None


def test_query_empty_extra_yields_no_columns() -> None:
    q = Query(id=3, client_id="c3", database_id=1, extra_json="{}")
    assert q.columns == []
    assert q.column_names == []
    assert q.main_dttm_col is None
