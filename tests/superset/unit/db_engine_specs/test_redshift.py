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
"""Native Liteset port of the upstream sync RedshiftEngineSpec unit tests.

Flask-free: no flask, flask_appbuilder or werkzeug.
"""

from __future__ import annotations

import datetime
from typing import Optional

import pytest

from tests.superset.unit.db_engine_specs.utils import assert_convert_dttm


@pytest.fixture
def dttm() -> datetime.datetime:
    return datetime.datetime(2019, 1, 2, 3, 4, 5, 678900)


@pytest.mark.parametrize(
    "target_type,expected_result",
    [
        ("Date", "TO_DATE('2019-01-02', 'YYYY-MM-DD')"),
        (
            "DateTime",
            "TO_TIMESTAMP('2019-01-02 03:04:05.678900', 'YYYY-MM-DD HH24:MI:SS.US')",
        ),
        (
            "TimeStamp",
            "TO_TIMESTAMP('2019-01-02 03:04:05.678900', 'YYYY-MM-DD HH24:MI:SS.US')",
        ),
        ("UnknownType", None),
    ],
)
def test_convert_dttm(
    target_type: str,
    expected_result: Optional[str],
    dttm: datetime.datetime,
) -> None:
    from superset.db_engine_specs.redshift import (
        RedshiftEngineSpec as spec,  # noqa: N813
    )

    assert_convert_dttm(spec, target_type, expected_result, dttm)
