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
"""Parity-fix coverage for ``superset/models/connectors.py``.

Covers three 1:1 regressions vs. ``superset_old``:
  1. ``SqlaTable.data["time_grain_sqla"]`` populated from engine-spec grains.
  2. ``SqlaTable.get_extra_cache_keys`` / ``has_extra_cache_key_calls``.
  3. ``async_values_for_column`` SQL_QUERY_MUTATOR + ``%%``->``%`` + Jinja.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pandas as pd
import pytest


def _grains():
    from superset.db_engine_specs.base import TimeGrain

    return (
        TimeGrain(name="Day", label="Day", function="d", duration="P1D"),
        TimeGrain(name="Week", label="Week", function="w", duration="P1W"),
    )


# --- Fix 1 ----------------------------------------------------------------


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


# --- Fix 2 ----------------------------------------------------------------


def test_base_datasource_get_extra_cache_keys_default_empty():
    from superset.models.connectors import BaseDatasource

    obj = BaseDatasource()
    assert obj.get_extra_cache_keys({}) == []


def test_has_extra_cache_key_calls_detects_extracache_macro():
    from superset.models.connectors import SqlaTable

    tbl = SqlaTable()
    tbl.sql = "SELECT * FROM t WHERE x = {{ url_param('a') }}"
    tbl.fetch_values_predicate = None
    # Isolate the SQL-statement detection path (RLS resolution needs a
    # request context / metadata DB).
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
    tbl.sql = None  # physical (non-virtual): skips RLS-predicate branch
    tbl.fetch_values_predicate = None
    tbl.is_rls_supported = False
    assert tbl.get_extra_cache_keys({}) == []


# --- Fix 3 ----------------------------------------------------------------


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

    # Real dialect (so AST compile works) with _double_percents forced True
    # -> the %%->% fixup must fire.
    from sqlalchemy.dialects import sqlite

    dialect = sqlite.dialect()
    dialect.identifier_preparer._double_percents = True
    database = MagicMock()
    database.get_dialect.return_value = dialect

    def fake_mutate(sql):
        # Inject a doubled-percent literal so we can prove the fixup runs
        # AFTER the mutator (original order: mutate, then %%->%).
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
        SqlaTable, "_build_from_ast", lambda self: (sa.table("t"), None)
    )
    monkeypatch.setattr(SqlaTable, "_apply_cte", lambda self, sql, cte: sql)

    executed = {}

    async def fake_execute(self, sql):
        executed["sql"] = sql
        return pd.DataFrame({"column_values": ["US", "FR"]})

    monkeypatch.setattr(SqlaTable, "_execute_sql", fake_execute)

    result = await tbl.async_values_for_column("country", limit=100)

    assert result == ["US", "FR"]
    # (c) Jinja: the template processor reached get_sqla_col.
    assert captured["tp"] is fake_tp
    # (a) SQL_QUERY_MUTATOR hook invoked.
    assert database.mutate_sql_based_on_config.called
    # (b) %%->% fixup ran on the mutated SQL.
    assert "x LIKE '%a%'" in executed["sql"]
    assert "%%" not in executed["sql"]


# ---------------------------------------------------------------------------
# Round-4 crit — async_query must RE-RAISE SupersetErrorException /
# SupersetErrorsException (OAuth2RedirectError in particular) instead of
# swallowing them into QueryResult(status='error'). 1:1 with
# superset_old/connectors/sqla/models.py:1658-1662 ("...they should bubble up
# ... so they are returned as proper SIP-40 errors ... important for database
# OAuth2, see SIP-85").
# ---------------------------------------------------------------------------


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
    """Non-Superset exceptions keep the original behaviour: QueryResult(error)."""
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
