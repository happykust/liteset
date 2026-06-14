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
"""Liteset port of ``tests/unit_tests/db_engine_specs/test_athena.py``.

Flask-free: no flask / flask_appbuilder / werkzeug imports.  The ``dttm``
fixture and ``assert_convert_dttm`` helper are inlined here instead of
pulling them from the vendored ``tests/unit_tests`` helpers (which import
Flask).
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import Boolean, Column

from superset.db_engine_specs.athena import AthenaEngineSpec
from superset.errors import ErrorLevel, SupersetError, SupersetErrorType
from superset.utils.core import FilterOperator


@pytest.fixture
def dttm() -> datetime:
    return datetime.strptime("2019-01-02 03:04:05.678900", "%Y-%m-%d %H:%M:%S.%f")


@pytest.mark.parametrize(
    "target_type,expected_result",
    [
        ("Date", "DATE '2019-01-02'"),
        ("TimeStamp", "TIMESTAMP '2019-01-02 03:04:05.678'"),
        ("UnknownType", None),
    ],
)
def test_convert_dttm(
    target_type: str,
    expected_result: str | None,
    dttm: datetime,
) -> None:
    for target in (
        target_type,
        target_type.upper(),
        target_type.lower(),
        target_type.capitalize(),
    ):
        assert (
            result := AthenaEngineSpec.convert_dttm(target_type=target, dttm=dttm)
        ) == expected_result, result


def test_extract_errors() -> None:
    """
    Test that custom error messages are extracted correctly.
    """
    msg = ": mismatched input 'from_'. Expecting: "
    result = AthenaEngineSpec.extract_errors(Exception(msg))
    assert result == [
        SupersetError(
            message='Please check your query for syntax errors at or near "from_". Then, try running your query again.',  # noqa: E501
            error_type=SupersetErrorType.SYNTAX_ERROR,
            level=ErrorLevel.ERROR,
            extra={
                "engine_name": "Amazon Athena",
                "issue_codes": [
                    {
                        "code": 1030,
                        "message": "Issue 1030 - The query has a syntax error.",
                    }
                ],
            },
        )
    ]


def test_get_text_clause_with_colon() -> None:
    """
    Make sure text clauses don't escape the colon character.
    """
    query = (
        "SELECT foo FROM tbl WHERE " r"abc >= TIMESTAMP '2021-11-26T00\:00\:00.000000'"
    )
    text_clause = AthenaEngineSpec.get_text_clause(query)
    assert text_clause.text == query


def test_handle_boolean_filter() -> None:
    """
    Test that Athena uses equality operators for boolean filters instead of IS.
    """
    bool_col = Column("test_col", Boolean)

    result_true = AthenaEngineSpec.handle_boolean_filter(
        bool_col, FilterOperator.IS_TRUE, True
    )
    # The result should be an equality comparison, not an IS comparison.
    assert (
        str(result_true.compile(compile_kwargs={"literal_binds": True}))
        == "test_col = true"
    )

    result_false = AthenaEngineSpec.handle_boolean_filter(
        bool_col, FilterOperator.IS_FALSE, False
    )
    assert (
        str(result_false.compile(compile_kwargs={"literal_binds": True}))
        == "test_col = false"
    )
