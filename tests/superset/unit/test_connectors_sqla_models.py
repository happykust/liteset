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
"""Tests for the dataset model at superset.models.connectors
(superset.connectors.sqla.models is now only a thin migration shim).
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
    """OAuth2RedirectError IS-A SupersetErrorException and must not be swallowed
    into QueryResult.

    The Liteset port uses async_query instead of the Flask-only sync query method, but
    preserves the guarantee that SupersetErrorException / SupersetErrorsException are
    re-raised so the SIP-40 OAuth2 redirect reaches the frontend.
    """
    database = Database(database_name="my_db", sqlalchemy_uri="sqlite://")
    sqla_table = SqlaTable(
        table_name="my_sqla_table",
        columns=[],
        metrics=[],
        database=database,
    )

    # Upstream set database.get_df.side_effect; async port patches _execute_sql instead.
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
    def make(catalog: str | None, schema: str | None, table_name: str) -> SqlaTable:
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
    """Upstream SqlaTable.query_datasources_by_name is replaced by
    AsyncDatasetDAO.validate_uniqueness.

    A bare name matches only the NULL-catalog/NULL-schema row; a
    catalog+schema-qualified lookup matches only the corresponding qualified row.
    """

    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'by_name.db'}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        try:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                database = Database(database_name="my_db", sqlalchemy_uri="sqlite://")
                session.add(database)
                await session.flush()
                await _seed_named_datasets(session, database.id)

                dao = AsyncDatasetDAO(session)

                assert not await dao.validate_uniqueness(
                    database.id,
                    "my_table",
                    schema=None,
                    catalog=None,
                )
                assert not await dao.validate_uniqueness(
                    database.id,
                    "my_table",
                    schema="schema1",
                    catalog="db1",
                )
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
    """Reproduce the OR-joined perm/schema_perm/catalog_perm IN-clause built
    by AsyncSecurityManager.filter_datasources_by_perms.
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
    """Empty permission sets produce no filter clauses
    (empty-permission user matches no datasets).
    """
    filters = _build_perms_clause(set(), set(), set())
    assert filters == []


def test_query_datasources_by_permissions_with_catalog_schema() -> None:
    """filter_datasources_by_perms builds an OR-joined
    perm/schema_perm/catalog_perm IN-clause.
    """
    engine = create_engine("sqlite://")
    filters = _build_perms_clause(
        {"[my_db].[table1](id:1)"},
        {"[my_db].[db1]"},
        # list (not set) for a deterministic order in the compiled SQL assertion
        ["[my_db].[db1].[schema1]", "[my_other_db].[schema]"],
    )
    clause = or_(*filters)
    assert str(clause.compile(engine, compile_kwargs={"literal_binds": True})) == (
        "tables.perm IN ('[my_db].[table1](id:1)') OR "
        "tables.schema_perm IN ('[my_db].[db1].[schema1]', '[my_other_db].[schema]') OR "  # noqa: E501
        "tables.catalog_perm IN ('[my_db].[db1]')"
    )


