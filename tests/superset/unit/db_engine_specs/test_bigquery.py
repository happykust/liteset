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
"""Native Liteset port of the upstream sync BigQueryEngineSpec unit tests.

Flask-free: no flask, flask_appbuilder or werkzeug.

``test_get_fields`` and ``test_select_star`` are retained as explicit skips:
they require ``sqlalchemy_bigquery.BigQueryDialect`` (the optional
``sqlalchemy-bigquery`` driver) to compile SQL, which is not installed in the
Liteset runtime dependency set. The ``BigQueryEngineSpec`` methods they exercise
(``_get_fields``, ``select_star``) do exist in the port; only the compile-time
dialect dependency is missing.
"""

from __future__ import annotations

from datetime import datetime
from importlib.util import find_spec
from typing import Optional

import pytest
from pytest_mock import MockerFixture
from sqlalchemy import select
from sqlalchemy.engine.url import make_url
from sqlalchemy.sql import sqltypes

from superset.db_engine_specs.base import ResultSetColumnType
from superset.sql.parse import Table
from superset.utils import json
from tests.superset.unit.db_engine_specs.utils import assert_convert_dttm

HAS_BIGQUERY_DIALECT = find_spec("sqlalchemy_bigquery") is not None


@pytest.fixture
def dttm() -> datetime:
    return datetime.strptime("2019-01-02 03:04:05.678900", "%Y-%m-%d %H:%M:%S.%f")


@pytest.mark.skipif(
    not HAS_BIGQUERY_DIALECT,
    reason="optional sqlalchemy-bigquery driver (BigQueryDialect) not installed",
)
def test_get_fields() -> None:
    """
    Test the custom ``_get_fields`` method.

    The method adds custom labels (aliases) to the columns to prevent
    collision when referencing record fields. Eg, if we had these two
    columns:

        name STRING
        project STRUCT<name STRING>

    One could write this query:

        SELECT
            `name`,
            `project`.`name`
        FROM
            the_table

    But then both columns would get aliased as "name".

    The custom method will replace the fields so that the final query
    looks like this:

        SELECT
            `name` AS `name`,
            `project`.`name` AS project__name
        FROM
            the_table

    """
    from sqlalchemy_bigquery import BigQueryDialect

    from superset.db_engine_specs.bigquery import BigQueryEngineSpec

    columns: list[ResultSetColumnType] = [
        {"column_name": "limit", "name": "limit", "type": "STRING", "is_dttm": False},
        {"column_name": "name", "name": "name", "type": "STRING", "is_dttm": False},
        {
            "column_name": "project.name",
            "name": "project.name",
            "type": "STRING",
            "is_dttm": False,
        },
    ]
    fields = BigQueryEngineSpec._get_fields(columns)

    query = select(fields)
    assert str(query.compile(dialect=BigQueryDialect())) == (
        "SELECT `limit` AS `limit`, `name` AS `name`, "
        "`project`.`name` AS `project__name`"
    )


