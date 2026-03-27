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

from unittest.mock import AsyncMock, MagicMock

import pytest

from superset.commands.sqllab import (
    CreateSqlLabPermalinkCommand,
    EstimateQueryCostCommand,
    ExecuteSQLCommand,
    FormatSQLCommand,
    GetSqlLabPermalinkCommand,
    GetSQLResultsCommand,
)
from superset.exceptions import CommandInvalidError, ObjectNotFoundError


async def test_execute_sql_validates_empty_sql():
    dao = AsyncMock()
    cmd = ExecuteSQLCommand(dao=dao, database_id=1, sql="  ")
    with pytest.raises(CommandInvalidError, match="empty"):
        await cmd.validate()


async def test_execute_sql_validates_no_db():
    dao = AsyncMock()
    cmd = ExecuteSQLCommand(dao=dao, database_id=0, sql="SELECT 1")
    with pytest.raises(CommandInvalidError, match="database_id"):
        await cmd.validate()


async def test_execute_sql_success():
    # Smoke test — stub implementation, revisit in superset/remaining-api
    dao = AsyncMock()
    cmd = ExecuteSQLCommand(dao=dao, database_id=1, sql="SELECT 1")
    result = await cmd.execute()
    assert result["status"] == "success"


async def test_estimate_query_cost_empty_sql():
    cmd = EstimateQueryCostCommand(database_id=1, sql="")
    with pytest.raises(CommandInvalidError):
        await cmd.validate()


async def test_estimate_query_cost_returns_result():
    # Smoke test — stub implementation, revisit in superset/remaining-api
    cmd = EstimateQueryCostCommand(database_id=1, sql="SELECT 1")
    result = await cmd.execute()
    assert isinstance(result, list)


async def test_format_sql_empty():
    cmd = FormatSQLCommand(sql="  ")
    with pytest.raises(CommandInvalidError):
        await cmd.validate()


async def test_format_sql_passthrough():
    # Smoke test — stub implementation, revisit in superset/remaining-api
    cmd = FormatSQLCommand(sql="SELECT 1")
    result = await cmd.execute()
    assert isinstance(result, str)
    assert "SELECT" in result.upper() or "select" in result.lower()


async def test_get_results_empty_key():
    cmd = GetSQLResultsCommand(key="")
    with pytest.raises(CommandInvalidError):
        await cmd.validate()


async def test_get_results_no_cache():
    # Smoke test — stub implementation, revisit in superset/remaining-api
    cmd = GetSQLResultsCommand(key="test-key")
    result = await cmd.execute()
    assert result["status"] == "not_found"


async def test_create_sqllab_permalink():
    dao = AsyncMock()
    dao.set_value = AsyncMock()
    cmd = CreateSqlLabPermalinkCommand(dao=dao, state={"sql": "SELECT 1"})
    key = await cmd.execute()
    assert isinstance(key, str)
    assert len(key) >= 16


async def test_get_sqllab_permalink():
    dao = AsyncMock()
    dao.get_value = AsyncMock(return_value='{"sql": "SELECT 1"}')
    cmd = GetSqlLabPermalinkCommand(dao=dao, key="abc12345")
    result = await cmd.execute()
    assert result == {"sql": "SELECT 1"}


async def test_get_sqllab_permalink_not_found():
    dao = AsyncMock()
    dao.get_value = AsyncMock(return_value=None)
    cmd = GetSqlLabPermalinkCommand(dao=dao, key="missing")
    with pytest.raises(ObjectNotFoundError):
        await cmd.execute()


# ---------------------------------------------------------------------------
# NEW-T7: FormatSQLCommand — ImportError and SqlglotError branches
# ---------------------------------------------------------------------------


async def test_format_sql_returns_original_when_sqlglot_not_installed():
    """FormatSQLCommand returns original SQL when sqlglot is not importable."""
    import builtins
    from unittest.mock import patch

    original_import = builtins.__import__

    def mock_import(name, *args, **kwargs):
        if name == "sqlglot":
            raise ImportError("No module named 'sqlglot'")
        return original_import(name, *args, **kwargs)

    cmd = FormatSQLCommand(sql="SELECT 1")
    await cmd.validate()
    with patch("builtins.__import__", side_effect=mock_import):
        result = await cmd.run()
    assert result == "SELECT 1"


async def test_format_sql_returns_original_on_sqlglot_error():
    """FormatSQLCommand returns original SQL when sqlglot fails to parse."""
    from unittest.mock import MagicMock, patch

    mock_sqlglot = MagicMock()
    mock_errors = MagicMock()

    class FakeSqlglotError(Exception):
        pass

    mock_errors.SqlglotError = FakeSqlglotError
    mock_sqlglot.transpile = MagicMock(side_effect=FakeSqlglotError("parse error"))
    mock_sqlglot.errors = mock_errors

    cmd = FormatSQLCommand(sql="INVALID SQL %%%")
    await cmd.validate()

    import sys

    with patch.dict(
        sys.modules, {"sqlglot": mock_sqlglot, "sqlglot.errors": mock_errors}
    ):
        result = await cmd.run()
    assert result == "INVALID SQL %%%"


# ---------------------------------------------------------------------------
# NEW-T9: Cache exception paths in SQLLab GetSQLResultsCommand
# ---------------------------------------------------------------------------


async def test_get_results_cache_exception_returns_not_found():
    """GetSQLResultsCommand returns not_found when cache.get raises."""
    cache = MagicMock()
    cache.get = MagicMock(side_effect=RuntimeError("connection refused"))
    cmd = GetSQLResultsCommand(key="test-key", cache_manager=cache)
    await cmd.validate()
    result = await cmd.run()
    assert result["status"] == "not_found"


async def test_get_results_cache_hit_returns_data():
    """GetSQLResultsCommand returns cached data on hit."""
    cached_data = {"status": "success", "data": [1, 2, 3], "columns": ["a"]}
    cache = MagicMock()
    cache.get = MagicMock(return_value=cached_data)
    cmd = GetSQLResultsCommand(key="test-key", cache_manager=cache)
    await cmd.validate()
    result = await cmd.run()
    assert result == cached_data
