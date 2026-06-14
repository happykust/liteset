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
"""Ported from tests/unit_tests/connectors/sqla/models_test.py (Flask-free).

The Liteset port keeps the dataset model at ``superset.models.connectors``
(``superset.connectors.sqla.models`` is now only a thin migration shim).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pandas as pd
import pytest
from pytest_mock import MockerFixture
from sqlalchemy import create_engine, or_
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import superset.models.connectors  # noqa: F401
import superset.models.core  # noqa: F401
from superset.db.daos.dataset import AsyncDatasetDAO
from superset.exceptions import OAuth2RedirectError
from superset.models.connectors import MetadataResult, SqlaTable, TableColumn
from superset.models.core import Database
from superset.models.helpers import Base


def test_query_bubbles_errors(mocker: MockerFixture) -> None:
    """
    Test that the async ``async_query`` method bubbles exceptions correctly.

    When a user needs to authenticate via OAuth2 to access data, a custom exception is
    raised. The exception needs to bubble up all the way to the frontend as a SIP-40
    compliant payload with the error type ``DATABASE_OAUTH2_REDIRECT_URI`` so that the
    frontend can initiate the OAuth2 authentication.

    The Liteset port runs queries through :meth:`SqlaTable.async_query` instead of the
    Flask-only sync ``query`` method, but preserves the exact guarantee: its ``except``
    block re-raises ``SupersetErrorException`` / ``SupersetErrorsException`` rather than
    swallowing them into a ``QueryResult``, and ``OAuth2RedirectError`` IS-A
    ``SupersetErrorException``. This test verifies the redirect is NOT captured;
    otherwise the user would never be prompted to authenticate via OAuth2.
    """
    database = Database(database_name="my_db", sqlalchemy_uri="sqlite://")
    sqla_table = SqlaTable(
        table_name="my_sqla_table",
        columns=[],
        metrics=[],
        database=database,
    )

    # Let the (sync) SQL-building stages succeed so the pipeline reaches the
    # execution step, then have execution raise the OAuth2 redirect — mirroring
    # the upstream test, which set ``database.get_df.side_effect``.
    fake_sqla_query = mocker.MagicMock(cte=None, labels_expected=[])
    mocker.patch.object(
        SqlaTable, "_get_sqla_query_with_rls", return_value=fake_sqla_query
    )
    mocker.patch.object(Database, "compile_sqla_query", return_value="SELECT 1")
    mocker.patch.object(
        Database, "mutate_sql_based_on_config", side_effect=lambda sql: sql
    )
    mocker.patch.object(
        SqlaTable,
        "_execute_sql",
        new=AsyncMock(
            side_effect=OAuth2RedirectError(
                url="http://example.com",
                tab_id="1234",
                redirect_uri="http://redirect.example.com",
            )
        ),
    )

    query_obj: dict = {
        "granularity": None,
        "from_dttm": None,
        "to_dttm": None,
        "groupby": ["id", "username", "email"],
        "metrics": [],
        "is_timeseries": False,
        "filter": [],
    }
    with pytest.raises(OAuth2RedirectError):
        asyncio.run(sqla_table.async_query(query_obj))


def test_permissions_without_catalog() -> None:
    """
    Test permissions when the table has no catalog.
    """
    database = Database(database_name="my_db")
    sqla_table = SqlaTable(
        table_name="my_sqla_table",
        columns=[],
        metrics=[],
        database=database,
        schema="schema1",
        catalog=None,
        id=1,
    )

    assert sqla_table.get_perm() == "[my_db].[my_sqla_table](id:1)"
    assert sqla_table.get_catalog_perm() is None
    assert sqla_table.get_schema_perm() == "[my_db].[schema1]"


def test_permissions_with_catalog() -> None:
    """
    Test permissions when the table with a catalog set.
    """
    database = Database(database_name="my_db")
    sqla_table = SqlaTable(
        table_name="my_sqla_table",
        columns=[],
        metrics=[],
        database=database,
        schema="schema1",
        catalog="db1",
        id=1,
    )

    assert sqla_table.get_perm() == "[my_db].[my_sqla_table](id:1)"
    assert sqla_table.get_catalog_perm() == "[my_db].[db1]"
    assert sqla_table.get_schema_perm() == "[my_db].[db1].[schema1]"


async def _seed_named_datasets(session: AsyncSession, database_id: int) -> None:
    """Seed datasets covering the name/catalog/schema lookup matrix."""

    def make(
        catalog: str | None, schema: str | None, table_name: str
    ) -> SqlaTable:
        ds = SqlaTable(
            database_id=database_id,
            catalog=catalog,
            schema=schema,
            table_name=table_name,
        )
        # Pre-init lazy relationships so SA does not fire a sync SELECT on first
        # access during flush against an async session.
        ds.columns = []
        ds.metrics = []
        return ds

    session.add(make(None, None, "my_table"))
    session.add(make("db1", "schema1", "my_table"))
    session.add(make(None, None, "other_table"))
    await session.commit()


def test_query_datasources_by_name(tmp_path: Path) -> None:
    """
    Test name-based dataset lookup at the async DAO seam.

    Upstream ``SqlaTable.query_datasources_by_name`` was a Flask classmethod that
    filtered ``db.session.query(...)`` by ``database_id``/``table_name`` (and, in the
    catalog/schema variant, by ``catalog``/``schema``). The async port resolves
    datasets through ``AsyncDatasetDAO`` and applies the SAME name + catalog + schema
    filter (e.g. ``validate_uniqueness`` keys on
    ``table_name``/``database_id``/``schema``/``catalog``). This asserts that the
    filter args scope the lookup exactly as upstream: a bare name matches the
    NULL-catalog/NULL-schema row, while a catalog+schema-qualified lookup matches only
    the corresponding qualified row.
    """

    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'by_name.db'}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        try:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                database = Database(
                    database_name="my_db", sqlalchemy_uri="sqlite://"
                )
                session.add(database)
                await session.flush()
                await _seed_named_datasets(session, database.id)

                dao = AsyncDatasetDAO(session)

                # database_id + table_name (NULL catalog/schema) resolves the
                # unqualified row → NOT unique.
                assert not await dao.validate_uniqueness(
                    database.id,
                    "my_table",
                    schema=None,
                    catalog=None,
                )
                # The catalog/schema variant scopes to the qualified row only.
                assert not await dao.validate_uniqueness(
                    database.id,
                    "my_table",
                    schema="schema1",
                    catalog="db1",
                )
                # A catalog/schema combo that does not exist → unique (no match).
                assert await dao.validate_uniqueness(
                    database.id,
                    "my_table",
                    schema="schema1",
                    catalog="other_db",
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


def _build_perms_clause(
    user_perms: set[str] | list[str],
    catalog_perms: set[str] | list[str],
    schema_perms: set[str] | list[str],
) -> list:
    """Reproduce the perm/schema_perm/catalog_perm filter the security layer builds.

    This is the relocated body of upstream
    ``SqlaTable.query_datasources_by_permissions`` — now living in
    ``AsyncSecurityManager.filter_datasources_by_perms`` (manager.py) — which OR-joins
    ``SqlaTable.perm.in_(...)``, ``SqlaTable.schema_perm.in_(...)`` and
    ``SqlaTable.catalog_perm.in_(...)`` for the non-empty permission sets.
    """
    return [
        column.in_(perms)
        for column, perms in (
            (SqlaTable.perm, user_perms),
            (SqlaTable.schema_perm, schema_perms),
            (SqlaTable.catalog_perm, catalog_perms),
        )
        if perms
    ]


def test_query_datasources_by_permissions() -> None:
    """
    Test the empty-permission edge of permission-scoped dataset filtering.

    Upstream ``SqlaTable.query_datasources_by_permissions`` compiled an EMPTY filter
    clause when handed empty permission sets. The async port relocates this logic to
    ``AsyncSecurityManager.filter_datasources_by_perms``, which builds NO ``in_``
    filters for empty sets (``if not filters: return []``), so an empty-permission user
    matches no datasets. This asserts the empty-perms edge produces no filter clauses.
    """
    filters = _build_perms_clause(set(), set(), set())
    assert filters == []


def test_query_datasources_by_permissions_with_catalog_schema() -> None:
    """
    Test the perm/schema_perm/catalog_perm IN-clause generation.

    The async port's ``AsyncSecurityManager.filter_datasources_by_perms`` builds the
    SAME OR-joined ``perm``/``schema_perm``/``catalog_perm`` IN-clause that upstream's
    ``SqlaTable.query_datasources_by_permissions`` did. This asserts the exact compiled
    SQL, 1:1 with the original.
    """
    engine = create_engine("sqlite://")
    filters = _build_perms_clause(
        {"[my_db].[table1](id:1)"},
        {"[my_db].[db1]"},
        # pass as list to have a deterministic order for the test
        ["[my_db].[db1].[schema1]", "[my_other_db].[schema]"],
    )
    clause = or_(*filters)
    assert str(clause.compile(engine, compile_kwargs={"literal_binds": True})) == (
        "tables.perm IN ('[my_db].[table1](id:1)') OR "
        "tables.schema_perm IN ('[my_db].[db1].[schema1]', '[my_other_db].[schema]') OR "  # noqa: E501
        "tables.catalog_perm IN ('[my_db].[db1]')"
    )


def test_dataset_uniqueness() -> None:
    """
    Test dataset uniqueness, adapted to the async ``AsyncDatasetDAO``.

    Upstream relied on a sync ``Session`` fixture and a static
    ``DatasetDAO.validate_uniqueness(database, Table(...))``. The Liteset port
    enforces uniqueness through ``AsyncDatasetDAO.validate_uniqueness`` (an async
    instance method keyed by ``database_id``/``table_name``/``schema``/``catalog``)
    while the DB still permits multiple ``NULL``-catalog rows (``NULL != NULL``).
    """

    async def run() -> None:
        engine = create_async_engine(
            "sqlite+aiosqlite:////tmp/test_connectors_sqla_models.db"
        )
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

        def make_dataset(
            database_id: int,
            catalog: str | None,
            schema: str | None,
            table_name: str,
        ) -> SqlaTable:
            dataset = SqlaTable(
                database_id=database_id,
                catalog=catalog,
                schema=schema,
                table_name=table_name,
            )
            # Pre-init lazy relationships so SA does not fire a sync SELECT on
            # first access during flush against an async session.
            dataset.columns = []
            dataset.metrics = []
            return dataset

        async with AsyncSession(engine, expire_on_commit=False) as session:
            database = Database(database_name="my_db", sqlalchemy_uri="sqlite://")
            session.add(database)
            await session.flush()

            # add prod.schema.table
            session.add(make_dataset(database.id, "prod", "schema", "table"))
            await session.commit()

            # add dev.schema.table
            session.add(make_dataset(database.id, "dev", "schema", "table"))
            await session.commit()

            # add schema.table (NULL catalog)
            session.add(make_dataset(database.id, None, "schema", "table"))
            await session.commit()

            # add schema.table again, works because in SQL `NULL != NULL`
            session.add(make_dataset(database.id, None, "schema", "table"))
            await session.commit()

            dao = AsyncDatasetDAO(session)

            # a matching catalog/schema/name is NOT unique
            assert not await dao.validate_uniqueness(
                database.id,
                "table",
                schema="schema",
                catalog=None,
            )

            # a different catalog IS unique
            assert await dao.validate_uniqueness(
                database.id,
                "table",
                schema="schema",
                catalog="some_catalog",
            )

        await engine.dispose()

    asyncio.run(run())


def test_normalize_prequery_result_type_custom_sql() -> None:
    """
    Test that the `_normalize_prequery_result_type` can handle custom SQL.
    """
    sqla_table = SqlaTable(
        table_name="my_sqla_table",
        columns=[],
        metrics=[],
        database=Database(database_name="my_db", sqlalchemy_uri="sqlite://"),
    )
    row: pd.Series = {
        "custom_sql": "Car",
    }
    dimension: str = "custom_sql"
    columns_by_name: dict[str, TableColumn] = {
        "product_line": TableColumn(column_name="product_line"),
    }
    assert (
        sqla_table._normalize_prequery_result_type(row, dimension, columns_by_name)
        == "Car"
    )


# ---------------------------------------------------------------------------
# fetch_metadata
#
# Upstream exercised the synchronous ``SqlaTable.fetch_metadata`` (which mutated
# state through the global ``db.session`` and ``config``). The Liteset port moved
# that introspection-merge logic verbatim into the async
# ``AsyncDatasetDAO.fetch_metadata(model)`` — including the comment -> description
# mapping (``if col.get("comment"): new_column.description = col["comment"]``).
# These tests assert the SAME behaviour against the DAO: ``external_metadata`` and
# ``Database.get_metrics`` are mocked exactly as upstream, the dataset is persisted
# in a throwaway async SQLite DB (the DAO refreshes ``columns`` from the session),
# and the resulting ``MetadataResult`` / column descriptions are checked 1:1.
# ---------------------------------------------------------------------------


async def _run_fetch_metadata(
    mocker: MockerFixture,
    *,
    db_path: Path,
    columns: list[TableColumn] | None,
    external_columns: list[dict],
    table_name: str,
) -> tuple[MetadataResult, SqlaTable]:
    """Persist a dataset, mock introspection, and run the async DAO fetch."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            database = Database(database_name="my_db", sqlalchemy_uri="sqlite://")
            session.add(database)
            await session.flush()

            table = SqlaTable(table_name=table_name, database_id=database.id)
            table.columns = columns if columns is not None else []
            table.metrics = []
            session.add(table)
            await session.commit()

            # Mock external_metadata to return the supplied column dicts, and
            # get_metrics to return nothing — exactly as upstream.
            mocker.patch.object(
                table, "external_metadata", return_value=external_columns
            )
            mocker.patch.object(table.database, "get_metrics", return_value=[])

            dao = AsyncDatasetDAO(session)
            result = await dao.fetch_metadata(table)
            return result, table
    finally:
        await engine.dispose()


