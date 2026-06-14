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
"""Liteset port of ``tests/unit_tests/db_engine_specs/test_base.py``.

Covers the synchronous ``superset.db_engine_specs.base.BaseEngineSpec``
helpers.  Flask-free: the upstream test reached into
``flask.current_app.config`` for ``test_validate_db_uri`` — here it is
translated to the Liteset ``SupersetSettings.db_sqla_uri_validator`` config
knob that ``BaseEngineSpec.validate_database_uri`` actually reads.

Named ``*_base_sync`` to avoid colliding with the pre-existing
``test_engine_spec_base.py`` (which covers the async base spec).
"""

from __future__ import annotations

import json
from textwrap import dedent
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import Boolean, Column, Integer, types
from sqlalchemy.dialects import sqlite
from sqlalchemy.engine.url import make_url, URL
from sqlalchemy.sql import sqltypes

from superset.db_engine_specs.base import BaseEngineSpec, convert_inspector_columns
from superset.sql.parse import Table
from superset.utils.core import FilterOperator, GenericDataType


def test_get_text_clause_with_colon() -> None:
    """
    Make sure text clauses are correctly escaped.
    """
    text_clause = BaseEngineSpec.get_text_clause(
        "SELECT foo FROM tbl WHERE foo = '123:456')"
    )
    assert text_clause.text == "SELECT foo FROM tbl WHERE foo = '123\\:456')"


def test_validate_db_uri() -> None:
    """
    Ensures that the ``validate_database_uri`` method invokes the validator.

    Upstream patched ``flask.current_app.config``; the Liteset engine spec
    reads ``SupersetSettings().db_sqla_uri_validator`` instead.
    """

    def mock_validate(sqlalchemy_uri: URL) -> None:
        raise ValueError("Invalid URI")

    fake_settings = MagicMock()
    fake_settings.db_sqla_uri_validator = mock_validate

    with patch("superset.config.SupersetSettings", return_value=fake_settings):
        with pytest.raises(ValueError):  # noqa: PT011
            BaseEngineSpec.validate_database_uri(URL.create("sqlite"))


@pytest.mark.parametrize(
    "original,expected",
    [
        (
            dedent(
                """
with currency as
(
select 'INR' as cur
)
select * from currency
"""
            ),
            None,
        ),
        (
            "SELECT 1 as cnt",
            None,
        ),
        (
            dedent(
                """
select 'INR' as cur
union
select 'AUD' as cur
union
select 'USD' as cur
"""
            ),
            None,
        ),
    ],
)
def test_cte_query_parsing(original: str, expected: str | None) -> None:
    actual = BaseEngineSpec.get_cte_query(original)
    assert actual == expected


@pytest.mark.parametrize(
    "native_type,sqla_type,attrs,generic_type,is_dttm",
    [
        ("SMALLINT", types.SmallInteger, None, GenericDataType.NUMERIC, False),
        ("INTEGER", types.Integer, None, GenericDataType.NUMERIC, False),
        ("BIGINT", types.BigInteger, None, GenericDataType.NUMERIC, False),
        ("DECIMAL", types.Numeric, None, GenericDataType.NUMERIC, False),
        ("NUMERIC", types.Numeric, None, GenericDataType.NUMERIC, False),
        ("REAL", types.REAL, None, GenericDataType.NUMERIC, False),
        ("DOUBLE PRECISION", types.Float, None, GenericDataType.NUMERIC, False),
        ("MONEY", types.Numeric, None, GenericDataType.NUMERIC, False),
        # String
        ("CHAR", types.String, None, GenericDataType.STRING, False),
        ("VARCHAR", types.String, None, GenericDataType.STRING, False),
        ("TEXT", types.String, None, GenericDataType.STRING, False),
        # Temporal
        ("DATE", types.Date, None, GenericDataType.TEMPORAL, True),
        ("TIMESTAMP", types.TIMESTAMP, None, GenericDataType.TEMPORAL, True),
        ("TIME", types.Time, None, GenericDataType.TEMPORAL, True),
        # Boolean
        ("BOOLEAN", types.Boolean, None, GenericDataType.BOOLEAN, False),
    ],
)
def test_get_column_spec(
    native_type: str,
    sqla_type: type[types.TypeEngine],
    attrs: dict[str, Any] | None,
    generic_type: GenericDataType,
    is_dttm: bool,
) -> None:
    from superset.db_engine_specs.databricks import DatabricksNativeEngineSpec

    column_spec = DatabricksNativeEngineSpec.get_column_spec(native_type)
    assert column_spec is not None
    assert isinstance(column_spec.sqla_type, sqla_type)
    for key, value in (attrs or {}).items():
        assert getattr(column_spec.sqla_type, key) == value
    assert column_spec.generic_type == generic_type
    assert column_spec.is_dttm == is_dttm