@pytest.mark.skipif(
    not HAS_BIGQUERY_DIALECT,
    reason="optional sqlalchemy-bigquery driver (BigQueryDialect) not installed",
)
def test_select_star(mocker: MockerFixture) -> None:
    """
    Test the ``select_star`` method.

    The method removes pseudo-columns from structures inside arrays. While these
    pseudo-columns show up as "columns" for metadata reasons, we can't select them
    in the query, as opposed to fields from non-array structures.
    """
    from sqlalchemy_bigquery import BigQueryDialect

    from superset.db_engine_specs.bigquery import BigQueryEngineSpec

    cols: list[ResultSetColumnType] = [
        {
            "column_name": "trailer",
            "name": "trailer",
            "type": sqltypes.ARRAY(sqltypes.JSON()),
            "nullable": True,
            "comment": None,
            "default": None,
            "precision": None,
            "scale": None,
            "max_length": None,
            "is_dttm": False,
        },
        {
            "column_name": "trailer.key",
            "name": "trailer.key",
            "type": sqltypes.String(),
            "nullable": True,
            "comment": None,
            "default": None,
            "precision": None,
            "scale": None,
            "max_length": None,
            "is_dttm": False,
        },
        {
            "column_name": "trailer.value",
            "name": "trailer.value",
            "type": sqltypes.String(),
            "nullable": True,
            "comment": None,
            "default": None,
            "precision": None,
            "scale": None,
            "max_length": None,
            "is_dttm": False,
        },
        {
            "column_name": "trailer.email",
            "name": "trailer.email",
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
    database = mocker.MagicMock()
    database.compile_sqla_query = lambda query, catalog, schema: str(
        query.compile(dialect=BigQueryDialect(), compile_kwargs={"literal_binds": True})
    )

    engine = mocker.MagicMock()
    engine.dialect = BigQueryDialect()

    sql = BigQueryEngineSpec.select_star(
        database=database,
        table=Table("my_table"),
        engine=engine,
        limit=100,
        show_cols=True,
        indent=True,
        latest_partition=False,
        cols=cols,
    )
    assert (
        sql
        == """SELECT
  `trailer` AS `trailer`
FROM `my_table`
LIMIT 100"""
    )


def test_get_parameters_from_uri_serializable() -> None:
    """
    Test that the result from ``get_parameters_from_uri`` is JSON serializable.
    """
    from superset.db_engine_specs.bigquery import BigQueryEngineSpec

    parameters = BigQueryEngineSpec.get_parameters_from_uri(
        "bigquery://dbt-tutorial-347100/",
        {"access_token": "TOP_SECRET"},
    )
    assert parameters == {"access_token": "TOP_SECRET", "query": {}}
    assert json.loads(json.dumps(parameters)) == parameters


def test_unmask_encrypted_extra() -> None:
    """
    Test that the private key can be reused from the previous `encrypted_extra`.
    """
    from superset.db_engine_specs.bigquery import BigQueryEngineSpec

    old = json.dumps(
        {
            "credentials_info": {
                "project_id": "black-sanctum-314419",
                "private_key": "SECRET",
            },
        }
    )
    new = json.dumps(
        {
            "credentials_info": {
                "project_id": "yellow-unicorn-314419",
                "private_key": "XXXXXXXXXX",
            },
        }
    )

    assert BigQueryEngineSpec.unmask_encrypted_extra(old, new) == json.dumps(
        {
            "credentials_info": {
                "project_id": "yellow-unicorn-314419",
                "private_key": "SECRET",
            },
        }
    )


def test_unmask_encrypted_extra_field_changeed() -> None:
    """
    Test that the private key is not reused when the field has changed.
    """
    from superset.db_engine_specs.bigquery import BigQueryEngineSpec

    old = json.dumps(
        {
            "credentials_info": {
                "project_id": "black-sanctum-314419",
                "private_key": "SECRET",
            },
        }
    )
    new = json.dumps(
        {
            "credentials_info": {
                "project_id": "yellow-unicorn-314419",
                "private_key": "NEW-SECRET",
            },
        }
    )

    assert BigQueryEngineSpec.unmask_encrypted_extra(old, new) == json.dumps(
        {
            "credentials_info": {
                "project_id": "yellow-unicorn-314419",
                "private_key": "NEW-SECRET",
            },
        }
    )


def test_unmask_encrypted_extra_when_old_is_none() -> None:
    """
    Test that a `None` value for the old field works for `encrypted_extra`.
    """
    from superset.db_engine_specs.bigquery import BigQueryEngineSpec

    old = None
    new = json.dumps(
        {
            "credentials_info": {
                "project_id": "yellow-unicorn-314419",
                "private_key": "XXXXXXXXXX",
            },
        }
    )

    assert BigQueryEngineSpec.unmask_encrypted_extra(old, new) == json.dumps(
        {
            "credentials_info": {
                "project_id": "yellow-unicorn-314419",
                "private_key": "XXXXXXXXXX",
            },
        }
    )


def test_unmask_encrypted_extra_when_new_is_none() -> None:
    """
    Test that a `None` value for the new field works for `encrypted_extra`.
    """
    from superset.db_engine_specs.bigquery import BigQueryEngineSpec

    old = json.dumps(
        {
            "credentials_info": {
                "project_id": "black-sanctum-314419",
                "private_key": "SECRET",
            },
        }
    )
    new = None

    assert BigQueryEngineSpec.unmask_encrypted_extra(old, new) is None


def test_mask_encrypted_extra() -> None:
    """
    Test that the private key is masked when the database is edited.
    """
    from superset.db_engine_specs.bigquery import BigQueryEngineSpec

    config = json.dumps(
        {
            "credentials_info": {
                "project_id": "black-sanctum-314419",
                "private_key": "SECRET",
            },
        }
    )

    assert BigQueryEngineSpec.mask_encrypted_extra(config) == json.dumps(
        {
            "credentials_info": {
                "project_id": "black-sanctum-314419",
                "private_key": "XXXXXXXXXX",
            },
        }
    )


def test_mask_encrypted_extra_when_empty() -> None:
    """
    Test that the encrypted extra will return a none value if the field is empty.
    """
    from superset.db_engine_specs.bigquery import BigQueryEngineSpec

    assert BigQueryEngineSpec.mask_encrypted_extra(None) is None


def test_parse_error_message() -> None:
    """
    Test that we parse a received message and just extract the useful information.
    """
    from superset.db_engine_specs.bigquery import BigQueryEngineSpec

    message = 'bigquery error: 400 Syntax error: Table "case_detail_all_suites" must be qualified with a dataset (e.g. dataset.table).\n\n(job ID: ddf30b05-44e8-4fbf-aa29-40bfccaed886)\n\n     -----Query Job SQL Follows-----     \n\n    |    .    |    .    |    .    |\n   1:select * from case_detail_all_suites\n   2:LIMIT 1001\n    |    .    |    .    |    .    |'  # noqa: E501
    expected_result = 'bigquery error: 400 Syntax error: Table "case_detail_all_suites" must be qualified with a dataset (e.g. dataset.table).'  # noqa: E501
    assert (
        str(BigQueryEngineSpec.parse_error_exception(Exception(message)))
        == expected_result
    )


def test_parse_error_raises_exception() -> None:
    """
    Test that we handle any exception we might get from calling the
    parse_error_exception method.
    """
    from superset.db_engine_specs.bigquery import BigQueryEngineSpec

    message = 'bigquery error: 400 Syntax error: Table "case_detail_all_suites" must be qualified with a dataset (e.g. dataset.table).'  # noqa: E501
    message_2 = "6"
    expected_result = 'bigquery error: 400 Syntax error: Table "case_detail_all_suites" must be qualified with a dataset (e.g. dataset.table).'  # noqa: E501
    assert (
        str(BigQueryEngineSpec.parse_error_exception(Exception(message)))
        == expected_result
    )
    assert str(BigQueryEngineSpec.parse_error_exception(Exception(message_2))) == "6"


@pytest.mark.parametrize(
    "target_type,expected_result",
    [
        ("Date", "CAST('2019-01-02' AS DATE)"),
        ("DateTime", "CAST('2019-01-02T03:04:05.678900' AS DATETIME)"),
        ("TimeStamp", "CAST('2019-01-02T03:04:05.678900' AS TIMESTAMP)"),
        ("Time", "CAST('03:04:05.678900' AS TIME)"),
        ("UnknownType", None),
    ],
)
def test_convert_dttm(
    target_type: str,
    expected_result: Optional[str],
    dttm: datetime,
) -> None:
    """
    DB Eng Specs (bigquery): Test conversion to date time
    """
    from superset.db_engine_specs.bigquery import (
        BigQueryEngineSpec as spec,  # noqa: N813
    )

    assert_convert_dttm(spec, target_type, expected_result, dttm)


def test_get_default_catalog(mocker: MockerFixture) -> None:
    """
    Test that we get the default catalog from the connection URI.
    """
    from superset.db_engine_specs.bigquery import BigQueryEngineSpec
    from superset.models.core import Database

    mocker.patch.object(Database, "get_sqla_engine")
    get_client = mocker.patch.object(BigQueryEngineSpec, "_get_client")
    get_client().project = "project"

    database = Database(
        database_name="my_db",
        sqlalchemy_uri="bigquery://project",
    )
    assert BigQueryEngineSpec.get_default_catalog(database) == "project"

    database = Database(
        database_name="my_db",
        sqlalchemy_uri="bigquery:///project",
    )
    assert BigQueryEngineSpec.get_default_catalog(database) == "project"

    database = Database(
        database_name="my_db",
        sqlalchemy_uri="bigquery://",
    )
    assert BigQueryEngineSpec.get_default_catalog(database) == "project"


def test_adjust_engine_params_catalog_as_host() -> None:
    """
    Test passing a custom catalog.

    In this test, the original URI has the catalog as the host.
    """
    from superset.db_engine_specs.bigquery import BigQueryEngineSpec

    url = make_url("bigquery://project")

    uri = BigQueryEngineSpec.adjust_engine_params(url, {})[0]
    assert str(uri) == "bigquery://project"

    uri = BigQueryEngineSpec.adjust_engine_params(
        url,
        {},
        catalog="other-project",
    )[0]
    assert str(uri) == "bigquery://other-project/"


def test_adjust_engine_params_catalog_as_database() -> None:
    """
    Test passing a custom catalog.

    In this test, the original URI has the catalog as the database.
    """
    from superset.db_engine_specs.bigquery import BigQueryEngineSpec

    url = make_url("bigquery:///project")

    uri = BigQueryEngineSpec.adjust_engine_params(url, {})[0]
    assert str(uri) == "bigquery:///project"

    uri = BigQueryEngineSpec.adjust_engine_params(
        url,
        {},
        catalog="other-project",
    )[0]
    assert str(uri) == "bigquery://other-project/"


def test_adjust_engine_params_no_catalog() -> None:
    """
    Test passing a custom catalog.

    In this test, the original URI has no catalog.
    """
    from superset.db_engine_specs.bigquery import BigQueryEngineSpec

    url = make_url("bigquery://")

    uri = BigQueryEngineSpec.adjust_engine_params(url, {})[0]
    assert str(uri) == "bigquery://"

    uri = BigQueryEngineSpec.adjust_engine_params(
        url,
        {},
        catalog="other-project",
    )[0]
    assert str(uri) == "bigquery://other-project/"
