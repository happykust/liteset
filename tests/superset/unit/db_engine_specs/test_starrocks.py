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
"""Native Liteset port of the upstream sync StarRocksEngineSpec unit tests.

Flask-free: no flask, flask_appbuilder or werkzeug.
"""

from __future__ import annotations

from typing import Any, Optional
from unittest import mock

import pytest
from sqlalchemy import JSON, types
from sqlalchemy.engine.url import make_url

from superset.db_engine_specs.starrocks import (
    ARRAY,
    BITMAP,
    DOUBLE,
    HLL,
    LARGEINT,
    MAP,
    PERCENTILE,
    StarRocksEngineSpec,
    STRUCT,
    TINYINT,
)
from superset.utils.core import GenericDataType
from tests.superset.unit.db_engine_specs.utils import assert_column_spec


@pytest.mark.parametrize(
    "native_type,sqla_type,attrs,generic_type,is_dttm",
    [
        # Numeric
        ("tinyint", TINYINT, None, GenericDataType.NUMERIC, False),
        ("largeint", LARGEINT, None, GenericDataType.NUMERIC, False),
        ("decimal(38,18)", types.DECIMAL, None, GenericDataType.NUMERIC, False),
        ("double", DOUBLE, None, GenericDataType.NUMERIC, False),
        # String
        ("char(10)", types.CHAR, None, GenericDataType.STRING, False),
        ("varchar(65533)", types.VARCHAR, None, GenericDataType.STRING, False),
        ("binary", types.String, None, GenericDataType.STRING, False),
        # Complex type
        ("array<varchar(65533)>", ARRAY, None, GenericDataType.STRING, False),
        ("map<string,int>", MAP, None, GenericDataType.STRING, False),
        ("struct<int,string>", STRUCT, None, GenericDataType.STRING, False),
        ("json", JSON, None, GenericDataType.STRING, False),
        ("bitmap", BITMAP, None, GenericDataType.STRING, False),
        ("hll", HLL, None, GenericDataType.STRING, False),
        ("percentile", PERCENTILE, None, GenericDataType.STRING, False),
    ],
)
def test_get_column_spec(
    native_type: str,
    sqla_type: type[types.TypeEngine],
    attrs: Optional[dict[str, Any]],
    generic_type: GenericDataType,
    is_dttm: bool,
) -> None:
    assert_column_spec(
        StarRocksEngineSpec, native_type, sqla_type, attrs, generic_type, is_dttm
    )


@pytest.mark.parametrize(
    "sqlalchemy_uri,connect_args,return_schema,return_connect_args",
    [
        (
            "starrocks://user:password@host/db1",
            {"param1": "some_value"},
            "db1",
            {"param1": "some_value"},
        ),
        (
            "starrocks://user:password@host/catalog1.db1",
            {"param1": "some_value"},
            "catalog1.db1",
            {"param1": "some_value"},
        ),
    ],
)
def test_adjust_engine_params(
    sqlalchemy_uri: str,
    connect_args: dict[str, Any],
    return_schema: str,
    return_connect_args: dict[str, Any],
) -> None:
    url = make_url(sqlalchemy_uri)
    returned_url, returned_connect_args = StarRocksEngineSpec.adjust_engine_params(
        url, connect_args
    )
    assert returned_url.database == return_schema
    assert returned_connect_args == return_connect_args


def test_get_schema_from_engine_params() -> None:
    """
    Test the ``get_schema_from_engine_params`` method.
    """
    assert (
        StarRocksEngineSpec.get_schema_from_engine_params(
            make_url("starrocks://localhost:9030/hive.default"),
            {},
        )
        == "default"
    )

    assert (
        StarRocksEngineSpec.get_schema_from_engine_params(
            make_url("starrocks://localhost:9030/hive"),
            {},
        )
        is None
    )


def test_impersonation_username() -> None:
    """
    Test impersonation and make sure that `impersonate_user` leaves the URL
    unchanged and that `get_prequeries` returns the appropriate impersonation query.
    """
    database = mock.MagicMock()
    database.impersonate_user = True
    database.get_effective_user.return_value = "alice"

    assert StarRocksEngineSpec.impersonate_user(
        database,
        username="alice",
        user_token=None,
        url=make_url("starrocks://service_user@localhost:9030/hive.default"),
        engine_kwargs={},
    ) == (make_url("starrocks://service_user@localhost:9030/hive.default"), {})

    assert StarRocksEngineSpec.get_prequeries(database) == [
        'EXECUTE AS "alice" WITH NO REVERT;'
    ]


def test_impersonation_disabled() -> None:
    """
    Test that impersonation is not applied when the feature is disabled in
    `impersonate_user` and `get_prequeries`.
    """
    database = mock.MagicMock()
    database.impersonate_user = False
    database.get_effective_user.return_value = "alice"

    assert StarRocksEngineSpec.impersonate_user(
        database,
        username="alice",
        user_token=None,
        url=make_url("starrocks://service_user@localhost:9030/hive.default"),
        engine_kwargs={},
    ) == (make_url("starrocks://service_user@localhost:9030/hive.default"), {})

    assert StarRocksEngineSpec.get_prequeries(database) == []
