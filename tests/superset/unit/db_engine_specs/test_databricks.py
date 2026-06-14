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
"""Native Liteset port of the upstream sync DatabricksNativeEngineSpec unit tests.

Flask-free: no flask, flask_appbuilder or werkzeug.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

import pytest
from pytest_mock import MockerFixture

from superset.db_engine_specs.databricks import DatabricksNativeEngineSpec
from superset.errors import ErrorLevel, SupersetError, SupersetErrorType
from superset.utils import json
from tests.superset.unit.db_engine_specs.utils import assert_convert_dttm


@pytest.fixture
def dttm() -> datetime:
    return datetime.strptime("2019-01-02 03:04:05.678900", "%Y-%m-%d %H:%M:%S.%f")


def test_get_parameters_from_uri() -> None:
    """
    Test that the result from ``get_parameters_from_uri`` is JSON serializable.
    """
    from superset.db_engine_specs.databricks import (
        DatabricksNativeEngineSpec,
        DatabricksNativeParametersType,
    )

    parameters = DatabricksNativeEngineSpec.get_parameters_from_uri(
        "databricks+connector://token:abc12345@my_hostname:1234/test"
    )
    assert parameters == DatabricksNativeParametersType(
        {
            "access_token": "abc12345",
            "host": "my_hostname",
            "port": 1234,
            "database": "test",
            "encryption": False,
        }
    )
    assert json.loads(json.dumps(parameters)) == parameters


def test_build_sqlalchemy_uri() -> None:
    """
    test that the parameters are can correctly be compiled into a
    sqlalchemy_uri
    """
    from superset.db_engine_specs.databricks import (
        DatabricksNativeEngineSpec,
        DatabricksNativeParametersType,
    )

    parameters = DatabricksNativeParametersType(
        {
            "access_token": "abc12345",
            "host": "my_hostname",
            "port": 1234,
            "database": "test",
            "encryption": False,
        }
    )
    encrypted_extra = None
    sqlalchemy_uri = DatabricksNativeEngineSpec.build_sqlalchemy_uri(
        parameters, encrypted_extra
    )
    assert sqlalchemy_uri == (
        "databricks+connector://token:abc12345@my_hostname:1234/test"
    )


def test_parameters_json_schema() -> None:
    """
    test that the parameters schema can be converted to json
    """
    from superset.db_engine_specs.databricks import DatabricksNativeEngineSpec

    json_schema = DatabricksNativeEngineSpec.parameters_json_schema()

    # The Liteset port hand-rolls the JSON Schema dict (see
    # ``DATABRICKS_NATIVE_PROPERTIES_SCHEMA``) instead of generating it from a
    # Marshmallow ``Schema`` like upstream. Two artifacts differ from upstream:
    #   * ``port`` carries ``format: int32`` / ``exclusiveMaximum: True`` that
    #     upstream's Marshmallow output omits.
    #   * ``required`` preserves declaration order rather than the alphabetical
    #     order Marshmallow's JSONSchema emits.
    # Values and property set are otherwise identical; assert the real output.
    assert json_schema == {
        "type": "object",
        "properties": {
            "access_token": {"type": "string"},
            "host": {"type": "string"},
            "port": {
                "type": "integer",
                "format": "int32",
                "minimum": 0,
                "maximum": 65536,
                "exclusiveMaximum": True,
                "description": "Database port",
            },
            "encryption": {
                "type": "boolean",
                "description": "Use an encrypted connection to the database",
            },
            "database": {"type": "string"},
            "http_path": {"type": "string"},
        },
        "required": ["access_token", "host", "port", "database", "http_path"],
    }


def test_get_extra_params(mocker: MockerFixture) -> None:
    """
    Test the ``get_extra_params`` method.
    """
    from superset.db_engine_specs.databricks import DatabricksNativeEngineSpec

    database = mocker.MagicMock()

    database.extra = {}
    assert DatabricksNativeEngineSpec.get_extra_params(database) == {
        "engine_params": {
            "connect_args": {
                "http_headers": [("User-Agent", "Apache Superset")],
                "_user_agent_entry": "Apache Superset",
            }
        }
    }

    database.extra = json.dumps(
        {
            "engine_params": {
                "connect_args": {
                    "http_headers": [("User-Agent", "Custom user agent")],
                    "_user_agent_entry": "Custom user agent",
                    "foo": "bar",
                }
            }
        }
    )
    assert DatabricksNativeEngineSpec.get_extra_params(database) == {
        "engine_params": {
            "connect_args": {
                "http_headers": [["User-Agent", "Custom user agent"]],
                "_user_agent_entry": "Custom user agent",
                "foo": "bar",
            }
        }
    }

    # it should also remove whitespace from http_path
    database.extra = json.dumps(
        {
            "engine_params": {
                "connect_args": {
                    "http_headers": [("User-Agent", "Custom user agent")],
                    "_user_agent_entry": "Custom user agent",
                    "http_path": "/some_path_here_with_whitespace ",
                }
            }
        }
    )
    assert DatabricksNativeEngineSpec.get_extra_params(database) == {
        "engine_params": {
            "connect_args": {
                "http_headers": [["User-Agent", "Custom user agent"]],
                "_user_agent_entry": "Custom user agent",
                "http_path": "/some_path_here_with_whitespace",
            }
        }
    }


def test_extract_errors() -> None:
    """
    Test that custom error messages are extracted correctly.
    """

    msg = ": mismatched input 'from_'. Expecting: "
    result = DatabricksNativeEngineSpec.extract_errors(Exception(msg))

    assert result == [
        SupersetError(
            message=": mismatched input 'from_'. Expecting: ",
            error_type=SupersetErrorType.GENERIC_DB_ENGINE_ERROR,
            level=ErrorLevel.ERROR,
            extra={
                "engine_name": "Databricks (legacy)",
                "issue_codes": [
                    {
                        "code": 1002,
                        "message": "Issue 1002 - The database returned an unexpected error.",  # noqa: E501
                    }
                ],
            },
        )
    ]


def test_extract_errors_with_context() -> None:
    """
    Test that custom error messages are extracted correctly with context.
    """

    msg = ": mismatched input 'from_'. Expecting: "
    context = {"hostname": "foo"}
    result = DatabricksNativeEngineSpec.extract_errors(Exception(msg), context)

    assert result == [
        SupersetError(
            message=": mismatched input 'from_'. Expecting: ",
            error_type=SupersetErrorType.GENERIC_DB_ENGINE_ERROR,
            level=ErrorLevel.ERROR,
            extra={
                "engine_name": "Databricks (legacy)",
                "issue_codes": [
                    {
                        "code": 1002,
                        "message": "Issue 1002 - The database returned an unexpected error.",  # noqa: E501
                    }
                ],
            },
        )
    ]


@pytest.mark.parametrize(
    "target_type,expected_result",
    [
        ("Date", "CAST('2019-01-02' AS DATE)"),
        (
            "TimeStamp",
            "CAST('2019-01-02 03:04:05.678900' AS TIMESTAMP)",
        ),
        ("UnknownType", None),
    ],
)
def test_convert_dttm(
    target_type: str,
    expected_result: Optional[str],
    dttm: datetime,
) -> None:
    from superset.db_engine_specs.databricks import (
        DatabricksNativeEngineSpec as spec,  # noqa: N813
    )

    assert_convert_dttm(spec, target_type, expected_result, dttm)


def test_get_prequeries(mocker: MockerFixture) -> None:
    """
    Test the ``get_prequeries`` method.
    """
    from superset.db_engine_specs.databricks import DatabricksNativeEngineSpec

    database = mocker.MagicMock()

    assert DatabricksNativeEngineSpec.get_prequeries(database) == []
    assert DatabricksNativeEngineSpec.get_prequeries(database, schema="test") == [
        "USE SCHEMA `test`",
    ]
    assert DatabricksNativeEngineSpec.get_prequeries(database, catalog="test") == [
        "USE CATALOG `test`",
    ]
    assert DatabricksNativeEngineSpec.get_prequeries(
        database, catalog="foo", schema="bar"
    ) == [
        "USE CATALOG `foo`",
        "USE SCHEMA `bar`",
    ]

    assert DatabricksNativeEngineSpec.get_prequeries(
        database, catalog="with-hyphen", schema="hyphen-again"
    ) == [
        "USE CATALOG `with-hyphen`",
        "USE SCHEMA `hyphen-again`",
    ]

    assert DatabricksNativeEngineSpec.get_prequeries(
        database, catalog="`escaped-hyphen`", schema="`hyphen-escaped`"
    ) == [
        "USE CATALOG `escaped-hyphen`",
        "USE SCHEMA `hyphen-escaped`",
    ]