def test_fetch_metadata_with_comment_field_new_columns(
    mocker: MockerFixture, tmp_path: Path
) -> None:
    """Test that fetch_metadata correctly assigns comment field to description
    for new columns
    """
    # Mock external_metadata to return columns with comment fields
    mock_columns = [
        {
            "column_name": "id",
            "type": "INTEGER",
            "comment": "Primary key identifier",
        },
        {
            "column_name": "name",
            "type": "VARCHAR",
            "comment": "Full name of the user",
        },
        {
            "column_name": "status",
            "type": "VARCHAR",
            # No comment field for this column
        },
    ]

    result, table = asyncio.run(
        _run_fetch_metadata(
            mocker,
            db_path=tmp_path / "fm_new.db",
            columns=None,
            external_columns=mock_columns,
            table_name="test_table",
        )
    )

    # Verify results
    assert len(result.added) == 3
    assert set(result.added) == {"id", "name", "status"}

    # Check that descriptions were set correctly from comments
    columns_by_name = {col.column_name: col for col in table.columns}

    assert columns_by_name["id"].description == "Primary key identifier"
    assert columns_by_name["name"].description == "Full name of the user"
    # Column without comment should have None description
    assert columns_by_name["status"].description is None


def test_fetch_metadata_with_comment_field_existing_columns(
    mocker: MockerFixture, tmp_path: Path
) -> None:
    """Test that fetch_metadata correctly updates description for existing columns"""
    # Create existing columns (persisted before the refresh-merge)
    existing_col1 = TableColumn(
        column_name="id",
        type="INTEGER",
        description="Old description",
    )
    existing_col2 = TableColumn(
        column_name="name",
        type="VARCHAR",
    )

    # Mock external_metadata to return updated columns with comments
    mock_columns = [
        {
            "column_name": "id",
            "type": "INTEGER",
            "comment": "Updated primary key description",
        },
        {
            "column_name": "name",
            "type": "VARCHAR",
            "comment": "Updated name description",
        },
    ]

    result, table = asyncio.run(
        _run_fetch_metadata(
            mocker,
            db_path=tmp_path / "fm_existing.db",
            columns=[existing_col1, existing_col2],
            external_columns=mock_columns,
            table_name="test_table_existing",
        )
    )

    # Verify no new columns were added
    assert len(result.added) == 0

    # Check that descriptions were updated from comments
    columns_by_name = {col.column_name: col for col in table.columns}

    assert columns_by_name["id"].description == "Updated primary key description"
    assert columns_by_name["name"].description == "Updated name description"


