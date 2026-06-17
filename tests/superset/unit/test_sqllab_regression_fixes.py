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
* HTTP-202-vs-200 status selection: idempotency re-submission must be 200,
  fresh Celery dispatch must be 202 (round-3 medium finding).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock as _AsyncMock, MagicMock, patch

import pytest

from superset.commands.sqllab.execute import ExecuteSQLCommand
from superset.common.query_status import QueryStatus
from superset.exceptions import SupersetCancelQueryException

# ---------------------------------------------------------------------------
# Finding 4 — stop_query raises on cancel-failure, only STOPPED on success
# ---------------------------------------------------------------------------


def _make_stop_query_dao(query):
    """Build an ``AsyncQueryDAO`` whose lookup returns *query*.

    ``stop_query`` loads via ``session.execute`` with an explicit
    ``selectinload(Query.database)`` (not ``find_one_or_none``) so the
    relationship is usable inside the ``to_thread`` worker.
    """
    from superset.db.daos.query import AsyncQueryDAO

    session = MagicMock()
    res = MagicMock()
    res.scalars.return_value.one_or_none.return_value = query
    session.execute = _AsyncMock(return_value=res)
    return AsyncQueryDAO(session=session)


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


# ---------------------------------------------------------------------------
# Round-3 medium finding: HTTP 202 vs 200 for execute endpoint
#
# Original ``sqllab/api.py:409-412``:
#   response_status = 202 if status == QUERY_IS_RUNNING else 200
#
# 202 must ONLY be returned for a freshly-dispatched Celery job
# (QUERY_IS_RUNNING = 3).  Re-submitted queries with the same client_id
# that already exist (QUERY_ALREADY_CREATED = 1) must return 200 even when
# their DB status is "running" or "pending".
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idempotency_path_sets_query_already_created_running() -> None:
    """Existing RUNNING query returns query_already_created=True in the dict."""
    existing = SimpleNamespace(
        status=QueryStatus.RUNNING,
        to_dict=lambda: {"id": 1, "state": "running"},
        id=1,
    )
    dao = _AsyncMock()
    dao.find_one_or_none = _AsyncMock(return_value=existing)

    cmd = ExecuteSQLCommand(
        dao=dao,
        database_id=1,
        sql="SELECT 1",
        client_id="dup-client-id",
        user_id=42,
    )
    result = await cmd.run()

    assert result.get("query_already_created") is True, (
        "Idempotency path must mark response with query_already_created=True "
        "so the controller returns HTTP 200, not 202."
    )


@pytest.mark.asyncio
async def test_idempotency_path_sets_query_already_created_pending() -> None:
    """Existing PENDING query also gets query_already_created=True."""
    existing = SimpleNamespace(
        status=QueryStatus.PENDING,
        to_dict=lambda: {"id": 2, "state": "pending"},
        id=2,
    )
    dao = _AsyncMock()
    dao.find_one_or_none = _AsyncMock(return_value=existing)

    cmd = ExecuteSQLCommand(
        dao=dao,
        database_id=1,
        sql="SELECT 1",
        client_id="dup-pending-id",
        user_id=42,
    )
    result = await cmd.run()

    assert result.get("query_already_created") is True


def _http_status_from_result(result_dict: dict) -> int:
    """Replicate the controller's status-code selection logic.

    Mirrors ``superset/controllers/sqllab.py`` execute handler:
    - pop ``query_already_created`` sentinel
    - 202 only when it is a fresh Celery async dispatch (status="running",
      no ``query_already_created`` flag)
    - 200 for everything else (sync success, re-submission, failure, …)
    """
    query_already_created = bool(result_dict.pop("query_already_created", False))
    status_str = result_dict.get("status")
    is_async_dispatch = not query_already_created and status_str in {
        "running",
        QueryStatus.RUNNING,
    }
    return 202 if is_async_dispatch else 200


def test_execute_status_code_idempotency_running_is_200() -> None:
    """Re-submitted RUNNING query → HTTP 200, not 202.

    QUERY_ALREADY_CREATED != QUERY_IS_RUNNING → 200.
    """
    result = {"status": "running", "query_already_created": True}
    assert _http_status_from_result(result) == 200


def test_execute_status_code_idempotency_pending_is_200() -> None:
    """Re-submitted PENDING query → HTTP 200."""
    result = {"status": "pending", "query_already_created": True}
    assert _http_status_from_result(result) == 200


def test_execute_status_code_fresh_celery_dispatch_is_202() -> None:
    """Fresh Celery job (no query_already_created flag, status=running) → HTTP 202.

    QUERY_IS_RUNNING → 202.
    """
    result = {"status": "running"}
    assert _http_status_from_result(result) == 202


def test_execute_status_code_sync_success_is_200() -> None:
    """Sync query success → HTTP 200."""
    result = {"status": "success"}
    assert _http_status_from_result(result) == 200


def test_execute_status_code_query_already_created_not_in_serialized_payload() -> None:
    """The sentinel key must be popped before serialization.

    After calling _http_status_from_result (which pops it), the dict must
    no longer contain ``query_already_created``.
    """
    result = {"status": "running", "query_already_created": True, "data": []}
    _http_status_from_result(result)
    assert "query_already_created" not in result


# ---------------------------------------------------------------------------
# Round-4 fix — pre-execution-check exceptions from execute_sql_statements
# map to SupersetErrorsException with the DEFAULT status (HTTP 500). NOT 422.
# ---------------------------------------------------------------------------


def test_map_execute_error_superset_error_exception_keeps_500() -> None:
    from superset.commands.sqllab.execute import _map_execute_statements_error
    from superset.errors import ErrorLevel, SupersetError, SupersetErrorType
    from superset.exceptions import (
        SupersetErrorException,
        SupersetErrorsException,
    )

    err = SupersetError(
        error_type=SupersetErrorType.DML_NOT_ALLOWED_ERROR,
        message="DML not allowed",
        level=ErrorLevel.ERROR,
    )
    mapped = _map_execute_statements_error(
        SupersetErrorException(err), db_engine_spec=MagicMock()
    )
    assert isinstance(mapped, SupersetErrorsException)
    assert mapped.errors == [err]
    # Default SupersetException status — the original returns HTTP 500 here.
    assert mapped.status_code == 500


def test_map_execute_error_errors_exception_keeps_500() -> None:
    from superset.commands.sqllab.execute import _map_execute_statements_error
    from superset.errors import ErrorLevel, SupersetError, SupersetErrorType
    from superset.exceptions import SupersetErrorsException

    errs = [
        SupersetError(
            error_type=SupersetErrorType.GENERIC_DB_ENGINE_ERROR,
            message="boom",
            level=ErrorLevel.ERROR,
        )
    ]
    mapped = _map_execute_statements_error(
        SupersetErrorsException(errs), db_engine_spec=MagicMock()
    )
    assert isinstance(mapped, SupersetErrorsException)
    assert mapped.errors == errs
    assert mapped.status_code == 500


def test_map_execute_error_generic_uses_extract_errors() -> None:
    from superset.commands.sqllab.execute import _map_execute_statements_error
    from superset.exceptions import SupersetErrorsException

    spec = MagicMock()
    spec.extract_errors.return_value = [{"message": "db says no"}]
    mapped = _map_execute_statements_error(ValueError("db says no"), spec)
    assert isinstance(mapped, SupersetErrorsException)
    spec.extract_errors.assert_called_once_with("db says no")
    assert mapped.status_code == 500
