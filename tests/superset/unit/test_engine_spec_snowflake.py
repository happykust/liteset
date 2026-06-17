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
"""Exercises the synchronous ``SnowflakeEngineSpec`` (pure helpers: dttm
conversion, connection mutator, error extraction, query cancellation,
extra/schema/catalog params, encrypted-extra masking, boolean filters).
All cases are Flask-free.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from unittest import mock

import pytest
from pytest_mock import MockerFixture
from sqlalchemy.engine.url import make_url

from superset.db_engine_specs.snowflake import SnowflakeEngineSpec
from superset.errors import ErrorLevel, SupersetError, SupersetErrorType
from superset.models.core import Database
from superset.models.sql_lab import Query
from superset.utils import json


@pytest.fixture
def dttm() -> datetime:
    return datetime(2019, 1, 2, 3, 4, 5, 678900)


def assert_convert_dttm(
    target_type: str,
    expected_result: Optional[str],
    dttm: datetime,
) -> None:
    for target in (
        target_type,
        target_type.upper(),
        target_type.lower(),
        target_type.capitalize(),
    ):
        assert (
            result := SnowflakeEngineSpec.convert_dttm(
                target_type=target,
                dttm=dttm,
                db_extra=None,
            )
        ) == expected_result, result


@pytest.mark.parametrize(
    "target_type,expected_result",
    [
        ("Date", "TO_DATE('2019-01-02')"),
        ("DateTime", "CAST('2019-01-02T03:04:05.678900' AS DATETIME)"),
        ("TimeStamp", "TO_TIMESTAMP('2019-01-02T03:04:05.678900')"),
        ("TIMESTAMP_NTZ", "TO_TIMESTAMP('2019-01-02T03:04:05.678900')"),
        ("TIMESTAMP_LTZ", "TO_TIMESTAMP('2019-01-02T03:04:05.678900')"),
        ("TIMESTAMP_TZ", "TO_TIMESTAMP('2019-01-02T03:04:05.678900')"),
        ("TIMESTAMPLTZ", "TO_TIMESTAMP('2019-01-02T03:04:05.678900')"),
        ("TIMESTAMPNTZ", "TO_TIMESTAMP('2019-01-02T03:04:05.678900')"),
        ("TIMESTAMPTZ", "TO_TIMESTAMP('2019-01-02T03:04:05.678900')"),
        (
            "TIMESTAMP WITH LOCAL TIME ZONE",
            "TO_TIMESTAMP('2019-01-02T03:04:05.678900')",
        ),
        ("TIMESTAMP WITHOUT TIME ZONE", "TO_TIMESTAMP('2019-01-02T03:04:05.678900')"),
        ("UnknownType", None),
    ],
)
def test_convert_dttm(
    target_type: str,
    expected_result: Optional[str],
    dttm: datetime,
) -> None:
    assert_convert_dttm(target_type, expected_result, dttm)


def test_database_connection_test_mutator() -> None:
    database = Database(sqlalchemy_uri="snowflake://abc")
    SnowflakeEngineSpec.mutate_db_for_connection_test(database)
    engine_params = json.loads(database.extra or "{}")

    assert {
        "engine_params": {"connect_args": {"validate_default_parameters": True}}
    } == engine_params


def test_extract_errors() -> None:
    msg = "Object dumbBrick does not exist or not authorized."
    result = SnowflakeEngineSpec.extract_errors(Exception(msg))
    assert result == [
        SupersetError(
            message="dumbBrick does not exist in this database.",
            error_type=SupersetErrorType.OBJECT_DOES_NOT_EXIST_ERROR,
            level=ErrorLevel.ERROR,
            extra={
                "engine_name": "Snowflake",
                "issue_codes": [
                    {
                        "code": 1029,
                        "message": "Issue 1029 - The object does not exist in the given database.",  # noqa: E501
                    }
                ],
            },
        )
    ]

    msg = "syntax error line 1 at position 10 unexpected 'limited'."
    result = SnowflakeEngineSpec.extract_errors(Exception(msg))
    assert result == [
        SupersetError(
            message='Please check your query for syntax errors at or near "limited". Then, try running your query again.',  # noqa: E501
            error_type=SupersetErrorType.SYNTAX_ERROR,
            level=ErrorLevel.ERROR,
            extra={
                "engine_name": "Snowflake",
                "issue_codes": [
                    {
                        "code": 1030,
                        "message": "Issue 1030 - The query has a syntax error.",
                    }
                ],
            },
        )
    ]


@mock.patch("sqlalchemy.engine.Engine.connect")
def test_get_cancel_query_id(engine_mock: mock.Mock) -> None:
    query = Query()
    cursor_mock = engine_mock.return_value.__enter__.return_value
    cursor_mock.fetchone.return_value = [123]
    assert SnowflakeEngineSpec.get_cancel_query_id(cursor_mock, query) == 123


@mock.patch("sqlalchemy.engine.Engine.connect")
def test_cancel_query(engine_mock: mock.Mock) -> None:
    query = Query()
    cursor_mock = engine_mock.return_value.__enter__.return_value
    assert SnowflakeEngineSpec.cancel_query(cursor_mock, query, "123") is True


@mock.patch("sqlalchemy.engine.Engine.connect")
def test_cancel_query_failed(engine_mock: mock.Mock) -> None:
    query = Query()
    cursor_mock = engine_mock.raiseError.side_effect = Exception()
    assert SnowflakeEngineSpec.cancel_query(cursor_mock, query, "123") is False


def test_get_extra_params(mocker: MockerFixture) -> None:
    """
    Test the ``get_extra_params`` method.
    """
    database = mocker.MagicMock()

    database.extra = {}
    assert SnowflakeEngineSpec.get_extra_params(database) == {
        "engine_params": {"connect_args": {"application": "Apache Superset"}}
    }

    database.extra = json.dumps(
        {
            "engine_params": {
                "connect_args": {"application": "Custom user agent", "foo": "bar"}
            }
        }
    )
    assert SnowflakeEngineSpec.get_extra_params(database) == {
        "engine_params": {
            "connect_args": {"application": "Custom user agent", "foo": "bar"}
        }
    }


def test_get_schema_from_engine_params() -> None:
    """
    Test the ``get_schema_from_engine_params`` method.
    """
    assert (
        SnowflakeEngineSpec.get_schema_from_engine_params(
            make_url("snowflake://user:pass@account/database_name/default"),
            {},
        )
        == "default"
    )

    assert (
        SnowflakeEngineSpec.get_schema_from_engine_params(
            make_url("snowflake://user:pass@account/database_name"),
            {},
        )
        is None
    )

    assert (
        SnowflakeEngineSpec.get_schema_from_engine_params(
            make_url("snowflake://user:pass@account/"),
            {},
        )
        is None
    )


def _render(url: object) -> str:
    # SQLAlchemy 2.0 masks the password in ``str(URL)``; render with the
    # password visible so we can compare against the original expectations.
    return url.render_as_string(hide_password=False)  # type: ignore[attr-defined]


def test_adjust_engine_params_fully_qualified() -> None:
    """
    Test the ``adjust_engine_params`` method when the URL has catalog and schema.
    """
    url = make_url("snowflake://user:pass@account/database_name/default")

    uri = SnowflakeEngineSpec.adjust_engine_params(url, {})[0]
    assert _render(uri) == "snowflake://user:pass@account/database_name/default"

    uri = SnowflakeEngineSpec.adjust_engine_params(
        url,
        {},
        schema="new_schema",
    )[0]
    assert _render(uri) == "snowflake://user:pass@account/database_name/new_schema"

    uri = SnowflakeEngineSpec.adjust_engine_params(
        url,
        {},
        catalog="new_catalog",
    )[0]
    assert _render(uri) == "snowflake://user:pass@account/new_catalog/default"

    uri = SnowflakeEngineSpec.adjust_engine_params(
        url,
        {},
        catalog="new_catalog",
        schema="new_schema",
    )[0]
    assert _render(uri) == "snowflake://user:pass@account/new_catalog/new_schema"


def test_adjust_engine_params_catalog_only() -> None:
    """
    Test the ``adjust_engine_params`` method when the URL has only the catalog.
    """
    url = make_url("snowflake://user:pass@account/database_name")

    uri = SnowflakeEngineSpec.adjust_engine_params(url, {})[0]
    assert _render(uri) == "snowflake://user:pass@account/database_name"

    uri = SnowflakeEngineSpec.adjust_engine_params(
        url,
        {},
        schema="new_schema",
    )[0]
    assert _render(uri) == "snowflake://user:pass@account/database_name/new_schema"

    uri = SnowflakeEngineSpec.adjust_engine_params(
        url,
        {},
        catalog="new_catalog",
    )[0]
    assert _render(uri) == "snowflake://user:pass@account/new_catalog"

    uri = SnowflakeEngineSpec.adjust_engine_params(
        url,
        {},
        catalog="new_catalog",
        schema="new_schema",
    )[0]
    assert _render(uri) == "snowflake://user:pass@account/new_catalog/new_schema"


def test_get_default_catalog() -> None:
    """
    Test the ``get_default_catalog`` method.
    """
    database = Database(
        database_name="my_db",
        sqlalchemy_uri="snowflake://user:pass@account/database_name",
    )
    assert SnowflakeEngineSpec.get_default_catalog(database) == "database_name"

    database = Database(
        database_name="my_db",
        sqlalchemy_uri="snowflake://user:pass@account/database_name/default",
    )
    assert SnowflakeEngineSpec.get_default_catalog(database) == "database_name"


def test_mask_encrypted_extra() -> None:
    """
    Test that the private keys are masked when the database is edited.
    """
    config = json.dumps(
        {
            "auth_method": "keypair",
            "auth_params": {
                "privatekey_body": (
                    "-----BEGIN ENCRYPTED PRIVATE KEY-----"
                    "..."
                    "-----END ENCRYPTED PRIVATE KEY-----"
                ),
                "privatekey_pass": "my_password",
            },
        }
    )

    assert SnowflakeEngineSpec.mask_encrypted_extra(config) == json.dumps(
        {
            "auth_method": "keypair",
            "auth_params": {
                "privatekey_body": "XXXXXXXXXX",
                "privatekey_pass": "XXXXXXXXXX",
            },
        }
    )


def test_mask_encrypted_extra_no_fields() -> None:
    """
    Test that the private key is masked when the database is edited.
    """
    config = json.dumps(
        {
            # this is a fake example and the fields are made up
            "auth_method": "token",
            "auth_params": {
                "jwt": "SECRET",
            },
        }
    )

    assert SnowflakeEngineSpec.mask_encrypted_extra(config) == json.dumps(
        {
            "auth_method": "token",
            "auth_params": {
                "jwt": "SECRET",
            },
        }
    )


def test_handle_boolean_filter() -> None:
    """
    Test that Snowflake uses equality operators for boolean filters instead of IS.
    """
    from sqlalchemy import Boolean, Column

    from superset.utils.core import FilterOperator

    bool_col = Column("test_col", Boolean)

    result_true = SnowflakeEngineSpec.handle_boolean_filter(
        bool_col, FilterOperator.IS_TRUE, True
    )
    assert (
        str(result_true.compile(compile_kwargs={"literal_binds": True}))
        == "test_col = true"
    )

    result_false = SnowflakeEngineSpec.handle_boolean_filter(
        bool_col, FilterOperator.IS_FALSE, False
    )
    assert (
        str(result_false.compile(compile_kwargs={"literal_binds": True}))
        == "test_col = false"
    )


def test_use_equality_for_boolean_filters_property() -> None:
    """
    Test that Snowflake has the use_equality_for_boolean_filters property set to True.
    """
    assert SnowflakeEngineSpec.use_equality_for_boolean_filters is True


def test_unmask_encrypted_extra() -> None:
    """
    Test that the private keys can be reused from the previous `encrypted_extra`.
    """
    old = json.dumps(
        {
            "auth_method": "keypair",
            "auth_params": {
                "privatekey_body": (
                    "-----BEGIN ENCRYPTED PRIVATE KEY-----"
                    "..."
                    "-----END ENCRYPTED PRIVATE KEY-----"
                ),
                "privatekey_pass": "my_password",
            },
        }
    )
    new = json.dumps(
        {
            "foo": "bar",
            "auth_method": "keypair",
            "auth_params": {
                "privatekey_body": "XXXXXXXXXX",
                "privatekey_pass": "XXXXXXXXXX",
            },
        }
    )

    assert SnowflakeEngineSpec.unmask_encrypted_extra(old, new) == json.dumps(
        {
            "foo": "bar",
            "auth_method": "keypair",
            "auth_params": {
                "privatekey_body": (
                    "-----BEGIN ENCRYPTED PRIVATE KEY-----"
                    "..."
                    "-----END ENCRYPTED PRIVATE KEY-----"
                ),
                "privatekey_pass": "my_password",
            },
        }
    )
