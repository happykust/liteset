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

from liteset.db.engine_specs.clickhouse import AsyncClickHouseEngineSpec


def test_engine_attributes() -> None:
    assert AsyncClickHouseEngineSpec.engine == "clickhouse"
    assert AsyncClickHouseEngineSpec.engine_name == "ClickHouse"
    assert AsyncClickHouseEngineSpec.default_driver == "asynch"


def test_time_grain_expressions() -> None:
    grains = AsyncClickHouseEngineSpec.get_time_grain_expressions()
    assert grains[None] == "{col}"
    assert grains["PT1M"] == "toStartOfMinute(toDateTime({col}))"
    assert grains["P1D"] == "toStartOfDay(toDateTime({col}))"
    assert grains["P1Y"] == "toStartOfYear(toDateTime({col}))"
    assert "PT1S" in grains
    assert len(grains) == 13


async def test_execute() -> None:
    mock_result = MagicMock()
    mock_result.returns_rows = True
    mock_result.keys.return_value = ["id", "value"]
    mock_result.fetchall.return_value = [(1, 42)]
    mock_result.rowcount = 1

    mock_conn = AsyncMock()
    mock_conn.execute.return_value = mock_result

    rs = await AsyncClickHouseEngineSpec.execute(mock_conn, "SELECT * FROM t")
    assert rs.columns == ["id", "value"]
    assert rs.data == [(1, 42)]
    assert rs.row_count == 1


async def test_fetch_data_with_limit() -> None:
    mock_result = MagicMock()
    mock_result.fetchmany.return_value = [(1,)]

    mock_conn = AsyncMock()
    mock_conn.execute.return_value = mock_result

    rows = await AsyncClickHouseEngineSpec.fetch_data(
        mock_conn, "SELECT id FROM t", limit=10
    )
    assert rows == [(1,)]
    mock_result.fetchmany.assert_called_once_with(10)


async def test_get_schema_names() -> None:
    mock_result = MagicMock()
    mock_result.fetchall.return_value = [("default",), ("system",)]

    mock_conn = AsyncMock()
    mock_conn.execute.return_value = mock_result

    schemas = await AsyncClickHouseEngineSpec.get_schema_names(mock_conn)
    assert schemas == {"default", "system"}


async def test_get_table_names_with_schema() -> None:
    mock_result = MagicMock()
    mock_result.fetchall.return_value = [("events",), ("users",)]

    mock_conn = AsyncMock()
    mock_conn.execute.return_value = mock_result

    tables = await AsyncClickHouseEngineSpec.get_table_names(mock_conn, schema="mydb")
    assert tables == {"events", "users"}
    query_text = str(mock_conn.execute.call_args[0][0])
    assert "FROM `mydb`" in query_text


async def test_get_table_names_rejects_invalid_schema() -> None:
    mock_conn = AsyncMock()
    with pytest.raises(ValueError, match="Invalid schema identifier"):
        await AsyncClickHouseEngineSpec.get_table_names(mock_conn, schema="'; DROP TABLE --")


def test_adjust_engine_params_defaults() -> None:
    uri, args = AsyncClickHouseEngineSpec.adjust_engine_params(
        "clickhouse+asynch://localhost/db"
    )
    assert args["connect_timeout"] == 10
    assert args["send_receive_timeout"] == 300


def test_adjust_engine_params_preserves_existing() -> None:
    uri, args = AsyncClickHouseEngineSpec.adjust_engine_params(
        "clickhouse+asynch://localhost/db",
        {"connect_timeout": 30},
    )
    assert args["connect_timeout"] == 30
    assert args["send_receive_timeout"] == 300
