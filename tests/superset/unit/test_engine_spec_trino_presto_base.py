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
"""Parity tests: TrinoEngineSpec must inherit the PrestoBase cost/partition
helpers (R13-10).

Upstream defines ``estimate_statement_cost``, ``query_cost_formatter``,
``get_function_names`` and ``where_latest_partition`` on
``PrestoBaseEngineSpec`` so that **both** Presto and Trino get them. The port
originally placed them on ``PrestoEngineSpec`` only, silently downgrading
Trino to the ``BaseEngineSpec`` stubs (cost-estimate raised, autocomplete
returned ``[]``, latest-partition filtering vanished).
"""

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from sqlalchemy import column, select, table

from superset.db_engine_specs.trino import Date, TimeStamp, TrinoEngineSpec


def test_trino_estimate_statement_cost_runs_explain():
    """Cost estimation must run EXPLAIN (TYPE IO, FORMAT JSON) like upstream."""
    cursor = MagicMock()
    cursor.fetchone.return_value = ['{"estimate": {"outputRowCount": 100}}']

    result = TrinoEngineSpec.estimate_statement_cost(MagicMock(), "SELECT 1", cursor)

    cursor.execute.assert_called_once_with("EXPLAIN (TYPE IO, FORMAT JSON) SELECT 1")
    assert result == {"estimate": {"outputRowCount": 100}}


def test_trino_query_cost_formatter_humanizes():
    raw_cost = [
        {
            "estimate": {
                "outputRowCount": 873265878.0,
                "outputSizeInBytes": 3414257.0,
                "cpuCost": 1000.0,
                "maxMemory": 0.0,
                "networkCost": 0.0,
            }
        }
    ]
    formatted = TrinoEngineSpec.query_cost_formatter(raw_cost)
    assert formatted == [
        {
            "Output count": "873 M rows",
            "Output size": "3 MB",
            "CPU cost": "1000",
            "Max memory": "0 B",
            "Network cost": "0",
        }
    ]


def test_trino_get_function_names_uses_show_functions():
    database = MagicMock()
    database.id = 1
    database.get_df.return_value = pd.DataFrame({"Function": ["abs", "array_agg"]})

    with patch("superset.extensions.cache_manager") as cache_manager:
        cache_manager.sync_cache.get.return_value = None
        names = TrinoEngineSpec.get_function_names(database)

    database.get_df.assert_called_once_with("SHOW FUNCTIONS")
    assert names == ["abs", "array_agg"]


def test_trino_where_latest_partition_filters_query():
    query = select(column("*")).select_from(table("my_table"))
    with patch.object(
        TrinoEngineSpec, "latest_partition", return_value=(["ds"], ["2024-01-01"])
    ):
        result = TrinoEngineSpec.where_latest_partition(
            database=MagicMock(),
            table=MagicMock(),
            query=query,
            columns=[{"column_name": "ds", "type": "VARCHAR"}],
        )

    assert result is not None
    compiled = str(result.compile(compile_kwargs={"literal_binds": True}))
    assert "ds = '2024-01-01'" in compiled


def test_trino_where_latest_partition_unpartitioned_returns_none():
    with patch.object(
        TrinoEngineSpec, "latest_partition", side_effect=Exception("not partitioned")
    ):
        assert (
            TrinoEngineSpec.where_latest_partition(
                database=MagicMock(),
                table=MagicMock(),
                query=select(column("*")),
                columns=None,
            )
            is None
        )


@pytest.mark.parametrize(
    "type_,literal,expected",
    [
        (Date, "2024-01-02", "DATE '2024-01-02'"),
        (TimeStamp, "2024-01-02 03:04:05", "TIMESTAMP '2024-01-02 03:04:05'"),
    ],
)
def test_presto_date_timestamp_inline_rendering(type_, literal, expected):
    """Upstream presto_sql_types render partition values as typed literals."""
    assert type_.process_bind_param(literal, MagicMock()) == expected