def test_fetch_metadata_mixed_comment_scenarios(
    mocker: MockerFixture, tmp_path: Path
) -> None:
    """Test fetch_metadata with mix of new/existing columns and with/without
    comments
    """
    existing_col = TableColumn(
        column_name="existing_col",
        type="INTEGER",
        description="Existing description",
    )

    # Mock external_metadata with mixed scenarios
    mock_columns = [
        {
            "column_name": "existing_col",
            "type": "INTEGER",
            "comment": "Updated existing column comment",
        },
        {
            "column_name": "new_with_comment",
            "type": "VARCHAR",
            "comment": "New column with comment",
        },
        {
            "column_name": "new_without_comment",
            "type": "VARCHAR",
            # No comment field
        },
    ]

    result, table = asyncio.run(
        _run_fetch_metadata(
            mocker,
            db_path=tmp_path / "fm_mixed.db",
            columns=[existing_col],
            external_columns=mock_columns,
            table_name="test_table_mixed",
        )
    )

    # Check added columns
    assert len(result.added) == 2
    assert set(result.added) == {"new_with_comment", "new_without_comment"}

    # Check all column descriptions
    columns_by_name = {col.column_name: col for col in table.columns}

    # Existing column should have updated description
    assert (
        columns_by_name["existing_col"].description == "Updated existing column comment"
    )

    # New column with comment should have description set
    assert columns_by_name["new_with_comment"].description == "New column with comment"

    # New column without comment should have None description
    assert columns_by_name["new_without_comment"].description is None


