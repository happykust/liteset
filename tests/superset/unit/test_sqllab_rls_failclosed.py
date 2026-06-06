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
raw, unfiltered SQL. This mirrors the original Flask
``superset_old/sql_lab.py::execute_sql_statements`` where the ``apply_rls``
loop runs unguarded.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest

from superset.tasks import sql_lab as sql_lab_task


class _BoomRLS(Exception):
    """Sentinel raised by the mocked ``apply_rls``."""


def _make_query() -> SimpleNamespace:
    database = SimpleNamespace(
        allow_dml=True,
        allow_run_async=False,
        db_engine_spec=SimpleNamespace(
            allows_sql_comments=True,
            run_multiple_statements_as_one=False,
        ),
        get_default_schema_for_query=lambda query: "public",
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
        mock.patch.object(sql_lab_task, "_resolve_results_backend", return_value=(None, False)),
        mock.patch.object(sql_lab_task, "_resolve_disallowed_functions", return_value=set()),
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
