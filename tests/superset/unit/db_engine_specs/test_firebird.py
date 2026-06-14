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
"""Native Liteset port of the upstream sync FirebirdEngineSpec unit tests.

Tests the synchronous ``superset.db_engine_specs.firebird`` spec. Flask-free:
no flask, flask_appbuilder or werkzeug.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

import pytest

from superset.db_engine_specs.firebird import FirebirdEngineSpec as spec  # noqa: N813
from tests.superset.unit.db_engine_specs.utils import assert_convert_dttm


@pytest.fixture
def dttm() -> datetime:
    return datetime.strptime("2019-01-02 03:04:05.678900", "%Y-%m-%d %H:%M:%S.%f")


@pytest.mark.parametrize(
    "time_grain,expected",
    [
        (None, "timestamp_column"),
        (
            "PT1S",
            (
                "CAST(CAST(timestamp_column AS DATE) "
                "|| ' ' "
                "|| EXTRACT(HOUR FROM timestamp_column) "
                "|| ':' "
                "|| EXTRACT(MINUTE FROM timestamp_column) "
                "|| ':' "
                "|| FLOOR(EXTRACT(SECOND FROM timestamp_column)) AS TIMESTAMP)"
            ),
        ),
        (
            "PT1M",
            (
                "CAST(CAST(timestamp_column AS DATE) "
                "|| ' ' "
                "|| EXTRACT(HOUR FROM timestamp_column) "
                "|| ':' "
                "|| EXTRACT(MINUTE FROM timestamp_column) "
                "|| ':00' AS TIMESTAMP)"
            ),
        ),
        ("P1D", "CAST(timestamp_column AS DATE)"),
        (
            "P1M",
            (
                "CAST(EXTRACT(YEAR FROM timestamp_column) "
                "|| '-' "
                "|| EXTRACT(MONTH FROM timestamp_column) "
                "|| '-01' AS DATE)"
            ),
        ),
        ("P1Y", "CAST(EXTRACT(YEAR FROM timestamp_column) || '-01-01' AS DATE)"),
    ],
)
def test_time_grain_expressions(time_grain: Optional[str], expected: str) -> None:
    assert (
        spec._time_grain_expressions[time_grain].format(
            col="timestamp_column",
        )
        == expected
    )


def test_epoch_to_dttm() -> None:
    assert (
        spec.epoch_to_dttm().format(col="timestamp_column")
        == "DATEADD(second, timestamp_column, CAST('00:00:00' AS TIMESTAMP))"
    )


@pytest.mark.parametrize(
    "target_type,expected_result",
    [
        ("Date", "CAST('2019-01-02' AS DATE)"),
        ("DateTime", "CAST('2019-01-02 03:04:05.6789' AS TIMESTAMP)"),
        ("TimeStamp", "CAST('2019-01-02 03:04:05.6789' AS TIMESTAMP)"),
        ("Time", "CAST('03:04:05.678900' AS TIME)"),
        ("UnknownType", None),
    ],
)
def test_convert_dttm(
    target_type: str,
    expected_result: Optional[str],
    dttm: datetime,
) -> None:
    assert_convert_dttm(spec, target_type, expected_result, dttm)
