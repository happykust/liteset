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
"""Unit tests for ``superset.models.core.Database`` (Flask-free port).

Adapted 1:1 in intent from ``tests/unit_tests/models/core_test.py``.

Liteset adaptations vs the upstream Flask suite:
  * ``TableColumn(database=...)`` is no longer a constructor arg — the port
    exposes ``TableColumn.database`` as a read-only property derived from the
    parent ``table``; the test attaches the column to a ``SqlaTable``.
  * ``Database.get_db_engine_spec`` is an ``lru_cache``'d classmethod backed
    by ``superset.db_engine_specs.load_engine_specs``; the test patches that
    loader and clears the cache.
  * ``Database.get_default_schema`` delegates to the engine spec (not the raw
    inspector), so the spec is patched via ``PropertyMock``.
  * The epoch ``dttm_sql_literal`` cases use ``datetime.timestamp()`` (local
    tz, identical to upstream); the test pins ``TZ=UTC`` so the expected
    epoch values match the upstream UTC CI.

Tests for upstream-only methods that the port does not expose
(``_get_sqla_engine``, ``add_database_to_signature``, ``get_all_catalog_names``,
``get_all_schema_names``, ``purge_oauth2_tokens``, the ``ENGINE_CONTEXT_MANAGER``
hook, and the OAuth2 dance inside ``get_raw_connection`` / ``get_sqla_engine``)
are marked ``@pytest.mark.skip`` with a factual reason, but the full upstream
test body is retained verbatim (mechanically adapted) so the lost coverage is
documented and the test can be un-skipped if the port grows the method.
"""

from __future__ import annotations

import functools
import time
from datetime import datetime
from unittest.mock import patch, PropertyMock

import pytest
from pytest_mock import MockerFixture
from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    select,
    Table as SqlalchemyTable,
)
from sqlalchemy.engine.reflection import Inspector
from sqlalchemy.engine.url import make_url  # noqa: F401
from sqlalchemy.sql import Select

from superset.errors import SupersetErrorType  # noqa: F401
from superset.exceptions import OAuth2Error, OAuth2RedirectError  # noqa: F401
from superset.models.connectors import SqlaTable, TableColumn
from superset.models.core import Database, DatabaseUserOAuth2Tokens  # noqa: F401
from superset.sql.parse import LimitMethod, Table
from superset.utils import json
from superset.utils.feature_flags import feature_flag_manager


def with_feature_flags(**mock_feature_flags):
    """
    Flask-free port of the upstream ``with_feature_flags`` decorator.

    Mirrors ``tests/unit_tests/conftest.py`` but resolves the
    ``feature_flag_manager`` from :mod:`superset.utils.feature_flags`
    (the Liteset manager) so this module imports no Flask machinery.
    """

    def mock_get_feature_flags():
        feature_flags = feature_flag_manager._feature_flags or {}
        return {**feature_flags, **mock_feature_flags}

    def decorate(test_fn):
        def wrapper(*args, **kwargs):
            with patch.object(
                feature_flag_manager,
                "get_feature_flags",
                side_effect=mock_get_feature_flags,
            ):
                test_fn(*args, **kwargs)

        return functools.update_wrapper(wrapper, test_fn)

    return decorate


# sample config for OAuth2 tests
oauth2_client_info = {
    "oauth2_client_info": {
        "id": "my_client_id",
        "secret": "my_client_secret",
        "authorization_request_uri": "https://abcd1234.snowflakecomputing.com/oauth/authorize",
        "token_request_uri": "https://abcd1234.snowflakecomputing.com/oauth/token-request",
        "scope": "refresh_token session:role:USERADMIN",
    }
}


@pytest.fixture
def query() -> Select:
    """
    A nested query fixture used to test query optimization.
    """
    metadata = MetaData()
    some_table = SqlalchemyTable(
        "some_table",
        metadata,
        Column("a", Integer),
        Column("b", Integer),
        Column("c", Integer),
    )

    inner_select = select(some_table.c.a, some_table.c.b, some_table.c.c)
    outer_select = select(inner_select.c.a, inner_select.c.b).where(
        inner_select.c.a > 1,
        inner_select.c.b == 2,
    )

    return outer_select


