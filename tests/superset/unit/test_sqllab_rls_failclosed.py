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
"""RLS-in-SQL-Lab must fail closed.

CRITICAL security regression test. When ``RLS_IN_SQLLAB`` is enabled and the
row-level-security rewrite (:func:`superset.utils.rls.apply_rls`) raises while
applying filters to the user's SQL, the query MUST fail (the exception must
propagate) — it must NOT swallow the error and fall through to executing the
raw, unfiltered SQL. The ``apply_rls`` loop runs unguarded.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest

from superset.tasks import sql_lab as sql_lab_task


class _BoomRLS(Exception):  # noqa: N818
    """Sentinel raised by the mocked ``apply_rls``."""


def _make_query(default_schema: str | None = "public") -> SimpleNamespace:
    database = SimpleNamespace(
        allow_dml=True,
        allow_run_async=False,
        db_engine_spec=SimpleNamespace(
            allows_sql_comments=True,
            run_multiple_statements_as_one=False,
        ),
        get_default_schema_for_query=lambda query: default_schema,
    )
    return SimpleNamespace(
        id=1,
        database=database,
        catalog=None,
        status=None,
        start_running_time=None,
        select_as_cta=False,
    )


def test_apply_rls_failure_propagates_and_blocks_execution() -> None:
    """apply_rls raising must propagate; raw SQL must never be executed."""
    query = _make_query()
    session = mock.MagicMock()

    with (
        mock.patch.object(sql_lab_task, "_get_session", return_value=session),
        mock.patch.object(sql_lab_task, "_get_query", return_value=query),
        mock.patch.object(
            sql_lab_task, "_resolve_results_backend", return_value=(None, False)
        ),
        mock.patch.object(
            sql_lab_task, "_resolve_disallowed_functions", return_value=set()
        ),
        # Feature flag ON so the RLS block is exercised.
        mock.patch.object(
            sql_lab_task,
            "_is_feature_enabled",
            side_effect=lambda name: name == "RLS_IN_SQLLAB",
        ),
        # apply_rls blows up while rewriting the statement.
        mock.patch(
            "superset.utils.rls.apply_rls", side_effect=_BoomRLS("rls rewrite failed")
        ) as apply_rls_mock,
        # If execution were ever reached, this would record it.
        mock.patch.object(sql_lab_task, "_execute_query") as execute_mock,
    ):
        with pytest.raises(_BoomRLS):
            sql_lab_task.execute_sql_statements(
                query_id=1,
                rendered_query="SELECT * FROM secret_table",
                return_results=True,
                store_results=False,
                start_time=None,
                expand_data=False,
                log_params=None,
                username=None,
            )

    # RLS was attempted ...
    assert apply_rls_mock.called
    # ... and the raw user SQL was NEVER executed (fail-closed).
    execute_mock.assert_not_called()
    # session is always closed in the finally block.
    session.close.assert_called_once()


def test_apply_rls_receives_none_default_schema_unchanged() -> None:
    """A ``None`` default schema must reach ``apply_rls`` as ``None``, not ``""``.

    ``get_default_schema_for_query`` is ``str | None``; engines without a
    default schema return ``None``. ``apply_rls`` -> ``Table.qualify`` resolves
    an unqualified table's schema to that value, and the dataset lookup is
    ``SqlaTable.schema == table.schema``. ``None`` renders as ``IS NULL`` and
    matches a dataset stored with ``schema IS NULL`` (the common case for
    schemaless engines); ``""`` does not, so coercing ``None`` -> ``""`` would
    silently skip RLS injection — an RLS bypass. The 3rd positional arg to
    ``apply_rls`` must therefore be the unmodified ``None``.
    """
    query = _make_query(default_schema=None)
    session = mock.MagicMock()

    with (
        mock.patch.object(sql_lab_task, "_get_session", return_value=session),
        mock.patch.object(sql_lab_task, "_get_query", return_value=query),
        mock.patch.object(
            sql_lab_task, "_resolve_results_backend", return_value=(None, False)
        ),
        mock.patch.object(
            sql_lab_task, "_resolve_disallowed_functions", return_value=set()
        ),
        mock.patch.object(
            sql_lab_task,
            "_is_feature_enabled",
            side_effect=lambda name: name == "RLS_IN_SQLLAB",
        ),
        # Record the call, then abort early so we don't drive the executor.
        mock.patch(
            "superset.utils.rls.apply_rls", side_effect=_BoomRLS("stop after record")
        ) as apply_rls_mock,
        mock.patch.object(sql_lab_task, "_execute_query"),
    ):
        with pytest.raises(_BoomRLS):
            sql_lab_task.execute_sql_statements(
                query_id=1,
                rendered_query="SELECT * FROM secret_table",
                return_results=True,
                store_results=False,
                start_time=None,
                expand_data=False,
                log_params=None,
                username=None,
            )

    assert apply_rls_mock.called
    # signature: apply_rls(database, catalog, default_schema, statement)
    default_schema_arg = apply_rls_mock.call_args.args[2]
    assert default_schema_arg is None, (
        f"default_schema coerced to {default_schema_arg!r}; "
        "an empty string misses NULL-schema datasets and bypasses RLS"
    )
