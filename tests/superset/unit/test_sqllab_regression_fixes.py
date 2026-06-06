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
"""Focused regression tests for the SQL-Lab Flask->Litestar port fixes.

Covers:
* ``AsyncQueryDAO.stop_query`` raises ``SupersetCancelQueryException`` on a
  failed cancel and only sets STOPPED on success (finding 4).
* ``PRESTO_EXPAND_DATA`` gating of ``expand_data`` (finding 3).
* CTAS ``tmp_schema_name`` computation (finding 2).
* ``DISPLAY_MAX_ROW`` cap on the sync execute payload (finding 6).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from superset.common.query_status import QueryStatus
from superset.exceptions import SupersetCancelQueryException


# ---------------------------------------------------------------------------
# Finding 4 — stop_query raises on cancel-failure, only STOPPED on success
# ---------------------------------------------------------------------------


def _make_stop_query_dao(query):
    """Build an ``AsyncQueryDAO`` whose ``find_one_or_none`` returns *query*."""
    from superset.db.daos.query import AsyncQueryDAO

    dao = AsyncQueryDAO(session=MagicMock())

    async def _find_one_or_none(**kwargs):
        return query

    dao.find_one_or_none = _find_one_or_none  # type: ignore[assignment]
    return dao


@pytest.mark.asyncio
async def test_stop_query_raises_when_cancel_fails() -> None:
    query = SimpleNamespace(
        status=QueryStatus.RUNNING,
        end_time=None,
        database=MagicMock(),
    )
    dao = _make_stop_query_dao(query)

    with patch("superset.tasks.sql_lab.cancel_query", return_value=False):
        with pytest.raises(SupersetCancelQueryException):
            await dao.stop_query("client-1")

    # Must NOT be marked STOPPED when the cancel failed.
    assert query.status == QueryStatus.RUNNING
    assert query.end_time is None


@pytest.mark.asyncio
async def test_stop_query_sets_stopped_on_successful_cancel() -> None:
    query = SimpleNamespace(
        status=QueryStatus.RUNNING,
        end_time=None,
        database=MagicMock(),
    )
    dao = _make_stop_query_dao(query)

    with patch("superset.tasks.sql_lab.cancel_query", return_value=True):
        result = await dao.stop_query("client-1")

    assert result is query
    assert query.status == QueryStatus.STOPPED
    assert query.end_time is not None


@pytest.mark.asyncio
async def test_stop_query_skips_terminal_states() -> None:
    query = SimpleNamespace(
        status=QueryStatus.SUCCESS,
        end_time=123.0,
        database=MagicMock(),
    )
    dao = _make_stop_query_dao(query)

    # cancel_query must not even be called for terminal states.
    with patch("superset.tasks.sql_lab.cancel_query") as cancel:
        result = await dao.stop_query("client-1")
        cancel.assert_not_called()

    assert result is query
    assert query.status == QueryStatus.SUCCESS


# ---------------------------------------------------------------------------
# Finding 3 — PRESTO_EXPAND_DATA gating + falsy default
# ---------------------------------------------------------------------------


def _make_execute_command(expand_data: bool):
    from superset.commands.sqllab.execute import ExecuteSQLCommand

    return ExecuteSQLCommand(
        dao=MagicMock(),
        database_id=1,
        sql="SELECT 1",
        expand_data=expand_data,
    )


def test_expand_data_disabled_when_flag_off() -> None:
    with patch(
        "superset.commands.sqllab.execute.ExecuteSQLCommand._is_feature_enabled",
        return_value=False,
    ):
        cmd = _make_execute_command(expand_data=True)
    assert cmd._expand_data is False


def test_expand_data_enabled_only_when_flag_and_param() -> None:
    with patch(
        "superset.commands.sqllab.execute.ExecuteSQLCommand._is_feature_enabled",
        return_value=True,
    ):
        on = _make_execute_command(expand_data=True)
        off = _make_execute_command(expand_data=False)
    assert on._expand_data is True
    assert off._expand_data is False


def test_execute_payload_schema_expand_data_default_falsy() -> None:
    from superset.schemas.sqllab import ExecutePayloadSchema

    body = ExecutePayloadSchema(database_id=1, sql="SELECT 1")
    assert body.expand_data is False


# ---------------------------------------------------------------------------
# Finding 2 — CTAS tmp_schema_name resolution
# ---------------------------------------------------------------------------


def test_ctas_target_schema_prefers_force_ctas_schema() -> None:
    cmd = _make_execute_command(expand_data=False)
    database = SimpleNamespace(force_ctas_schema="forced_schema")
    assert cmd._get_ctas_target_schema_name(database) == "forced_schema"


def test_ctas_target_schema_uses_config_func() -> None:
    cmd = _make_execute_command(expand_data=False)
    cmd._schema = "myschema"
    cmd._sql = "SELECT 1"
    cmd._current_user = SimpleNamespace(username="bob")
    database = SimpleNamespace(force_ctas_schema=None)

    func = MagicMock(return_value="computed_schema")
    settings = MagicMock(sqllab_ctas_schema_name_func=func)
    with patch("superset.config.SupersetSettings", return_value=settings):
        result = cmd._get_ctas_target_schema_name(database)

    assert result == "computed_schema"
    func.assert_called_once_with(database, cmd._current_user, "myschema", "SELECT 1")


def test_ctas_target_schema_none_when_no_func() -> None:
    cmd = _make_execute_command(expand_data=False)
    database = SimpleNamespace(force_ctas_schema=None)
    settings = MagicMock(sqllab_ctas_schema_name_func=None)
    with patch("superset.config.SupersetSettings", return_value=settings):
        assert cmd._get_ctas_target_schema_name(database) is None


# ---------------------------------------------------------------------------
# Finding 6 — DISPLAY_MAX_ROW cap on the sync execute payload
# ---------------------------------------------------------------------------


def test_display_max_row_caps_rows_and_flags() -> None:
    cmd = _make_execute_command(expand_data=False)
    payload = {
        "status": QueryStatus.SUCCESS,
        "data": list(range(5)),
        "query": {"rows": 5},
    }
    settings = MagicMock(display_max_row=2)
    with patch("superset.config.SupersetSettings", return_value=settings):
        cmd._apply_display_max_row(payload)

    assert payload["data"] == [0, 1]
    assert payload["displayLimitReached"] is True


def test_display_max_row_no_cap_when_under_limit() -> None:
    cmd = _make_execute_command(expand_data=False)
    payload = {
        "status": QueryStatus.SUCCESS,
        "data": [0, 1],
        "query": {"rows": 2},
    }
    settings = MagicMock(display_max_row=10)
    with patch("superset.config.SupersetSettings", return_value=settings):
        cmd._apply_display_max_row(payload)

    assert payload["data"] == [0, 1]
    assert "displayLimitReached" not in payload


def test_display_max_row_skips_non_success() -> None:
    cmd = _make_execute_command(expand_data=False)
    payload = {
        "status": QueryStatus.FAILED,
        "data": list(range(5)),
        "query": {"rows": 5},
    }
    settings = MagicMock(display_max_row=2)
    with patch("superset.config.SupersetSettings", return_value=settings):
        cmd._apply_display_max_row(payload)

    assert payload["data"] == list(range(5))
    assert "displayLimitReached" not in payload