def test_dataset_uniqueness() -> None:
    """Uniqueness is enforced by AsyncDatasetDAO.validate_uniqueness; the DB
    still permits multiple NULL-catalog rows (NULL != NULL in SQL).
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

            session.add(make_dataset(database.id, "prod", "schema", "table"))
            await session.commit()

            session.add(make_dataset(database.id, "dev", "schema", "table"))
            await session.commit()

            session.add(make_dataset(database.id, None, "schema", "table"))
            await session.commit()

            # NULL catalog rows are always unique to SQL (NULL != NULL)
            session.add(make_dataset(database.id, None, "schema", "table"))
            await session.commit()

            dao = AsyncDatasetDAO(session)

            assert not await dao.validate_uniqueness(
                database.id,
                "table",
                schema="schema",
                catalog=None,
            )

            assert await dao.validate_uniqueness(
                database.id,
                "table",
                schema="schema",
                catalog="some_catalog",
            )

        await engine.dispose()

    asyncio.run(run())


def test_normalize_prequery_result_type_custom_sql() -> None:
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


async def _run_fetch_metadata(
    mocker: MockerFixture,
    *,
    db_path: Path,
    columns: list[TableColumn] | None,
    external_columns: list[dict],
    table_name: str,
) -> tuple[MetadataResult, SqlaTable]:
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
    """fetch_metadata must assign comment field to description for new columns."""
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

    assert len(result.added) == 3
    assert set(result.added) == {"id", "name", "status"}

    columns_by_name = {col.column_name: col for col in table.columns}

    assert columns_by_name["id"].description == "Primary key identifier"
    assert columns_by_name["name"].description == "Full name of the user"
    assert columns_by_name["status"].description is None


def test_fetch_metadata_with_comment_field_existing_columns(
    mocker: MockerFixture, tmp_path: Path
) -> None:
    """fetch_metadata must update description on existing columns from the
    DB comment.
    """
    existing_col1 = TableColumn(
        column_name="id",
        type="INTEGER",
        description="Old description",
    )
    existing_col2 = TableColumn(
        column_name="name",
        type="VARCHAR",
    )

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

    assert len(result.added) == 0

    columns_by_name = {col.column_name: col for col in table.columns}

    assert columns_by_name["id"].description == "Updated primary key description"
    assert columns_by_name["name"].description == "Updated name description"


def test_fetch_metadata_mixed_comment_scenarios(
    mocker: MockerFixture, tmp_path: Path
) -> None:
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

    assert len(result.added) == 2
    assert set(result.added) == {"new_with_comment", "new_without_comment"}

    columns_by_name = {col.column_name: col for col in table.columns}

    assert (
        columns_by_name["existing_col"].description == "Updated existing column comment"
    )
    assert columns_by_name["new_with_comment"].description == "New column with comment"
    assert columns_by_name["new_without_comment"].description is None


def test_fetch_metadata_no_comment_field_safe_handling(
    mocker: MockerFixture, tmp_path: Path
) -> None:
    """fetch_metadata must not raise when comment field is absent."""
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

    assert len(result.added) == 2
    assert set(result.added) == {"col1", "col2"}

    columns_by_name = {col.column_name: col for col in table.columns}
    assert columns_by_name["col1"].description is None
    assert columns_by_name["col2"].description is None


def test_fetch_metadata_empty_comment_field_handling(
    mocker: MockerFixture, tmp_path: Path
) -> None:
    """fetch_metadata must not set description for empty-string or None
    comments (both are falsy).
    """
    mock_columns = [
        {
            "column_name": "col_with_empty_comment",
            "type": "INTEGER",
            "comment": "",
        },
        {
            "column_name": "col_with_none_comment",
            "type": "VARCHAR",
            "comment": None,
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

    assert len(result.added) == 3

    columns_by_name = {col.column_name: col for col in table.columns}

    assert columns_by_name["col_with_empty_comment"].description is None
    assert columns_by_name["col_with_none_comment"].description is None
    assert columns_by_name["col_with_valid_comment"].description == "Valid comment"


@pytest.mark.parametrize(
    "supports_cross_catalog,table_name,catalog,schema,expected_name,expected_schema",
    [
        (
            True,
            "test_table",
            "test_project",
            "test_dataset",
            '"test_project"."test_dataset"."test_table"',
            None,
        ),
        (
            True,
            "test_table",
            "test_project",
            None,
            '"test_project"."test_table"',
            None,
        ),
        (
            True,
            "test_table",
            None,
            "test_schema",
            "test_table",
            "test_schema",
        ),
        (
            True,
            "test_table",
            None,
            None,
            "test_table",
            None,
        ),
        (
            False,
            "test_table",
            "test_catalog",
            "test_schema",
            "test_table",
            "test_schema",
        ),
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
    database = mocker.MagicMock()
    database.db_engine_spec.supports_cross_catalog_queries = supports_cross_catalog
    database.quote_identifier = lambda x: f'"{x}"'

    table = SqlaTable(
        table_name=table_name,
        database=database,
        schema=schema,
        catalog=catalog,
    )

    sqla_table = table.get_sqla_table()

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
    """Each catalog/schema/table component must be quoted individually,
    not as one concatenated string.
    """
    from sqlalchemy import select

    engine = create_engine("postgresql://user:pass@host/db")

    database = mocker.MagicMock()
    database.db_engine_spec.supports_cross_catalog_queries = True
    database.quote_identifier = engine.dialect.identifier_preparer.quote

    table = SqlaTable(
        table_name=table_name,
        database=database,
        schema=schema,
        catalog=catalog,
    )

    sqla_table = table.get_sqla_table()
    query = select(sqla_table)
    compiled = str(query.compile(engine, compile_kwargs={"literal_binds": True}))

    assert expected_in_sql in compiled, f"Expected {expected_in_sql} in SQL: {compiled}"
    assert not_expected_in_sql not in compiled, (
        f"Should not have {not_expected_in_sql} in SQL: {compiled}"
    )


def test_get_sqla_table_without_cross_catalog_ignores_catalog(
    mocker: MockerFixture,
) -> None:
    from sqlalchemy import select

    engine = create_engine("postgresql://user:pass@localhost/db")

    database = mocker.MagicMock()
    database.db_engine_spec.supports_cross_catalog_queries = False
    database.quote_identifier = engine.dialect.identifier_preparer.quote

    table = SqlaTable(
        table_name="my_table",
        database=database,
        schema="my_schema",
        catalog="my_catalog",
    )

    sqla_table = table.get_sqla_table()

    query = select(sqla_table)
    compiled = str(query.compile(engine, compile_kwargs={"literal_binds": True}))

    assert "my_schema" in compiled
    assert "my_table" in compiled
    assert "my_catalog" not in compiled


def test_quoted_name_prevents_double_quoting(mocker: MockerFixture) -> None:
    """quoted_name(..., quote=False) must not cause double quoting of
    catalog.schema.table.
    """
    from sqlalchemy import select

    engine = create_engine("postgresql://user:pass@host/db")

    database = mocker.MagicMock()
    database.db_engine_spec.supports_cross_catalog_queries = True
    database.quote_identifier = engine.dialect.identifier_preparer.quote

    table = SqlaTable(
        table_name="MY_TABLE",
        database=database,
        schema="MY_SCHEMA",
        catalog="MY_DB",
    )

    sqla_table = table.get_sqla_table()

    query = select(sqla_table)
    compiled = str(query.compile(engine, compile_kwargs={"literal_binds": True}))

    # BAD: '"MY_DB.MY_SCHEMA.MY_TABLE"' — entire dotted path as one quoted token
    assert '"MY_DB.MY_SCHEMA.MY_TABLE"' not in compiled
    # GOOD: each component quoted separately
    assert '"MY_DB"."MY_SCHEMA"."MY_TABLE"' in compiled
