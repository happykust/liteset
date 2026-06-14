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
"""Native Liteset port of the upstream sync ElasticSearch engine-spec tests.

Tests the synchronous ``superset.db_engine_specs.elasticsearch`` specs
(ElasticSearch + OpenDistro). Flask-free: no flask, flask_appbuilder or
werkzeug.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

import pytest

from superset.db_engine_specs.elasticsearch import (
    ElasticSearchEngineSpec,
    OpenDistroEngineSpec,
)
from tests.superset.unit.db_engine_specs.utils import assert_convert_dttm


@pytest.fixture
def dttm() -> datetime:
    return datetime.strptime("2019-01-02 03:04:05.678900", "%Y-%m-%d %H:%M:%S.%f")


@pytest.mark.parametrize(
    "target_type,db_extra,expected_result",
    [
        ("DateTime", None, "CAST('2019-01-02T03:04:05' AS DATETIME)"),
        (
            "DateTime",
            {"version": "7.7"},
            "CAST('2019-01-02T03:04:05' AS DATETIME)",
        ),
        (
            "DateTime",
            {"version": "7.8"},
            "DATETIME_PARSE('2019-01-02 03:04:05', 'yyyy-MM-dd HH:mm:ss')",
        ),
        (
            "DateTime",
            {"version": "unparseable semver version"},
            "CAST('2019-01-02T03:04:05' AS DATETIME)",
        ),
        ("Unknown", None, None),
    ],
)
def test_elasticsearch_convert_dttm(
    target_type: str,
    db_extra: Optional[dict[str, Any]],
    expected_result: Optional[str],
    dttm: datetime,
) -> None:
    assert_convert_dttm(
        ElasticSearchEngineSpec, target_type, expected_result, dttm, db_extra
    )


@pytest.mark.parametrize(
    "target_type,expected_result",
    [
        ("DateTime", "'2019-01-02T03:04:05'"),
        ("Unknown", None),
    ],
)
def test_opendistro_convert_dttm(
    target_type: str,
    expected_result: Optional[str],
    dttm: datetime,
) -> None:
    assert_convert_dttm(OpenDistroEngineSpec, target_type, expected_result, dttm)


@pytest.mark.parametrize(
    "original,expected",
    [
        ("Col", "Col"),
        ("Col.keyword", "Col_keyword"),
    ],
)
def test_opendistro_sqla_column_label(original: str, expected: str) -> None:
    """
    DB Eng Specs (opendistro): Test column label
    """
    assert OpenDistroEngineSpec.make_label_compatible(original) == expected