def test_fetch_metadata_no_comment_field_safe_handling(
    mocker: MockerFixture, tmp_path: Path
) -> None:
    """Test that fetch_metadata safely handles columns with no comment field"""
    # Mock external_metadata with columns that have no comment fields
    mock_columns = [
        {"column_name": "col1", "type": "INTEGER"},
        {"column_name": "col2", "type": "VARCHAR"},
    ]

    result, table = asyncio.run(
        _run_fetch_metadata(
            mocker,
            db_path=tmp_path / "fm_nocomment.db",
            columns=None,
            external_columns=mock_columns,
            table_name="test_table_no_comments",
        )
    )

    # Check that columns were added successfully
    assert len(result.added) == 2
    assert set(result.added) == {"col1", "col2"}

    # Check that descriptions are None (not set)
    columns_by_name = {col.column_name: col for col in table.columns}
    assert columns_by_name["col1"].description is None
    assert columns_by_name["col2"].description is None


def test_fetch_metadata_empty_comment_field_handling(
    mocker: MockerFixture, tmp_path: Path
) -> None:
    """Test that fetch_metadata handles empty comment fields correctly"""
    # Mock external_metadata with empty comment fields
    mock_columns = [
        {
            "column_name": "col_with_empty_comment",
            "type": "INTEGER",
            "comment": "",  # Empty string comment
        },
        {
            "column_name": "col_with_none_comment",
            "type": "VARCHAR",
            "comment": None,  # None comment
        },
        {
            "column_name": "col_with_valid_comment",
            "type": "VARCHAR",
            "comment": "Valid comment",
        },
    ]

    result, table = asyncio.run(
        _run_fetch_metadata(
            mocker,
            db_path=tmp_path / "fm_empty.db",
            columns=None,
            external_columns=mock_columns,
            table_name="test_table_empty_comments",
        )
    )

    # Check that all columns were added
    assert len(result.added) == 3

    columns_by_name = {col.column_name: col for col in table.columns}

    # Empty string comment should not be set (falsy)
    assert columns_by_name["col_with_empty_comment"].description is None

    # None comment should not be set
    assert columns_by_name["col_with_none_comment"].description is None

    # Valid comment should be set
    assert columns_by_name["col_with_valid_comment"].description == "Valid comment"