@pytest.mark.parametrize(
    "cols, expected_result",
    [
        (
            [{"name": "John", "type": "integer", "is_dttm": False}],
            [
                {
                    "column_name": "John",
                    "name": "John",
                    "type": "integer",
                    "is_dttm": False,
                }
            ],
        ),
        (
            [{"name": "hugh", "type": "integer", "is_dttm": False}],
            [
                {
                    "column_name": "hugh",
                    "name": "hugh",
                    "type": "integer",
                    "is_dttm": False,
                }
            ],
        ),
    ],
)
def test_convert_inspector_columns(
    cols: list[dict[str, Any]], expected_result: list[dict[str, Any]]
) -> None:
    assert convert_inspector_columns(cols) == expected_result


def test_select_star() -> None:
    """
    Test the ``select_star`` method.
    """
    cols: list[dict[str, Any]] = [
        {
            "column_name": "a",
            "name": "a",
            "type": sqltypes.String(),
            "nullable": True,
            "comment": None,
            "default": None,
            "precision": None,
            "scale": None,
            "max_length": None,
            "is_dttm": False,
        },
    ]

    # mock the database so we can compile the query
    database = MagicMock()
    database.compile_sqla_query = lambda query, catalog, schema: str(
        query.compile(dialect=sqlite.dialect())
    )

    engine = MagicMock()
    engine.dialect = sqlite.dialect()

    sql = BaseEngineSpec.select_star(
        database=database,
        table=Table("my_table", "my_schema", "my_catalog"),
        engine=engine,
        limit=100,
        show_cols=True,
        indent=True,
        latest_partition=False,
        cols=cols,
    )
    assert sql == "SELECT\n  a\nFROM my_schema.my_table\nLIMIT ?\nOFFSET ?"


def test_extra_table_metadata() -> None:
    """
    Test the deprecated ``extra_table_metadata`` method.
    """
    from superset.models.core import Database

    class ThirdPartyDBEngineSpec(BaseEngineSpec):
        @classmethod
        def extra_table_metadata(
            cls,
            database: Database,
            table_name: str,
            schema_name: str | None,
        ) -> dict[str, Any]:
            return {"table": table_name, "schema": schema_name}

    database = MagicMock()

    with patch("superset.db_engine_specs.base.warnings") as warnings:
        assert ThirdPartyDBEngineSpec.get_extra_table_metadata(
            database,
            Table("table", "schema"),
        ) == {"table": "table", "schema": "schema"}

        assert (
            ThirdPartyDBEngineSpec.get_extra_table_metadata(
                database,
                Table("table", "schema", "catalog"),
            )
            == {}
        )

        warnings.warn.assert_called()


def test_get_default_catalog() -> None:
    """
    Test the ``get_default_catalog`` method.
    """
    database = MagicMock()
    assert BaseEngineSpec.get_default_catalog(database) is None