def test_get_metrics(mocker: MockerFixture) -> None:
    """
    Tests for ``get_metrics``.
    """
    from superset.db_engine_specs.base import MetricType
    from superset.db_engine_specs.sqlite import SqliteEngineSpec
    from superset.models.core import Database

    database = Database(database_name="my_database", sqlalchemy_uri="sqlite://")
    assert database.get_metrics(Table("table")) == [
        {
            "expression": "COUNT(*)",
            "metric_name": "count",
            "metric_type": "count",
            "verbose_name": "COUNT(*)",
        }
    ]

    class CustomSqliteEngineSpec(SqliteEngineSpec):
        @classmethod
        def get_metrics(
            cls,
            database: Database,
            inspector: Inspector,
            table: Table,
        ) -> list[MetricType]:
            return [
                {
                    "expression": "COUNT(DISTINCT user_id)",
                    "metric_name": "count_distinct_user_id",
                    "metric_type": "count_distinct",
                    "verbose_name": "COUNT(DISTINCT user_id)",
                },
            ]

    database.get_db_engine_spec = mocker.MagicMock(return_value=CustomSqliteEngineSpec)
    assert database.get_metrics(Table("table")) == [
        {
            "expression": "COUNT(DISTINCT user_id)",
            "metric_name": "count_distinct_user_id",
            "metric_type": "count_distinct",
            "verbose_name": "COUNT(DISTINCT user_id)",
        },
    ]


def test_get_db_engine_spec(mocker: MockerFixture) -> None:
    """
    Tests for ``get_db_engine_spec``.
    """
    from superset.db_engine_specs import BaseEngineSpec
    from superset.models.core import Database

    # pylint: disable=abstract-method
    class PostgresDBEngineSpec(BaseEngineSpec):
        """
        A DB engine spec with drivers and a default driver.
        """

        engine = "postgresql"
        engine_aliases = {"postgres"}
        drivers = {
            "psycopg2": "The default Postgres driver",
            "asyncpg": "An async Postgres driver",
        }
        default_driver = "psycopg2"

    # pylint: disable=abstract-method
    class OldDBEngineSpec(BaseEngineSpec):
        """
        And old DB engine spec without drivers nor a default driver.
        """

        engine = "mysql"

    load_engine_specs = mocker.patch("superset.db_engine_specs.load_engine_specs")
    load_engine_specs.return_value = [
        PostgresDBEngineSpec,
        OldDBEngineSpec,
    ]
    # ``get_db_engine_spec`` is lru_cache'd on the class; reset between runs.
    Database.get_db_engine_spec.cache_clear()

    try:
        assert (
            Database(database_name="db", sqlalchemy_uri="postgresql://").db_engine_spec
            == PostgresDBEngineSpec
        )
        assert (
            Database(
                database_name="db", sqlalchemy_uri="postgresql+psycopg2://"
            ).db_engine_spec
            == PostgresDBEngineSpec
        )
        assert (
            Database(
                database_name="db", sqlalchemy_uri="postgresql+asyncpg://"
            ).db_engine_spec
            == PostgresDBEngineSpec
        )
        assert (
            Database(
                database_name="db", sqlalchemy_uri="postgresql+fancynewdriver://"
            ).db_engine_spec
            == PostgresDBEngineSpec
        )
        assert (
            Database(database_name="db", sqlalchemy_uri="mysql://").db_engine_spec
            == OldDBEngineSpec
        )
        assert (
            Database(
                database_name="db", sqlalchemy_uri="mysql+mysqlconnector://"
            ).db_engine_spec
            == OldDBEngineSpec
        )
        assert (
            Database(
                database_name="db", sqlalchemy_uri="mysql+fancynewdriver://"
            ).db_engine_spec
            == OldDBEngineSpec
        )
    finally:
        Database.get_db_engine_spec.cache_clear()