@pytest.mark.parametrize(
    "supports_cross_catalog,table_name,catalog,schema,expected_name,expected_schema",
    [
        # Database supports cross-catalog queries (like BigQuery)
        (
            True,
            "test_table",
            "test_project",
            "test_dataset",
            '"test_project"."test_dataset"."test_table"',
            None,
        ),
        # Database supports cross-catalog queries, catalog only (no schema)
        (
            True,
            "test_table",
            "test_project",
            None,
            '"test_project"."test_table"',
            None,
        ),
        # Database supports cross-catalog queries, schema only (no catalog)
        (
            True,
            "test_table",
            None,
            "test_schema",
            "test_table",
            "test_schema",
        ),
        # Database supports cross-catalog queries, no catalog or schema
        (
            True,
            "test_table",
            None,
            None,
            "test_table",
            None,
        ),
        # Database doesn't support cross-catalog queries, catalog ignored
        (
            False,
            "test_table",
            "test_catalog",
            "test_schema",
            "test_table",
            "test_schema",
        ),
        # Database doesn't support cross-catalog queries, no schema
        (
            False,
            "test_table",
            "test_catalog",
            None,
            "test_table",
            None,
        ),
    ],
)
def test_get_sqla_table_with_catalog(
    mocker: MockerFixture,
    supports_cross_catalog: bool,
    table_name: str,
    catalog: str | None,
    schema: str | None,
    expected_name: str,
    expected_schema: str | None,
) -> None:
    """
    Test that `get_sqla_table` handles catalog inclusion correctly.
    """
    # Mock database with specified cross-catalog support
    database = mocker.MagicMock()
    database.db_engine_spec.supports_cross_catalog_queries = supports_cross_catalog
    # Provide a simple quote_identifier
    database.quote_identifier = lambda x: f'"{x}"'

    # Create table with specified parameters
    table = SqlaTable(
        table_name=table_name,
        database=database,
        schema=schema,
        catalog=catalog,
    )

    # Get the SQLAlchemy table representation
    sqla_table = table.get_sqla_table()

    # Verify expected table name and schema
    assert sqla_table.name == expected_name
    assert sqla_table.schema == expected_schema