def test_quote_table() -> None:
    """
    Test the ``quote_table`` function.
    """
    dialect = sqlite.dialect()

    assert BaseEngineSpec.quote_table(Table("table"), dialect) == '"table"'
    assert (
        BaseEngineSpec.quote_table(Table("table", "schema"), dialect)
        == 'schema."table"'
    )
    assert (
        BaseEngineSpec.quote_table(Table("table", "schema", "catalog"), dialect)
        == 'catalog.schema."table"'
    )
    assert (
        BaseEngineSpec.quote_table(Table("ta ble", "sche.ma", 'cata"log'), dialect)
        == '"cata""log"."sche.ma"."ta ble"'
    )


def test_mask_encrypted_extra() -> None:
    """
    Test that the private key is masked when the database is edited.
    """
    config = json.dumps(
        {
            "foo": "bar",
            "service_account_info": {
                "project_id": "black-sanctum-314419",
                "private_key": "SECRET",
            },
        }
    )

    assert BaseEngineSpec.mask_encrypted_extra(config) == json.dumps(
        {
            "foo": "XXXXXXXXXX",
            "service_account_info": "XXXXXXXXXX",
        }
    )


def test_unmask_encrypted_extra() -> None:
    """
    Test that the private key can be reused from the previous ``encrypted_extra``.
    """
    old = json.dumps(
        {
            "foo": "bar",
            "service_account_info": {
                "project_id": "black-sanctum-314419",
                "private_key": "SECRET",
            },
        }
    )
    new = json.dumps(
        {
            "foo": "XXXXXXXXXX",
            "service_account_info": "XXXXXXXXXX",
        }
    )

    assert BaseEngineSpec.unmask_encrypted_extra(old, new) == json.dumps(
        {
            "foo": "bar",
            "service_account_info": {
                "project_id": "black-sanctum-314419",
                "private_key": "SECRET",
            },
        }
    )


def test_impersonate_user_backwards_compatible() -> None:
    """
    Test that the ``impersonate_user`` method calls the methods it replaced.

    The Liteset implementation inspects the real signature of
    ``update_impersonation_config`` (it imports ``inspect.signature`` inside
    the method), so we drive behaviour with a subclass whose
    ``update_impersonation_config`` declares a ``database`` parameter, then
    patch with ``autospec=True`` to preserve that signature while recording
    calls.
    """
    database = MagicMock()
    url = make_url("sqlite://foo.db")
    new_url = make_url("sqlite://bar.db")
    engine_kwargs = {"connect_args": {"user": "alice"}}

    class SpecWithDatabase(BaseEngineSpec):
        @classmethod
        def update_impersonation_config(  # type: ignore[override]
            cls,
            database: Any,
            connect_args: dict[str, Any],
            uri: Any,
            username: str | None,
            access_token: str | None,
        ) -> None:
            pass

    with (
        patch.object(
            SpecWithDatabase, "get_url_for_impersonation", return_value=new_url
        ) as get_url_for_impersonation,
        patch.object(
            SpecWithDatabase, "update_impersonation_config", autospec=True
        ) as update_impersonation_config,
    ):
        SpecWithDatabase.impersonate_user(
            database, "alice", "SECRET", url, engine_kwargs
        )

    get_url_for_impersonation.assert_called_once_with(url, True, "alice", "SECRET")
    assert update_impersonation_config.call_args.args == (
        database,
        {"user": "alice"},
        new_url,
        "alice",
        "SECRET",
    )


