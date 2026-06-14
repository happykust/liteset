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
"""Liteset port of ``tests/unit_tests/commands/databases/tables_test.py``.

Adapted to the async ``TablesDatabaseCommand`` which takes an injected
async DAO + security manager. The model's
``get_all_table_names_in_schema`` / ``get_all_view_names_in_schema`` are
async; per-user filtering goes through
``security_manager.filter_datasources_by_perms`` and the per-table
``extra`` enrichment is batch-fetched via ``dao.get_table_extra_lookup``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, call, MagicMock

import pytest

from superset.commands.database.tables import TablesDatabaseCommand
from superset.utils.core import DatasourceName


def make_database(catalog: str | None) -> MagicMock:
    database = MagicMock()
    database.database_name = "test_database"
    database.table_cache_enabled = False
    database.table_cache_timeout = None
    database.get_default_catalog.return_value = catalog
    database.get_all_table_names_in_schema = AsyncMock(
        return_value={
            ("table1", "schema1", catalog),
            ("table2", "schema1", catalog),
        }
    )
    database.get_all_view_names_in_schema = AsyncMock(
        return_value={
            ("view1", "schema1", catalog),
        }
    )
    return database


def make_dao(database: MagicMock, extra_lookup: dict[str, Any]) -> AsyncMock:
    dao = AsyncMock()
    dao.find_by_id = AsyncMock(return_value=database)
    dao.get_table_extra_lookup = AsyncMock(return_value=extra_lookup)
    return dao


def make_security_manager(filtered_tables: Any, filtered_views: Any) -> MagicMock:
    sm = MagicMock()
    sm.filter_datasources_by_perms = AsyncMock(
        side_effect=[filtered_tables, filtered_views]
    )
    return sm


async def test_tables_with_catalog() -> None:
    """Tables/views are listed and per-user filtered for a catalog database."""
    database = make_database("catalog1")
    dao = make_dao(database, {"table1": {"foo": "bar"}})
    sm = make_security_manager(
        [
            DatasourceName("table1", "schema1", "catalog1"),
            DatasourceName("table2", "schema1", "catalog1"),
        ],
        [DatasourceName("view1", "schema1", "catalog1")],
    )

    command = TablesDatabaseCommand(
        dao, 1, "catalog1", "schema1", False, security_manager=sm, user=None
    )
    await command.validate()
    payload = await command.run()

    assert payload == {
        "count": 3,
        "result": [
            {"value": "table1", "type": "table", "extra": {"foo": "bar"}},
            {"value": "table2", "type": "table", "extra": None},
            {"value": "view1", "type": "view"},
        ],
    }

    sm.filter_datasources_by_perms.assert_has_awaits(
        [
            call(
                database=database,
                catalog="catalog1",
                schema="schema1",
                datasource_names=[
                    DatasourceName("table1", "schema1", "catalog1"),
                    DatasourceName("table2", "schema1", "catalog1"),
                ],
                user=None,
            ),
            call(
                database=database,
                catalog="catalog1",
                schema="schema1",
                datasource_names=[
                    DatasourceName("view1", "schema1", "catalog1"),
                ],
                user=None,
            ),
        ],
    )

    database.get_all_table_names_in_schema.assert_awaited_with(
        catalog="catalog1",
        schema="schema1",
        force=False,
        cache=database.table_cache_enabled,
        cache_timeout=database.table_cache_timeout,
    )


async def test_tables_without_catalog() -> None:
    """Tables/views are listed for a database without a catalog."""
    database = make_database(None)
    dao = make_dao(database, {"table1": {"foo": "bar"}})
    sm = make_security_manager(
        [
            DatasourceName("table1", "schema1"),
            DatasourceName("table2", "schema1"),
        ],
        [DatasourceName("view1", "schema1")],
    )

    command = TablesDatabaseCommand(
        dao, 1, None, "schema1", False, security_manager=sm, user=None
    )
    await command.validate()
    payload = await command.run()

    assert payload == {
        "count": 3,
        "result": [
            {"value": "table1", "type": "table", "extra": {"foo": "bar"}},
            {"value": "table2", "type": "table", "extra": None},
            {"value": "view1", "type": "view"},
        ],
    }

    sm.filter_datasources_by_perms.assert_has_awaits(
        [
            call(
                database=database,
                catalog=None,
                schema="schema1",
                datasource_names=[
                    DatasourceName("table1", "schema1"),
                    DatasourceName("table2", "schema1"),
                ],
                user=None,
            ),
            call(
                database=database,
                catalog=None,
                schema="schema1",
                datasource_names=[
                    DatasourceName("view1", "schema1"),
                ],
                user=None,
            ),
        ],
    )

    database.get_all_table_names_in_schema.assert_awaited_with(
        catalog=None,
        schema="schema1",
        force=False,
        cache=database.table_cache_enabled,
        cache_timeout=database.table_cache_timeout,
    )


async def test_tables_database_not_found() -> None:
    """Validation raises when the database id does not resolve."""
    from superset.commands.database.exceptions import DatabaseNotFoundError

    dao = AsyncMock()
    dao.find_by_id = AsyncMock(return_value=None)

    command = TablesDatabaseCommand(dao, 999, None, "schema1", False)
    with pytest.raises(DatabaseNotFoundError):
        await command.validate()