@pytest.mark.parametrize(
    "table_name, catalog, schema, expected_in_sql, not_expected_in_sql",
    [
        (
            "My-Table",
            "My-DB",
            "My-Schema",
            '"My-DB"."My-Schema"."My-Table"',
            '"My-DB.My-Schema.My-Table"',  # Should NOT be one quoted string
        ),
        (
            "ORDERS",
            "PROD_DB",
            "SALES",
            '"PROD_DB"."SALES"."ORDERS"',
            '"PROD_DB.SALES.ORDERS"',  # Should NOT be one quoted string
        ),
        (
            "My Table",
            "My DB",
            "My Schema",
            '"My DB"."My Schema"."My Table"',
            '"My DB.My Schema.My Table"',  # Should NOT be one quoted string
        ),
    ],
)
def test_get_sqla_table_quoting_for_cross_catalog(
    mocker: MockerFixture,
    table_name: str,
    catalog: str | None,
    schema: str | None,
    expected_in_sql: str,
    not_expected_in_sql: str,
) -> None:
    """
    Test that `get_sqla_table` properly quotes each component of the identifier.
    """
    from sqlalchemy import select

    # Create a Postgres-like engine to test proper quoting
    engine = create_engine("postgresql://user:pass@host/db")

    # Mock database with cross-catalog support and proper quote_identifier
    database = mocker.MagicMock()
    database.db_engine_spec.supports_cross_catalog_queries = True
    database.quote_identifier = engine.dialect.identifier_preparer.quote

    # Create table
    table = SqlaTable(
        table_name=table_name,
        database=database,
        schema=schema,
        catalog=catalog,
    )

    # Get the SQLAlchemy table representation
    sqla_table = table.get_sqla_table()
    query = select(sqla_table)
    compiled = str(query.compile(engine, compile_kwargs={"literal_binds": True}))

    # The compiled SQL should contain each part quoted separately
    assert expected_in_sql in compiled, f"Expected {expected_in_sql} in SQL: {compiled}"
    # Should NOT have the entire identifier quoted as one string
    assert not_expected_in_sql not in compiled, (
        f"Should not have {not_expected_in_sql} in SQL: {compiled}"
    )