@pytest.mark.parametrize(
    "dttm,col,database,result",
    [
        (
            datetime(2023, 1, 1, 1, 23, 45, 600000),
            TableColumn(python_date_format="epoch_s"),
            Database(),
            "1672536225",
        ),
        (
            datetime(2023, 1, 1, 1, 23, 45, 600000),
            TableColumn(python_date_format="epoch_ms"),
            Database(),
            "1672536225000",
        ),
        (
            datetime(2023, 1, 1, 1, 23, 45, 600000),
            TableColumn(python_date_format="%Y-%m-%d"),
            Database(),
            "'2023-01-01'",
        ),
        (
            datetime(2023, 1, 1, 1, 23, 45, 600000),
            TableColumn(column_name="ds"),
            Database(
                extra=json.dumps(
                    {
                        "python_date_format_by_column_name": {
                            "ds": "%Y-%m-%d",
                        },
                    },
                ),
                sqlalchemy_uri="foo://",
            ),
            "'2023-01-01'",
        ),
        (
            datetime(2023, 1, 1, 1, 23, 45, 600000),
            TableColumn(),
            Database(sqlalchemy_uri="foo://"),
            "'2023-01-01 01:23:45.600000'",
        ),
        (
            datetime(2023, 1, 1, 1, 23, 45, 600000),
            TableColumn(type="TimeStamp"),
            Database(sqlalchemy_uri="trino://"),
            "TIMESTAMP '2023-01-01 01:23:45.600000'",
        ),
    ],
)
def test_dttm_sql_literal(
    dttm: datetime,
    col: TableColumn,
    database: Database,
    result: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ``dttm_sql_literal`` uses ``datetime.timestamp()`` for epoch formats,
    # which is timezone-sensitive for naive datetimes; pin UTC so the expected
    # epoch values match (identical behaviour to the upstream UTC CI).
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    assert SqlaTable(database=database).dttm_sql_literal(dttm, col) == result


def test_table_column_database() -> None:
    database = Database(database_name="db")
    table = SqlaTable(table_name="t", database=database)
    column = TableColumn(column_name="c")
    column.table = table
    assert column.database is database


def test_get_prequeries(mocker: MockerFixture) -> None:
    """
    Tests for ``get_prequeries`` (driven through ``get_raw_connection``).
    """
    mocker.patch.object(Database, "get_sqla_engine")
    db_engine_spec = mocker.patch.object(
        Database, "db_engine_spec", new_callable=PropertyMock
    )
    db_engine_spec.return_value.get_prequeries.return_value = ["set a=1", "set b=2"]

    database = Database(database_name="db")
    with database.get_raw_connection() as conn:
        conn.cursor().execute.assert_has_calls(
            [mocker.call("set a=1"), mocker.call("set b=2")]
        )


def test_catalog_cache() -> None:
    """
    Test the catalog cache.
    """
    database = Database(
        database_name="db",
        sqlalchemy_uri="sqlite://",
        extra=json.dumps({"metadata_cache_timeout": {"catalog_cache_timeout": 10}}),
    )

    assert database.catalog_cache_enabled
    assert database.catalog_cache_timeout == 10


def test_get_default_catalog() -> None:
    """
    Test the `get_default_catalog` method.
    """
    database = Database(
        database_name="db",
        sqlalchemy_uri="postgresql://user:password@host:5432/examples",
    )

    assert database.get_default_catalog() == "examples"


def test_get_default_schema(mocker: MockerFixture) -> None:
    """
    Test the `get_default_schema` method.

    The port delegates to the engine spec's ``get_default_schema`` rather than
    reading the raw inspector's ``default_schema_name``.
    """
    database = Database(
        database_name="db",
        sqlalchemy_uri="postgresql://user:password@host:5432/examples",
    )

    db_engine_spec = mocker.patch.object(
        Database, "db_engine_spec", new_callable=PropertyMock
    )
    db_engine_spec.return_value.get_default_schema.return_value = "public"

    assert database.get_default_schema("examples") == "public"
    db_engine_spec.return_value.get_default_schema.assert_called_with(
        database, "examples"
    )


@pytest.mark.skip(
    reason="Port has no Database.get_all_catalog_names; catalog enumeration is "
    "served by the async DAO layer, not a sync model method."
)
def test_get_all_catalog_names(mocker: MockerFixture) -> None:
    """
    Test the `get_all_catalog_names` method.
    """
    database = Database(
        database_name="db",
        sqlalchemy_uri="postgresql://user:password@host:5432/examples",
    )

    get_inspector = mocker.patch.object(database, "get_inspector")
    with get_inspector() as inspector:
        inspector.bind.execute.return_value = [("examples",), ("other",)]

    assert database.get_all_catalog_names(force=True) == {"examples", "other"}
    get_inspector.assert_called_with(ssh_tunnel=None)


@pytest.mark.skip(
    reason="Port has no Database.get_all_schema_names; schema enumeration is "
    "served by the async DAO layer, not a sync model method."
)
def test_get_all_schema_names_needs_oauth2(mocker: MockerFixture) -> None:
    """
    Test the `get_all_schema_names` method when OAuth2 is needed.
    """
    database = Database(
        database_name="db",
        sqlalchemy_uri="snowflake://:@abcd1234.snowflakecomputing.com/db",
        encrypted_extra=json.dumps(oauth2_client_info),
    )

    class DriverSpecificError(Exception):
        """
        A custom exception that is raised by the Snowflake driver.
        """

    mocker.patch.object(
        database.db_engine_spec,
        "oauth2_exception",
        DriverSpecificError,
    )
    mocker.patch.object(
        database.db_engine_spec,
        "get_schema_names",
        side_effect=DriverSpecificError("User needs to authenticate"),
    )
    mocker.patch.object(database, "get_inspector")
    user = mocker.MagicMock()
    user.id = 42
    mocker.patch("superset.db_engine_specs.base.g", user=user)

    with pytest.raises(OAuth2RedirectError) as excinfo:
        database.get_all_schema_names()

    assert excinfo.value.message == "You don't have permission to access the data."
    assert excinfo.value.error.error_type == SupersetErrorType.OAUTH2_REDIRECT


@pytest.mark.skip(
    reason="Port has no Database.get_all_catalog_names; catalog enumeration is "
    "served by the async DAO layer, not a sync model method."
)
def test_get_all_catalog_names_needs_oauth2(mocker: MockerFixture) -> None:
    """
    Test the `get_all_catalog_names` method when OAuth2 is needed.
    """
    database = Database(
        database_name="db",
        sqlalchemy_uri="snowflake://:@abcd1234.snowflakecomputing.com/db",
        encrypted_extra=json.dumps(oauth2_client_info),
    )

    class DriverSpecificError(Exception):
        """
        A custom exception that is raised by the Snowflake driver.
        """

    mocker.patch.object(
        database.db_engine_spec,
        "oauth2_exception",
        DriverSpecificError,
    )
    mocker.patch.object(
        database.db_engine_spec,
        "get_catalog_names",
        side_effect=DriverSpecificError("User needs to authenticate"),
    )
    mocker.patch.object(database, "get_inspector")
    user = mocker.MagicMock()
    user.id = 42
    mocker.patch("superset.db_engine_specs.base.g", user=user)

    with pytest.raises(OAuth2RedirectError) as excinfo:
        database.get_all_catalog_names()

    assert excinfo.value.message == "You don't have permission to access the data."
    assert excinfo.value.error.error_type == SupersetErrorType.OAUTH2_REDIRECT


@pytest.mark.skip(
    reason="Port has no Database._get_sqla_engine; engine creation lives in "
    "superset.utils.database.get_sync_engine."
)
def test_get_sqla_engine(mocker: MockerFixture) -> None:
    """
    Test `_get_sqla_engine`.
    """
    from superset.models.core import Database

    user = mocker.MagicMock()
    user.email = "alice.doe@example.org"
    mocker.patch(
        "superset.models.core.security_manager.find_user",
        return_value=user,
    )
    mocker.patch("superset.models.core.get_username", return_value="alice")

    create_engine = mocker.patch("superset.models.core.create_engine")

    database = Database(database_name="my_db", sqlalchemy_uri="trino://")
    database._get_sqla_engine(nullpool=False)

    create_engine.assert_called_with(
        make_url("trino:///"),
        connect_args={"source": "Apache Superset"},
    )


@pytest.mark.skip(
    reason="Port has no Database._get_sqla_engine; user impersonation is applied "
    "inside superset.utils.database.get_sync_engine."
)
def test_get_sqla_engine_user_impersonation(mocker: MockerFixture) -> None:
    """
    Test user impersonation in `_get_sqla_engine`.
    """
    from superset.models.core import Database

    user = mocker.MagicMock()
    user.email = "alice.doe@example.org"
    mocker.patch(
        "superset.models.core.security_manager.find_user",
        return_value=user,
    )
    mocker.patch("superset.models.core.get_username", return_value="alice")

    create_engine = mocker.patch("superset.models.core.create_engine")

    database = Database(
        database_name="my_db",
        sqlalchemy_uri="trino://",
        impersonate_user=True,
    )
    database._get_sqla_engine(nullpool=False)

    create_engine.assert_called_with(
        make_url("trino:///"),
        connect_args={"user": "alice", "source": "Apache Superset"},
    )


@pytest.mark.skip(reason="Port has no Database.add_database_to_signature helper.")
def test_add_database_to_signature() -> None:
    """Test `add_database_to_signature`."""
    args = ["param1", "param2"]

    def func_without_db(param1, param2):
        pass

    def func_with_db_start(database, param1, param2):
        pass

    def func_with_db_end(param1, param2, database):
        pass

    database = Database(
        database_name="my_db",
        sqlalchemy_uri="trino://",
        impersonate_user=True,
    )
    args1 = database.add_database_to_signature(func_without_db, args.copy())
    assert args1 == ["param1", "param2"]
    args2 = database.add_database_to_signature(func_with_db_start, args.copy())
    assert args2 == [database, "param1", "param2"]
    args3 = database.add_database_to_signature(func_with_db_end, args.copy())
    assert args3 == ["param1", "param2", database]


@pytest.mark.skip(
    reason="Port has no Database._get_sqla_engine; impersonation-with-email-prefix "
    "is handled inside superset.utils.database.get_sync_engine."
)
@with_feature_flags(IMPERSONATE_WITH_EMAIL_PREFIX=True)
def test_get_sqla_engine_user_impersonation_email(mocker: MockerFixture) -> None:
    """
    Test user impersonation in `_get_sqla_engine` with `username_from_email`.
    """
    from superset.models.core import Database

    user = mocker.MagicMock()
    user.email = "alice.doe@example.org"
    mocker.patch(
        "superset.models.core.security_manager.find_user",
        return_value=user,
    )
    mocker.patch("superset.models.core.get_username", return_value="alice")

    create_engine = mocker.patch("superset.models.core.create_engine")

    database = Database(
        database_name="my_db",
        sqlalchemy_uri="trino://",
        impersonate_user=True,
    )
    database._get_sqla_engine(nullpool=False)

    create_engine.assert_called_with(
        make_url("trino:///"),
        connect_args={"user": "alice.doe", "source": "Apache Superset"},
    )


def test_is_oauth2_enabled() -> None:
    """
    Test the `is_oauth2_enabled` method.
    """
    database = Database(
        database_name="db",
        sqlalchemy_uri="postgresql://user:password@host:5432/examples",
    )

    assert not database.is_oauth2_enabled()

    database.encrypted_extra = json.dumps(oauth2_client_info)
    assert database.is_oauth2_enabled()


def test_get_oauth2_config(mocker: MockerFixture) -> None:
    """
    Test the `get_oauth2_config` method.
    """
    # Upstream's redirect_uri comes from ``url_for(..., _external=True)`` which
    # the Flask test app resolves to ``http://example.com``. The port derives
    # it from the deployment base URL; pin that callback to the exact upstream
    # value so the full dict (incl. redirect_uri) is asserted 1:1.
    mocker.patch(
        "superset.utils.oauth2._default_oauth2_redirect_uri",
        return_value="http://example.com/api/v1/database/oauth2/",
    )

    database = Database(
        database_name="db",
        sqlalchemy_uri="postgresql://user:password@host:5432/examples",
    )

    assert database.get_oauth2_config() is None

    database.encrypted_extra = json.dumps(oauth2_client_info)
    assert database.get_oauth2_config() == {
        "id": "my_client_id",
        "secret": "my_client_secret",
        "authorization_request_uri": "https://abcd1234.snowflakecomputing.com/oauth/authorize",
        "token_request_uri": "https://abcd1234.snowflakecomputing.com/oauth/token-request",
        "scope": "refresh_token session:role:USERADMIN",
        "redirect_uri": "http://example.com/api/v1/database/oauth2/",
        "request_content_type": "json",
    }


@pytest.mark.skip(
    reason="Port's get_raw_connection omits the OAuth2 re-auth wrapper "
    "(check_for_oauth2 is async-only) and has no Database._get_sqla_engine."
)
def test_raw_connection_oauth_engine(mocker: MockerFixture) -> None:
    """
    Test that we can start OAuth2 from `raw_connection()` errors.

    With OAuth2, some databases will raise an exception when the engine is first
    created (eg, BigQuery). Others, like, Snowflake, when the connection is
    created. And finally, GSheets will raise an exception when the query is
    executed.

    This tests verifies that when calling `raw_connection()` the OAuth2 flow is
    triggered when the engine is created.
    """
    g = mocker.patch("superset.db_engine_specs.base.g")
    g.user = mocker.MagicMock()
    g.user.id = 42

    database = Database(
        id=1,
        database_name="my_db",
        sqlalchemy_uri="sqlite://",
        encrypted_extra=json.dumps(oauth2_client_info),
    )
    database.db_engine_spec.oauth2_exception = OAuth2Error  # type: ignore
    _get_sqla_engine = mocker.patch.object(database, "_get_sqla_engine")
    _get_sqla_engine.side_effect = OAuth2Error("OAuth2 required")

    with pytest.raises(OAuth2RedirectError) as excinfo:
        with database.get_raw_connection() as conn:
            conn.cursor()
    assert str(excinfo.value) == "You don't have permission to access the data."


@pytest.mark.skip(
    reason="Port's get_raw_connection omits the OAuth2 re-auth wrapper "
    "(check_for_oauth2 is async-only)."
)
def test_raw_connection_oauth_connection(mocker: MockerFixture) -> None:
    """
    Test that we can start OAuth2 from `raw_connection()` errors.

    With OAuth2, some databases will raise an exception when the engine is first
    created (eg, BigQuery). Others, like, Snowflake, when the connection is
    created. And finally, GSheets will raise an exception when the query is
    executed.

    This tests verifies that when calling `raw_connection()` the OAuth2 flow is
    triggered when the connection is created.
    """
    g = mocker.patch("superset.db_engine_specs.base.g")
    g.user = mocker.MagicMock()
    g.user.id = 42

    database = Database(
        id=1,
        database_name="my_db",
        sqlalchemy_uri="sqlite://",
        encrypted_extra=json.dumps(oauth2_client_info),
    )
    database.db_engine_spec.oauth2_exception = OAuth2Error  # type: ignore
    get_sqla_engine = mocker.patch.object(database, "get_sqla_engine")
    get_sqla_engine().__enter__().raw_connection.side_effect = OAuth2Error(
        "OAuth2 required"
    )

    with pytest.raises(OAuth2RedirectError) as excinfo:
        with database.get_raw_connection() as conn:
            conn.cursor()
    assert str(excinfo.value) == "You don't have permission to access the data."


@pytest.mark.skip(
    reason="Port's get_raw_connection omits the OAuth2 re-auth wrapper "
    "(check_for_oauth2 is async-only)."
)
def test_raw_connection_oauth_execute(mocker: MockerFixture) -> None:
    """
    Test that we can start OAuth2 from `raw_connection()` errors.

    With OAuth2, some databases will raise an exception when the engine is first
    created (eg, BigQuery). Others, like, Snowflake, when the connection is
    created. And finally, GSheets will raise an exception when the query is
    executed.

    This tests verifies that when calling `raw_connection()` the OAuth2 flow is
    triggered when the connection is created.
    """
    g = mocker.patch("superset.db_engine_specs.base.g")
    g.user = mocker.MagicMock()
    g.user.id = 42

    database = Database(
        id=1,
        database_name="my_db",
        sqlalchemy_uri="sqlite://",
        encrypted_extra=json.dumps(oauth2_client_info),
    )
    database.db_engine_spec.oauth2_exception = OAuth2Error  # type: ignore
    get_sqla_engine = mocker.patch.object(database, "get_sqla_engine")
    get_sqla_engine().__enter__().raw_connection().cursor().execute.side_effect = (
        OAuth2Error("OAuth2 required")
    )

    with pytest.raises(OAuth2RedirectError) as excinfo:  # noqa: PT012
        with database.get_raw_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
    assert str(excinfo.value) == "You don't have permission to access the data."


def test_get_schema_access_for_file_upload() -> None:
    """
    Test the `get_schema_access_for_file_upload` method.
    """
    # Skip if gsheets dialect is not available (Shillelagh not installed)
    try:
        from sqlalchemy import create_engine

        create_engine("gsheets://")
    except Exception:
        pytest.skip("gsheets:// dialect not available (Shillelagh not installed)")

    database = Database(
        database_name="first-database",
        sqlalchemy_uri="gsheets://",
        extra=json.dumps(
            {
                "metadata_params": {},
                "engine_params": {},
                "metadata_cache_timeout": {},
                "schemas_allowed_for_file_upload": '["public"]',
            }
        ),
    )

    assert database.get_schema_access_for_file_upload() == {"public"}


@pytest.mark.skip(
    reason="Port has no ENGINE_CONTEXT_MANAGER config hook nor "
    "Database._get_sqla_engine; engine creation is handled by "
    "superset.utils.database.get_sync_engine."
)
def test_engine_context_manager(mocker: MockerFixture) -> None:
    """
    Test the engine context manager.
    """
    from unittest.mock import MagicMock

    engine_context_manager = MagicMock()
    # Upstream patches ``current_app.config["ENGINE_CONTEXT_MANAGER"]``; the
    # port has no such config hook, so the upstream Flask config patch is
    # represented here by the manager mock that the port would consult.
    mocker.patch(
        "superset.models.core.config",
        {"ENGINE_CONTEXT_MANAGER": engine_context_manager},
        create=True,
    )
    _get_sqla_engine = mocker.patch.object(Database, "_get_sqla_engine")

    database = Database(database_name="my_db", sqlalchemy_uri="trino://")
    with database.get_sqla_engine("catalog", "schema"):
        pass

    engine_context_manager.assert_called_once_with(database, "catalog", "schema")
    engine_context_manager().__enter__.assert_called_once()
    engine_context_manager().__exit__.assert_called_once_with(None, None, None)
    _get_sqla_engine.assert_called_once_with(
        catalog="catalog",
        schema="schema",
        nullpool=True,
        source=None,
        sqlalchemy_uri="trino://",
    )


@pytest.mark.skip(
    reason="Port has no Database._get_sqla_engine; the OAuth2 dance on "
    "create_engine failure is exercised in superset.utils.database."
)
def test_engine_oauth2(mocker: MockerFixture) -> None:
    """
    Test that we handle OAuth2 when `create_engine` fails.
    """
    database = Database(database_name="my_db", sqlalchemy_uri="trino://")
    mocker.patch.object(database, "_get_sqla_engine", side_effect=Exception)
    mocker.patch.object(database, "is_oauth2_enabled", return_value=True)
    mocker.patch.object(database.db_engine_spec, "needs_oauth2", return_value=True)
    start_oauth2_dance = mocker.patch.object(
        database.db_engine_spec,
        "start_oauth2_dance",
        side_effect=OAuth2Error("OAuth2 required"),
    )

    with pytest.raises(OAuth2Error):
        with database.get_sqla_engine("catalog", "schema"):
            pass

    start_oauth2_dance.assert_called_with(database)


@pytest.mark.skip(
    reason="Port has no Database.purge_oauth2_tokens and no sync session "
    "fixture; OAuth2 token purging lives in the async DAO layer."
)
def test_purge_oauth2_tokens(session) -> None:
    """
    Test the `purge_oauth2_tokens` method.
    """
    from superset.models.security import User

    Database.metadata.create_all(session.get_bind())  # pylint: disable=no-member

    user = User(
        first_name="Alice",
        last_name="Doe",
        email="adoe@example.org",
        username="adoe",
    )
    session.add(user)
    session.flush()

    database1 = Database(database_name="my_oauth2_db", sqlalchemy_uri="sqlite://")
    database2 = Database(database_name="my_other_oauth2_db", sqlalchemy_uri="sqlite://")
    session.add_all([database1, database2])
    session.flush()

    tokens = [
        DatabaseUserOAuth2Tokens(
            user_id=user.id,
            database_id=database1.id,
            access_token="my_access_token",  # noqa: S106
            access_token_expiration=datetime(2023, 1, 1),
            refresh_token="my_refresh_token",  # noqa: S106
        ),
        DatabaseUserOAuth2Tokens(
            user_id=user.id,
            database_id=database2.id,
            access_token="my_other_access_token",  # noqa: S106
            access_token_expiration=datetime(2024, 1, 1),
            refresh_token="my_other_refresh_token",  # noqa: S106
        ),
    ]
    session.add_all(tokens)
    session.flush()

    assert len(session.query(DatabaseUserOAuth2Tokens).all()) == 2

    token = (
        session.query(DatabaseUserOAuth2Tokens)
        .filter_by(database_id=database1.id)
        .one()
    )
    assert token.user_id == user.id
    assert token.database_id == database1.id
    assert token.access_token == "my_access_token"  # noqa: S105
    assert token.access_token_expiration == datetime(2023, 1, 1)
    assert token.refresh_token == "my_refresh_token"  # noqa: S105

    database1.purge_oauth2_tokens()

    # confirm token was deleted
    token = (
        session.query(DatabaseUserOAuth2Tokens)
        .filter_by(database_id=database1.id)
        .one_or_none()
    )
    assert token is None

    # make sure other DB tokens weren't deleted
    token = (
        session.query(DatabaseUserOAuth2Tokens)
        .filter_by(database_id=database2.id)
        .one()
    )
    assert token is not None

    # make sure database was not deleted... just in case
    database = session.query(Database).filter_by(id=database1.id).one()
    assert database.name == "my_oauth2_db"


def test_compile_sqla_query_no_optimization(query: Select) -> None:
    """
    Test the `compile_sqla_query` method.
    """
    from superset.models.core import Database

    database = Database(
        database_name="db",
        sqlalchemy_uri="sqlite://",
    )

    space = " "
    assert (
        database.compile_sqla_query(query, is_virtual=True)
        == f"""SELECT anon_1.a, anon_1.b{space}
FROM (SELECT some_table.a AS a, some_table.b AS b, some_table.c AS c{space}
FROM some_table) AS anon_1{space}
WHERE anon_1.a > 1 AND anon_1.b = 2"""  # noqa: S608
    )


def test_compile_sqla_query(query: Select, mocker: MockerFixture) -> None:
    """
    Test the `compile_sqla_query` method with OPTIMIZE_SQL enabled.
    """
    from superset.models.core import Database

    mocker.patch(
        "superset.utils.feature_flags.feature_flag_manager",
        mocker.MagicMock(is_feature_enabled=lambda feature: feature == "OPTIMIZE_SQL"),
    )

    database = Database(
        database_name="db",
        sqlalchemy_uri="sqlite://",
    )

    assert (
        database.compile_sqla_query(query, is_virtual=True)
        == """SELECT
  anon_1.a,
  anon_1.b
FROM (
  SELECT
    some_table.a AS a,
    some_table.b AS b,
    some_table.c AS c
  FROM some_table
  WHERE
    some_table.a > 1 AND some_table.b = 2
) AS anon_1
WHERE
  TRUE AND TRUE"""
    )


def test_get_all_table_names_in_schema(mocker: MockerFixture) -> None:
    """
    Test the `get_all_table_names_in_schema` method (async in the port).
    """
    import asyncio

    database = Database(
        database_name="db",
        sqlalchemy_uri="postgresql://user:password@host:5432/examples",
    )

    mocker.patch.object(database, "get_inspector")
    get_table_names = mocker.patch(
        "superset.db_engine_specs.postgres.PostgresEngineSpec.get_table_names"
    )
    get_table_names.return_value = {"first_table", "second_table", "third_table"}

    tables_list = asyncio.run(
        database.get_all_table_names_in_schema(
            catalog="examples",
            schema="public",
        )
    )
    assert sorted(tables_list) == sorted(
        {
            ("first_table", "public", "examples"),
            ("second_table", "public", "examples"),
            ("third_table", "public", "examples"),
        }
    )


def test_get_all_view_names_in_schema(mocker: MockerFixture) -> None:
    """
    Test the `get_all_view_names_in_schema` method (async in the port).
    """
    import asyncio

    database = Database(
        database_name="db",
        sqlalchemy_uri="postgresql://user:password@host:5432/examples",
    )

    mocker.patch.object(database, "get_inspector")
    get_view_names = mocker.patch(
        "superset.db_engine_specs.base.BaseEngineSpec.get_view_names"
    )
    get_view_names.return_value = {"first_view", "second_view", "third_view"}

    views_list = asyncio.run(
        database.get_all_view_names_in_schema(
            catalog="examples",
            schema="public",
        )
    )
    assert sorted(views_list) == sorted(
        {
            ("first_view", "public", "examples"),
            ("second_view", "public", "examples"),
            ("third_view", "public", "examples"),
        }
    )


@pytest.mark.parametrize(
    "sql, limit, force, method, expected",
    [
        (
            "SELECT * FROM table",
            100,
            False,
            LimitMethod.FORCE_LIMIT,
            "SELECT\n  *\nFROM table\nLIMIT 100",
        ),
        (
            "SELECT * FROM table LIMIT 100",
            10,
            False,
            LimitMethod.FORCE_LIMIT,
            "SELECT\n  *\nFROM table\nLIMIT 10",
        ),
        (
            "SELECT * FROM table LIMIT 10",
            100,
            False,
            LimitMethod.FORCE_LIMIT,
            "SELECT\n  *\nFROM table\nLIMIT 10",
        ),
        (
            "SELECT * FROM table LIMIT 10",
            100,
            True,
            LimitMethod.FORCE_LIMIT,
            "SELECT\n  *\nFROM table\nLIMIT 100",
        ),
        (
            "SELECT * FROM a  \t \n   ; \t  \n  ",
            1000,
            False,
            LimitMethod.FORCE_LIMIT,
            "SELECT\n  *\nFROM a\nLIMIT 1000",
        ),
        (
            "SELECT 'LIMIT 777'",
            1000,
            False,
            LimitMethod.FORCE_LIMIT,
            "SELECT\n  'LIMIT 777'\nLIMIT 1000",
        ),
        (
            "SELECT * FROM table",
            1000,
            False,
            LimitMethod.FETCH_MANY,
            "SELECT\n  *\nFROM table",
        ),
        (
            "SELECT * FROM (SELECT * FROM a LIMIT 10) LIMIT 9999",
            1000,
            False,
            LimitMethod.FORCE_LIMIT,
            """SELECT
  *
FROM (
  SELECT
    *
  FROM a
  LIMIT 10
)
LIMIT 1000""",
        ),
        (
            """
SELECT
    'LIMIT 777' AS a
  , b
FROM
    table
LIMIT 99990""",
            1000,
            None,
            LimitMethod.FORCE_LIMIT,
            "SELECT\n  'LIMIT 777' AS a,\n  b\nFROM table\nLIMIT 1000",
        ),
        (
            """
SELECT
    'LIMIT 777' AS a
  , b
FROM
table
LIMIT         99990            ;""",
            1000,
            None,
            LimitMethod.FORCE_LIMIT,
            "SELECT\n  'LIMIT 777' AS a,\n  b\nFROM table\nLIMIT 1000",
        ),
        (
            """
SELECT
    'LIMIT 777' AS a
  , b
FROM
table
LIMIT 99990, 999999""",
            1000,
            None,
            LimitMethod.FORCE_LIMIT,
            "SELECT\n  'LIMIT 777' AS a,\n  b\nFROM table\nLIMIT 1000\nOFFSET 99990",
        ),
        (
            """
SELECT
    'LIMIT 777' AS a
  , b
FROM
table
LIMIT 99990
OFFSET 999999""",
            1000,
            None,
            LimitMethod.FORCE_LIMIT,
            "SELECT\n  'LIMIT 777' AS a,\n  b\nFROM table\nLIMIT 1000\nOFFSET 999999",
        ),
    ],
)
def test_apply_limit_to_sql(
    sql: str,
    limit: int,
    force: bool,
    method: LimitMethod,
    expected: str,
    mocker: MockerFixture,
) -> None:
    """
    Test the `apply_limit_to_sql` method.
    """
    db = Database(database_name="test_database", sqlalchemy_uri="sqlite://")
    db_engine_spec = mocker.MagicMock(limit_method=method)
    db.get_db_engine_spec = mocker.MagicMock(return_value=db_engine_spec)

    limited = db.apply_limit_to_sql(sql, limit, force)
    assert limited == expected
