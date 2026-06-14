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
"""Native Liteset port of the upstream sync DuckDB / MotherDuck engine-spec tests.

Tests the synchronous ``superset.db_engine_specs.duckdb`` specs. Flask-free:
no flask, flask_appbuilder or werkzeug.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

import pytest
from pytest_mock import MockerFixture

from superset.db_engine_specs.duckdb import (
    DuckDBEngineSpec,
    DuckDBEngineSpec as spec,  # noqa: N813
    DuckDBParametersType,
    MotherDuckEngineSpec,
)
from superset.utils import json
from superset.utils.core import GenericDataType
from tests.superset.unit.db_engine_specs.utils import assert_convert_dttm


@pytest.fixture
def dttm() -> datetime:
    return datetime.strptime("2019-01-02 03:04:05.678900", "%Y-%m-%d %H:%M:%S.%f")


@pytest.mark.parametrize(
    "target_type,expected_result",
    [
        ("Text", "'2019-01-02 03:04:05.678900'"),
        ("DateTime", "'2019-01-02 03:04:05.678900'"),
        ("UnknownType", None),
    ],
)
def test_convert_dttm(
    target_type: str,
    expected_result: Optional[str],
    dttm: datetime,
) -> None:
    assert_convert_dttm(spec, target_type, expected_result, dttm)


def test_get_extra_params(mocker: MockerFixture) -> None:
    """
    Test the ``get_extra_params`` method.

    In Liteset the version string is read from ``SupersetSettings`` (the
    upstream ``VERSION_STRING`` config key), so we patch the settings object
    to return a deterministic version.
    """
    settings = mocker.MagicMock()
    settings.version_string = "1.0.0"
    # get_user_agent also reads SupersetSettings; force the default user-agent
    # path by leaving user_agent_func unset.
    settings.user_agent_func = None
    mocker.patch("superset.config.SupersetSettings", return_value=settings)

    database = mocker.MagicMock()

    database.extra = {}
    assert DuckDBEngineSpec.get_extra_params(database) == {
        "engine_params": {
            "connect_args": {"config": {"custom_user_agent": "apache-superset/1.0.0"}}
        }
    }

    database.extra = json.dumps(
        {"engine_params": {"connect_args": {"config": {"custom_user_agent": "my-app"}}}}
    )
    assert DuckDBEngineSpec.get_extra_params(database) == {
        "engine_params": {
            "connect_args": {
                "config": {"custom_user_agent": "apache-superset/1.0.0 my-app"}
            }
        }
    }


def test_build_sqlalchemy_uri() -> None:
    """Test DuckDBEngineSpec.build_sqlalchemy_uri"""
    # No database provided, default to :memory:
    parameters = DuckDBParametersType()
    uri = DuckDBEngineSpec.build_sqlalchemy_uri(parameters)
    assert "duckdb:///:memory:" == uri

    # Database provided
    parameters = DuckDBParametersType(database="/path/to/duck.db")
    uri = DuckDBEngineSpec.build_sqlalchemy_uri(parameters)
    assert "duckdb:////path/to/duck.db" == uri


def test_md_build_sqlalchemy_uri() -> None:
    """Test MotherDuckEngineSpec.build_sqlalchemy_uri"""
    # No access token provided, throw ValueError
    parameters = DuckDBParametersType(database="my_db")
    with pytest.raises(ValueError):  # noqa: PT011
        MotherDuckEngineSpec.build_sqlalchemy_uri(parameters)

    # No database provided, default to "md:"
    parameters = DuckDBParametersType(access_token="token")  # noqa: S106
    uri = MotherDuckEngineSpec.build_sqlalchemy_uri(parameters)
    assert "duckdb:///md:?motherduck_token=token"

    # Database and access_token provided
    parameters = DuckDBParametersType(database="my_db", access_token="token")  # noqa: S106
    uri = MotherDuckEngineSpec.build_sqlalchemy_uri(parameters)
    assert "duckdb:///md:my_db?motherduck_token=token" == uri


def test_get_parameters_from_uri() -> None:
    uri = "duckdb:////path/to/duck.db"
    parameters = DuckDBEngineSpec.get_parameters_from_uri(uri)

    assert parameters["database"] == "/path/to/duck.db"

    uri = "duckdb:///md:my_db?motherduck_token=token"
    parameters = DuckDBEngineSpec.get_parameters_from_uri(uri)

    assert parameters["database"] == "md:my_db"
    assert parameters["access_token"] == "token"  # noqa: S105


def test_column_type_recognition() -> None:
    """Test that DuckDB column types are properly recognized as numeric."""
    # Test standard float/double types
    numeric_types = [
        "FLOAT",
        "DOUBLE",
        "DOUBLE PRECISION",
        "REAL",
        "DECIMAL(10,2)",
        "NUMERIC(10,2)",
        "INTEGER",
        "BIGINT",
        "SMALLINT",
        # DuckDB-specific unsigned types
        "HUGEINT",
        "UBIGINT",
        "UINTEGER",
        "USMALLINT",
        "UTINYINT",
    ]

    for type_str in numeric_types:
        col_spec = DuckDBEngineSpec.get_column_spec(type_str)
        assert col_spec is not None, f"Type {type_str} should be recognized"
        assert col_spec.generic_type == GenericDataType.NUMERIC, (
            f"Type {type_str} should be recognized as NUMERIC, "
            f"got {col_spec.generic_type}"
        )

    # Test that TINYINT (non-unsigned) is also recognized
    # Note: TINYINT is not in the default mappings, but should be handled
    col_spec = DuckDBEngineSpec.get_column_spec("TINYINT")
    # TINYINT matches the pattern "^int" so it should be recognized
    assert col_spec is None, "TINYINT doesn't match any patterns"