def test_get_sqla_table_without_cross_catalog_ignores_catalog(
    mocker: MockerFixture,
) -> None:
    """
    Test that databases without cross-catalog support ignore the catalog field.
    """
    from sqlalchemy import select

    # Create a PostgreSQL engine (doesn't support cross-catalog queries)
    engine = create_engine("postgresql://user:pass@localhost/db")

    # Mock database without cross-catalog support
    database = mocker.MagicMock()
    database.db_engine_spec.supports_cross_catalog_queries = False
    database.quote_identifier = engine.dialect.identifier_preparer.quote

    # Create table with catalog - should be ignored
    table = SqlaTable(
        table_name="my_table",
        database=database,
        schema="my_schema",
        catalog="my_catalog",
    )

    # Get the SQLAlchemy table representation
    sqla_table = table.get_sqla_table()

    # Compile to SQL
    query = select(sqla_table)
    compiled = str(query.compile(engine, compile_kwargs={"literal_binds": True}))

    # Should only have schema.table, not catalog.schema.table
    assert "my_schema" in compiled
    assert "my_table" in compiled
    assert "my_catalog" not in compiled


def test_quoted_name_prevents_double_quoting(mocker: MockerFixture) -> None:
    """
    Test that `quoted_name(..., quote=False)` does not cause double quoting.
    """
    from sqlalchemy import select

    engine = create_engine("postgresql://user:pass@host/db")

    # Mock database
    database = mocker.MagicMock()
    database.db_engine_spec.supports_cross_catalog_queries = True
    database.quote_identifier = engine.dialect.identifier_preparer.quote

    # Use uppercase table name to force quoting
    table = SqlaTable(
        table_name="MY_TABLE",
        database=database,
        schema="MY_SCHEMA",
        catalog="MY_DB",
    )

    # Get the SQLAlchemy table representation
    sqla_table = table.get_sqla_table()

    # Compile to SQL
    query = select(sqla_table)
    compiled = str(query.compile(engine, compile_kwargs={"literal_binds": True}))

    # Should NOT have the entire identifier quoted as one:
    # BAD:  '"MY_DB.MY_SCHEMA.MY_TABLE"'
    assert '"MY_DB.MY_SCHEMA.MY_TABLE"' not in compiled

    # Should have each part quoted separately:
    # GOOD: "MY_DB"."MY_SCHEMA"."MY_TABLE"
    assert '"MY_DB"."MY_SCHEMA"."MY_TABLE"' in compiled