def test_impersonate_user_no_database() -> None:
    """
    Test ``impersonate_user`` when ``update_impersonation_config`` has the
    old (no-``database``) signature.
    """
    database = MagicMock()
    url = make_url("sqlite://foo.db")
    new_url = make_url("sqlite://bar.db")
    engine_kwargs = {"connect_args": {"user": "alice"}}

    class SpecWithoutDatabase(BaseEngineSpec):
        @classmethod
        def update_impersonation_config(  # type: ignore[override]
            cls,
            connect_args: dict[str, Any],
            uri: Any,
            username: str | None,
            access_token: str | None,
        ) -> None:
            pass

    with (
        patch.object(
            SpecWithoutDatabase, "get_url_for_impersonation", return_value=new_url
        ) as get_url_for_impersonation,
        patch.object(
            SpecWithoutDatabase, "update_impersonation_config", autospec=True
        ) as update_impersonation_config,
    ):
        SpecWithoutDatabase.impersonate_user(
            database, "alice", "SECRET", url, engine_kwargs
        )

    get_url_for_impersonation.assert_called_once_with(url, True, "alice", "SECRET")
    assert update_impersonation_config.call_args.args == (
        {"user": "alice"},
        new_url,
        "alice",
        "SECRET",
    )


def test_handle_boolean_filter_default_behavior() -> None:
    """
    Test that BaseEngineSpec uses IS operators for boolean filters by default.
    """
    bool_col = Column("test_col", Boolean)

    result_true = BaseEngineSpec.handle_boolean_filter(bool_col, "IS TRUE", True)
    assert hasattr(result_true, "left")  # IS comparison has left/right attributes
    assert hasattr(result_true, "right")

    result_false = BaseEngineSpec.handle_boolean_filter(bool_col, "IS FALSE", False)
    assert hasattr(result_false, "left")
    assert hasattr(result_false, "right")


def test_handle_boolean_filter_with_equality() -> None:
    """
    Test that BaseEngineSpec can use equality operators when configured.
    """

    class TestEngineSpec(BaseEngineSpec):
        use_equality_for_boolean_filters = True

    bool_col = Column("test_col", Boolean)

    result_true = TestEngineSpec.handle_boolean_filter(bool_col, "IS TRUE", True)
    assert str(type(result_true)).endswith("BinaryExpression'>")

    result_false = TestEngineSpec.handle_boolean_filter(bool_col, "IS FALSE", False)
    assert str(type(result_false)).endswith("BinaryExpression'>")


def test_handle_null_filter() -> None:
    """
    Test null/not null filter handling.
    """
    bool_col = Column("test_col", Boolean)

    result_null = BaseEngineSpec.handle_null_filter(bool_col, FilterOperator.IS_NULL)
    assert hasattr(result_null, "left")
    assert hasattr(result_null, "right")

    result_not_null = BaseEngineSpec.handle_null_filter(
        bool_col, FilterOperator.IS_NOT_NULL
    )
    assert hasattr(result_not_null, "left")
    assert hasattr(result_not_null, "right")

    with pytest.raises(ValueError, match="Invalid null filter operator"):
        BaseEngineSpec.handle_null_filter(bool_col, "INVALID")  # type: ignore[arg-type]


def test_handle_comparison_filter() -> None:
    """
    Test comparison filter handling for all operators.
    """
    int_col = Column("test_col", Integer)

    operators_and_values = [
        (FilterOperator.EQUALS, 5),
        (FilterOperator.NOT_EQUALS, 5),
        (FilterOperator.GREATER_THAN, 5),
        (FilterOperator.LESS_THAN, 5),
        (FilterOperator.GREATER_THAN_OR_EQUALS, 5),
        (FilterOperator.LESS_THAN_OR_EQUALS, 5),
    ]

    for op, value in operators_and_values:
        result = BaseEngineSpec.handle_comparison_filter(int_col, op, value)
        assert str(type(result)).endswith("BinaryExpression'>")

    with pytest.raises(ValueError, match="Invalid comparison filter operator"):
        BaseEngineSpec.handle_comparison_filter(int_col, "INVALID", 5)  # type: ignore[arg-type]


def test_use_equality_for_boolean_filters_property() -> None:
    """
    Test that BaseEngineSpec has the correct default value for the boolean
    filter property.
    """
    assert BaseEngineSpec.use_equality_for_boolean_filters is False
