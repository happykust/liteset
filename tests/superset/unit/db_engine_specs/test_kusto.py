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
"""Native Liteset port of the upstream sync Kusto engine-spec unit tests.

Flask-free: no flask, flask_appbuilder or werkzeug.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

import pytest
from sqlalchemy import column

from superset.db_engine_specs.kusto import (
    KustoKqlEngineSpec,
    KustoSqlEngineSpec,
)
from superset.sql.parse import SQLScript
from tests.superset.unit.db_engine_specs.utils import assert_convert_dttm


@pytest.fixture
def dttm() -> datetime:
    return datetime.strptime("2019-01-02 03:04:05.678900", "%Y-%m-%d %H:%M:%S.%f")


@pytest.mark.parametrize(
    "sql,expected",
    [
        ("SELECT foo FROM tbl", False),
        ("SHOW TABLES", False),
        ("EXPLAIN SELECT foo FROM tbl", False),
        ("INSERT INTO tbl (foo) VALUES (1)", True),
    ],
)
def test_sql_has_mutation(sql: str, expected: bool) -> None:
    """
    Make sure that SQL dialect consider only SELECT statements as read-only
    """
    assert (
        SQLScript(
            sql,
            engine=KustoSqlEngineSpec.engine,
        ).has_mutation()
        == expected
    )


@pytest.mark.parametrize(
    "kql,expected",
    [
        ("tbl | limit 100", False),
        ("let foo = 1; tbl | where bar == foo", False),
        (".show tables", False),
        ("print 1", False),
        ("set querytrace; Events | take 100", False),
        (".drop table foo", True),
        (".set-or-append table foo <| bar", True),
    ],
)
def test_kql_has_mutation(kql: str, expected: bool) -> None:
    """
    Make sure that KQL dialect consider only SELECT statements as read-only
    """
    assert (
        SQLScript(
            kql,
            engine=KustoKqlEngineSpec.engine,
        ).has_mutation()
        == expected
    )


@pytest.mark.parametrize(
    "target_type,expected_result",
    [
        ("DateTime", "datetime(2019-01-02T03:04:05.678900)"),
        ("TimeStamp", "datetime(2019-01-02T03:04:05.678900)"),
        ("Date", "datetime(2019-01-02)"),
        ("UnknownType", None),
    ],
)
def test_kql_convert_dttm(
    target_type: str,
    expected_result: Optional[str],
    dttm: datetime,
) -> None:
    assert_convert_dttm(KustoKqlEngineSpec, target_type, expected_result, dttm)


@pytest.mark.parametrize(
    "target_type,expected_result",
    [
        ("Date", "CONVERT(DATE, '2019-01-02', 23)"),
        ("DateTime", "CONVERT(DATETIME, '2019-01-02T03:04:05.678', 126)"),
        ("SmallDateTime", "CONVERT(SMALLDATETIME, '2019-01-02 03:04:05', 20)"),
        ("TimeStamp", "CONVERT(TIMESTAMP, '2019-01-02 03:04:05', 20)"),
        ("UnknownType", None),
    ],
)
def test_sql_convert_dttm(
    target_type: str,
    expected_result: Optional[str],
    dttm: datetime,
) -> None:
    assert_convert_dttm(KustoSqlEngineSpec, target_type, expected_result, dttm)


@pytest.mark.parametrize(
    "in_duration,expected_result",
    [
        ("PT1S", "bin(temporal,1s)"),
        ("PT1M", "bin(temporal,1m)"),
        ("PT5M", "bin(temporal,5m)"),
        ("PT1H", "bin(temporal,1h)"),
        ("P1D", "startofday(temporal)"),
        ("P1W", "startofweek(temporal)"),
        ("P1M", "startofmonth(temporal)"),
        ("P1Y", "startofyear(temporal)"),
    ],
)
def test_timegrain_expressions(in_duration: str, expected_result: str) -> None:
    col = column("temporal")

    actual_result = KustoKqlEngineSpec.get_timestamp_expr(
        col=col, pdf=None, time_grain=in_duration
    )
    assert str(actual_result) == expected_result
