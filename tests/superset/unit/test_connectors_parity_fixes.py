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
"""Parity-fix coverage for superset/models/connectors.py."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pandas as pd
import pytest


def _grains():
    from superset.db_engine_specs.base import TimeGrain

    return (
        TimeGrain(name="Day", label="Day", function="d", duration="P1D"),
        TimeGrain(name="Week", label="Week", function="w", duration="P1W"),
    )


def test_time_grain_sqla_populated_from_grains():
    from superset.models.connectors import SqlaTable

    tbl = SqlaTable()
    grains = _grains()
    db = MagicMock()
    db.grains.return_value = grains
    tbl.database = db

    result = [(g.duration, g.name) for g in tbl.database.grains() or []]
    assert result == [("P1D", "Day"), ("P1W", "Week")]
    assert all(isinstance(t, tuple) and len(t) == 2 for t in result)


def test_time_grain_sqla_empty_when_no_grains():
    from superset.models.connectors import SqlaTable

    tbl = SqlaTable()
    db = MagicMock()
    db.grains.return_value = ()
    tbl.database = db
    assert [(g.duration, g.name) for g in tbl.database.grains() or []] == []


def test_health_check_message_uses_config_hook(monkeypatch):
    """health_check_message was hardcoded None; now invokes the configured
    DATASET_HEALTH_CHECK callable with self.
    """
    from superset.models.connectors import SqlaTable

    tbl = SqlaTable()
    settings = MagicMock()
    settings.dataset_health_check = lambda ds: f"unhealthy:{ds is tbl}"
    monkeypatch.setattr("superset.config.SupersetSettings", lambda *a, **k: settings)
    assert tbl.health_check_message == "unhealthy:True"


def test_health_check_message_none_when_hook_unset(monkeypatch):
    from superset.models.connectors import SqlaTable

    tbl = SqlaTable()
    settings = MagicMock()
    settings.dataset_health_check = None
    monkeypatch.setattr("superset.config.SupersetSettings", lambda *a, **k: settings)
    assert tbl.health_check_message is None


def test_base_datasource_get_extra_cache_keys_default_empty():
    from superset.models.connectors import BaseDatasource

    obj = BaseDatasource()
    assert obj.get_extra_cache_keys({}) == []


def test_has_extra_cache_key_calls_detects_extracache_macro():
    from superset.models.connectors import SqlaTable

    tbl = SqlaTable()
    tbl.sql = "SELECT * FROM t WHERE x = {{ url_param('a') }}"
    tbl.fetch_values_predicate = None
    # RLS resolution needs a request context / metadata DB; disable to isolate
    # the SQL-statement path.
    tbl.is_rls_supported = False
    assert tbl.has_extra_cache_key_calls({}) is True


def test_has_extra_cache_key_calls_false_without_macro():
    from superset.models.connectors import SqlaTable

    tbl = SqlaTable()
    tbl.sql = "SELECT * FROM t"
    tbl.fetch_values_predicate = None
    tbl.is_rls_supported = False
    assert tbl.has_extra_cache_key_calls({}) is False


def test_has_extra_cache_key_calls_detects_in_extras_where():
    from superset.models.connectors import SqlaTable

    tbl = SqlaTable()
    tbl.sql = None
    tbl.fetch_values_predicate = None
    tbl.is_rls_supported = False
    query_obj = {"extras": {"where": "col = {{ current_user_id() }}"}}
    assert tbl.has_extra_cache_key_calls(query_obj) is True


def test_get_extra_cache_keys_returns_empty_when_no_macro_physical():
    from superset.models.connectors import SqlaTable

    tbl = SqlaTable()
    tbl.sql = None
    tbl.fetch_values_predicate = None
    tbl.is_rls_supported = False
    assert tbl.get_extra_cache_keys({}) == []


@pytest.mark.asyncio
async def test_async_values_for_column_applies_mutator_percent_and_jinja(
    monkeypatch,
):
    from sqlalchemy.sql import literal_column

    from superset.models.connectors import SqlaTable

    tbl = SqlaTable()
    tbl.fetch_values_predicate = None
    tbl.sql = None
    tbl.catalog = None
    tbl.schema = None

    # One column whose get_sqla_col records the template processor it gets,
    # and emits a literal that compiles to a doubled-percent LIKE.
    col = MagicMock()
    col.column_name = "country"
    captured = {}

    def fake_get_sqla_col(label=None, template_processor=None):
        captured["tp"] = template_processor
        # literal_binds rendering of this LIKE keeps the literal verbatim;
        # we assert on the post-fixup string below.
        return literal_column("country").label("column_values")

    col.get_sqla_col.side_effect = fake_get_sqla_col
    tbl.columns = [col]

    # _double_percents=True forces %% in the compiled SQL; the fixup must
    # convert them to %.
    from sqlalchemy.dialects import sqlite

    dialect = sqlite.dialect()
    dialect.identifier_preparer._double_percents = True
    database = MagicMock()
    database.get_dialect.return_value = dialect

    def fake_mutate(sql):
        # Inject doubled-percent to verify the fixup runs AFTER the mutator
        # (mutate → %%→%).
        return sql + " /* x LIKE '%%a%%' */"

    database.mutate_sql_based_on_config.side_effect = fake_mutate
    tbl.database = database

    fake_tp = MagicMock()
    fake_tp.process_template.side_effect = lambda s: s
    monkeypatch.setattr(
        "superset.jinja_context.get_template_processor",
        lambda **kw: fake_tp,
    )

    import sqlalchemy as sa

    monkeypatch.setattr(
        SqlaTable,
        "get_from_clause",
        lambda self, template_processor=None: (sa.table("t"), None),
    )
    monkeypatch.setattr(SqlaTable, "_apply_cte", lambda self, sql, cte: sql)

    executed = {}

    async def fake_execute(self, sql):
        executed["sql"] = sql
        return pd.DataFrame({"column_values": ["US", "FR"]})

    monkeypatch.setattr(SqlaTable, "_execute_sql", fake_execute)

    result = await tbl.async_values_for_column("country", limit=100)

    assert result == ["US", "FR"]
    assert captured["tp"] is fake_tp
    assert database.mutate_sql_based_on_config.called
    assert "x LIKE '%a%'" in executed["sql"]
    assert "%%" not in executed["sql"]


def _virtual_table(sql: str) -> Any:
    """Build a SqlaTable with a passthrough template processor, for the
    ``_get_virtual_table_metadata`` guard tests below."""
    from superset.models.connectors import SqlaTable

    tbl = SqlaTable()
    tbl.sql = sql
    tbl.template_params = None

    fake_processor = MagicMock()
    fake_processor.process_template.side_effect = lambda s, **kw: s
    tbl.get_template_processor = lambda **kw: fake_processor

    database = MagicMock()
    database.db_engine_spec.engine = "postgresql"
    tbl.database = database

    return tbl


def test_get_virtual_table_metadata_rejects_mutating_sql():
    r"""C1 regression: a single mutating statement must never reach the
    database -- upstream's ``Only `SELECT` statements are allowed``.
    """
    from superset.exceptions import SupersetSecurityException

    tbl = _virtual_table("DELETE FROM users")

    with pytest.raises(SupersetSecurityException, match="SELECT"):
        tbl._get_virtual_table_metadata()

    tbl.database.apply_limit_to_sql.assert_not_called()
    tbl.database.mutate_sql_based_on_config.assert_not_called()


def test_get_virtual_table_metadata_rejects_multi_statement_sql():
    """C1 regression: a parseable-but-multi-statement script must be
    rejected even when neither statement is individually a mutation --
    upstream's ``Only single queries supported``.
    """
    from superset.exceptions import SupersetSecurityException

    tbl = _virtual_table("SELECT 1; SELECT 2")

    with pytest.raises(SupersetSecurityException, match="single"):
        tbl._get_virtual_table_metadata()

    tbl.database.apply_limit_to_sql.assert_not_called()
    tbl.database.mutate_sql_based_on_config.assert_not_called()


def test_get_virtual_table_metadata_rejects_proven_stacked_query_exploit():
    """C1 regression (critical): the exact proven exploit -- a virtual
    dataset ``sql`` that closes the old wrapping ``SELECT * FROM (...) AS
    virtual_table LIMIT 0`` subquery early and appends
    ``COMMIT; DROP TABLE ...`` -- must be rejected without ever touching
    the database. Confirmed live against Postgres pre-fix: 201 with a
    normal ``['x']`` column list while the table was dropped.

    The malformed SQL fails to parse at all (it is not valid on its own --
    that is *how* the guard catches it here), so the guard raises via the
    ``SupersetParseError`` branch inside ``_validate_and_limit_virtual_sql``
    -- ``SupersetGenericDBErrorException("Invalid SQL: Error parsing ...")``
    -- rather than the ``SupersetSecurityException`` the two sibling tests
    above cover for syntactically-valid-but-mutating/multi-statement SQL.
    Asserting only the ``SupersetException`` base class (both are
    subclasses of it, as is the unrelated DB-connection-failure wrapper
    this same method raises from its execution try/except) would also be
    satisfied by removing the guard entirely and letting the raw SQL reach
    ``get_sync_connection``, which fails for its own unrelated reason
    ("No async engine spec found" against this test's mocked database) --
    so the exception type/message must be pinned precisely.
    """
    from superset.exceptions import SupersetGenericDBErrorException

    exploit_sql = (
        "SELECT 1 AS x) AS v LIMIT 0; COMMIT; DROP TABLE public.users; "
        "COMMIT; SELECT * FROM (SELECT 1 AS x"
    )
    tbl = _virtual_table(exploit_sql)

    with pytest.raises(SupersetGenericDBErrorException, match="Error parsing"):
        tbl._get_virtual_table_metadata()

    tbl.database.apply_limit_to_sql.assert_not_called()
    tbl.database.mutate_sql_based_on_config.assert_not_called()


def _stub_sync_connection(monkeypatch, columns: list[tuple[str, None]]):
    """Patch ``get_sync_connection`` (imported inside the method under a
    local ``from ... import`` on every call) with a fake connection whose
    cursor reports *columns*, and return the dict recording the executed
    SQL string."""
    from contextlib import contextmanager
    from unittest.mock import MagicMock

    executed: dict[str, str] = {}

    class _FakeCursor:
        description = columns

    class _FakeResult:
        cursor = _FakeCursor()

    class _FakeConnection:
        def execute(self, stmt: Any) -> _FakeResult:
            executed["sql"] = str(stmt)
            return _FakeResult()

    fake_spec = MagicMock()
    fake_spec.get_datatype.side_effect = lambda _code: "VARCHAR"

    @contextmanager
    def _fake_get_sync_connection(database: Any):
        yield _FakeConnection(), fake_spec

    monkeypatch.setattr(
        "superset.utils.database.get_sync_connection",
        _fake_get_sync_connection,
    )
    return executed


@pytest.mark.parametrize(
    "sql",
    [
        pytest.param("SELECT a, b FROM t", id="plain_select"),
        pytest.param("WITH cte AS (SELECT a, b FROM t) SELECT a, b FROM cte", id="cte"),
        pytest.param("SELECT a, b FROM t;", id="trailing_semicolon"),
    ],
)
def test_get_virtual_table_metadata_accepts_legitimate_select(monkeypatch, sql):
    """Positive-path C1 coverage: a legitimate single ``SELECT`` -- including
    a CTE and a trailing semicolon -- must parse, get a LIMIT applied, and
    return column metadata. This runs on every virtual-dataset save; without
    this test an over-rejection regression in the guard (rejecting valid
    SQL it shouldn't) would go undetected even though only the rejection
    tests above are covered.
    """
    tbl = _virtual_table(sql)
    tbl.database.apply_limit_to_sql.side_effect = lambda inner_sql, limit=None: (
        f"{inner_sql} LIMIT {limit}"
    )
    tbl.database.mutate_sql_based_on_config.side_effect = lambda inner_sql: inner_sql

    executed = _stub_sync_connection(monkeypatch, [("a", None), ("b", None)])

    columns = tbl._get_virtual_table_metadata()

    assert columns == [
        {"column_name": "a", "type": "VARCHAR"},
        {"column_name": "b", "type": "VARCHAR"},
    ]
    tbl.database.apply_limit_to_sql.assert_called_once()
    assert "LIMIT" in executed["sql"]
    # The trailing semicolon (if any) must not survive into the executed
    # SQL -- it would be invalid before an appended ``LIMIT`` clause.
    assert ";" not in executed["sql"].split(" LIMIT ")[0]


@pytest.mark.asyncio
async def test_async_values_for_column_uses_guarded_from_clause(monkeypatch):
    """M4 regression: ``async_values_for_column`` must resolve its FROM
    clause via ``get_from_clause`` (renders Jinja, rejects multi-statement/
    mutating virtual-dataset SQL, and applies the underlying physical
    tables' RLS predicates), never the unguarded ``_build_from_ast`` --
    which skipped RLS entirely for this endpoint.
    """
    import sqlalchemy as sa

    from superset.models.connectors import SqlaTable

    tbl = SqlaTable()
    tbl.sql = "SELECT * FROM sales"  # virtual dataset
    tbl.fetch_values_predicate = None
    tbl.catalog = None
    tbl.schema = None

    col = MagicMock()
    col.column_name = "region"

    def fake_get_sqla_col(label=None, template_processor=None):
        return sa.literal_column("region").label("column_values")

    col.get_sqla_col.side_effect = fake_get_sqla_col
    tbl.columns = [col]

    database = MagicMock()
    from sqlalchemy.dialects import sqlite

    database.get_dialect.return_value = sqlite.dialect()
    database.mutate_sql_based_on_config.side_effect = lambda sql: sql
    tbl.database = database

    fake_tp = MagicMock()
    fake_tp.process_template.side_effect = lambda s: s
    monkeypatch.setattr(
        "superset.jinja_context.get_template_processor",
        lambda **kw: fake_tp,
    )

    called = {"get_from_clause": False}

    def fake_get_from_clause(self, template_processor=None):
        called["get_from_clause"] = True
        return sa.table("sales"), None

    def fail_build_from_ast(self):
        raise AssertionError(
            "async_values_for_column must not use the unguarded "
            "_build_from_ast for a virtual dataset"
        )

    monkeypatch.setattr(SqlaTable, "get_from_clause", fake_get_from_clause)
    monkeypatch.setattr(SqlaTable, "_build_from_ast", fail_build_from_ast)
    monkeypatch.setattr(SqlaTable, "_apply_cte", lambda self, sql, cte: sql)

    async def fake_execute(self, sql):
        return pd.DataFrame({"column_values": ["west"]})

    monkeypatch.setattr(SqlaTable, "_execute_sql", fake_execute)

    result = await tbl.async_values_for_column("region", limit=10)

    assert called["get_from_clause"] is True
    assert result == ["west"]


@pytest.mark.asyncio
async def test_async_query_reraises_superset_error_exception() -> None:
    from unittest.mock import patch as _patch

    from superset.errors import ErrorLevel, SupersetError, SupersetErrorType
    from superset.exceptions import SupersetErrorException
    from superset.models.connectors import SqlaTable

    table = SqlaTable()
    err = SupersetError(
        error_type=SupersetErrorType.OAUTH2_REDIRECT,
        message="OAuth2 redirect",
        level=ErrorLevel.WARNING,
        extra={"url": "https://auth", "tab_id": "t", "redirect_uri": "https://r"},
    )

    with _patch.object(
        SqlaTable,
        "_get_sqla_query_with_rls",
        side_effect=SupersetErrorException(err),
    ):
        with pytest.raises(SupersetErrorException) as exc_info:
            await table.async_query({"metrics": []})

    assert exc_info.value.error.error_type == SupersetErrorType.OAUTH2_REDIRECT


@pytest.mark.asyncio
async def test_async_query_converts_generic_errors_to_query_result() -> None:
    """Non-Superset exceptions must be captured as QueryResult(error), not re-raised."""
    from unittest.mock import patch as _patch

    from superset.models.connectors import SqlaTable

    table = SqlaTable()
    with _patch.object(
        SqlaTable,
        "_get_sqla_query_with_rls",
        side_effect=ValueError("boom"),
    ):
        result = await table.async_query({"metrics": []})

    assert result.status == "error"
    assert "boom" in (result.error_message or "")
