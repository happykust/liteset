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

from superset.db.engine_specs.postgres import AsyncPostgresEngineSpec


def test_engine_attributes() -> None:
    assert AsyncPostgresEngineSpec.engine == "postgresql"
    assert AsyncPostgresEngineSpec.engine_name == "PostgreSQL"
    assert AsyncPostgresEngineSpec.default_driver == "asyncpg"


def test_time_grain_expressions() -> None:
    grains = AsyncPostgresEngineSpec.get_time_grain_expressions()
    assert grains[None] == "{col}"
    assert "DATE_TRUNC('day', {col})" == grains["P1D"]
    assert "DATE_TRUNC('month', {col})" == grains["P1M"]
    assert "PT1S" in grains
    assert "PT1H" in grains
    assert "P1Y" in grains
    assert len(grains) == 15


async def test_execute() -> None:
    mock_result = MagicMock()
    mock_result.returns_rows = True
    mock_result.keys.return_value = ["id", "name"]
    mock_result.fetchall.return_value = [(1, "alice"), (2, "bob")]
    mock_result.rowcount = 2

    mock_conn = AsyncMock()
    mock_conn.execute.return_value = mock_result

    rs = await AsyncPostgresEngineSpec.execute(mock_conn, "SELECT * FROM users")
    assert rs.columns == ["id", "name"]
    assert rs.data == [(1, "alice"), (2, "bob")]
    assert rs.row_count == 2


async def test_execute_non_returning() -> None:
    mock_result = MagicMock()
    mock_result.returns_rows = False
    mock_result.rowcount = 3

    mock_conn = AsyncMock()
    mock_conn.execute.return_value = mock_result

    rs = await AsyncPostgresEngineSpec.execute(
        mock_conn, "UPDATE users SET active = true"
    )
    assert rs.columns == []
    assert rs.data == []
    assert rs.row_count == 3


async def test_fetch_data() -> None:
    mock_result = MagicMock()
    mock_result.fetchall.return_value = [(1, "alice"), (2, "bob")]

    mock_conn = AsyncMock()
    mock_conn.execute.return_value = mock_result

    rows = await AsyncPostgresEngineSpec.fetch_data(mock_conn, "SELECT * FROM users")
    assert rows == [(1, "alice"), (2, "bob")]


async def test_fetch_data_with_limit() -> None:
    mock_result = MagicMock()
    mock_result.fetchmany.return_value = [(1, "alice")]

    mock_conn = AsyncMock()
    mock_conn.execute.return_value = mock_result

    rows = await AsyncPostgresEngineSpec.fetch_data(
        mock_conn, "SELECT * FROM users", limit=1
    )
    assert rows == [(1, "alice")]
    mock_result.fetchmany.assert_called_once_with(1)


def test_extract_errors_invalid_username() -> None:
    ex = Exception('FATAL: role "baduser" does not exist')
    errors = AsyncPostgresEngineSpec.extract_errors(ex)
    assert len(errors) == 1
    assert errors[0]["message"] == "Invalid username: baduser"
    assert errors[0]["error_type"] == "DatabaseError"


def test_extract_errors_unknown_database() -> None:
    ex = Exception('FATAL: database "nope" does not exist')
    errors = AsyncPostgresEngineSpec.extract_errors(ex)
    assert errors[0]["message"] == "Unknown database: nope"


def test_extract_errors_fallback() -> None:
    ex = ValueError("something unexpected")
    errors = AsyncPostgresEngineSpec.extract_errors(ex)
    assert errors[0]["message"] == "something unexpected"
    assert errors[0]["error_type"] == "ValueError"


def test_adjust_engine_params_defaults() -> None:
    uri, args = AsyncPostgresEngineSpec.adjust_engine_params(
        "postgresql+asyncpg://localhost/db"
    )
    assert uri == "postgresql+asyncpg://localhost/db"
    assert args["statement_cache_size"] == 0
    assert args["prepared_statement_cache_size"] == 0


def test_adjust_engine_params_preserves_existing() -> None:
    uri, args = AsyncPostgresEngineSpec.adjust_engine_params(
        "postgresql+asyncpg://localhost/db",
        {"statement_cache_size": 100, "sslmode": "require"},
    )
    assert args["statement_cache_size"] == 100  # preserved, not overwritten
    assert args["sslmode"] == "require"
    assert args["prepared_statement_cache_size"] == 0  # added
