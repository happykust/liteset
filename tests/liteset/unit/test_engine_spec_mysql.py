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

from liteset.db.engine_specs.mysql import AsyncMySQLEngineSpec


def test_engine_attributes() -> None:
    assert AsyncMySQLEngineSpec.engine == "mysql"
    assert AsyncMySQLEngineSpec.engine_name == "MySQL"
    assert AsyncMySQLEngineSpec.default_driver == "asyncmy"


def test_time_grain_expressions() -> None:
    grains = AsyncMySQLEngineSpec.get_time_grain_expressions()
    assert grains[None] == "{col}"
    assert grains["P1D"] == "DATE({col})"
    assert "PT1S" in grains
    assert "PT1M" in grains
    assert "PT1H" in grains
    assert "P1W" in grains
    assert "P1M" in grains
    assert "P3M" in grains
    assert "P1Y" in grains
    assert "1969-12-29T00:00:00Z/P1W" in grains
    assert len(grains) == 10


async def test_execute() -> None:
    mock_result = MagicMock()
    mock_result.returns_rows = True
    mock_result.keys.return_value = ["id", "value"]
    mock_result.fetchall.return_value = [(1, "test")]
    mock_result.rowcount = 1

    mock_conn = AsyncMock()
    mock_conn.execute.return_value = mock_result

    rs = await AsyncMySQLEngineSpec.execute(mock_conn, "SELECT * FROM t")
    assert rs.columns == ["id", "value"]
    assert rs.data == [(1, "test")]
    assert rs.row_count == 1


async def test_fetch_data_with_limit() -> None:
    mock_result = MagicMock()
    mock_result.fetchmany.return_value = [(1,)]

    mock_conn = AsyncMock()
    mock_conn.execute.return_value = mock_result

    rows = await AsyncMySQLEngineSpec.fetch_data(
        mock_conn, "SELECT id FROM t", limit=5
    )
    assert rows == [(1,)]
    mock_result.fetchmany.assert_called_once_with(5)


def test_extract_errors_access_denied() -> None:
    ex = Exception("Access denied for user 'root'@'localhost'")
    errors = AsyncMySQLEngineSpec.extract_errors(ex)
    assert errors[0]["message"] == "Access denied for user: root"


def test_extract_errors_unknown_host() -> None:
    ex = Exception("Unknown MySQL server host 'badhost.example.com'")
    errors = AsyncMySQLEngineSpec.extract_errors(ex)
    assert errors[0]["message"] == "Unknown hostname: badhost.example.com"


def test_extract_errors_unknown_database() -> None:
    ex = Exception("Unknown database 'nope'")
    errors = AsyncMySQLEngineSpec.extract_errors(ex)
    assert errors[0]["message"] == "Unknown database: nope"


def test_extract_errors_fallback() -> None:
    ex = RuntimeError("unexpected")
    errors = AsyncMySQLEngineSpec.extract_errors(ex)
    assert errors[0]["message"] == "unexpected"
    assert errors[0]["error_type"] == "RuntimeError"


def test_adjust_engine_params_defaults() -> None:
    uri, args = AsyncMySQLEngineSpec.adjust_engine_params(
        "mysql+asyncmy://localhost/db"
    )
    assert args["charset"] == "utf8mb4"
    assert args["connect_timeout"] == 10


def test_adjust_engine_params_preserves_existing() -> None:
    uri, args = AsyncMySQLEngineSpec.adjust_engine_params(
        "mysql+asyncmy://localhost/db",
        {"charset": "latin1", "read_timeout": 30},
    )
    assert args["charset"] == "latin1"  # preserved
    assert args["read_timeout"] == 30  # preserved
    assert args["connect_timeout"] == 10  # added
