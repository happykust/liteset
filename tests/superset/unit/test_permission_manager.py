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
"""Tests for AsyncPermissionManager and permission string formatters."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from superset.security.permission_manager import (
    AsyncPermissionManager,
    get_catalog_perm,
    get_database_perm,
    get_dataset_perm,
    get_schema_perm,
)
from superset.security.permissions import (
    CATALOG_ACCESS,
    DATABASE_ACCESS,
    DATASOURCE_ACCESS,
    SCHEMA_ACCESS,
)


# ---------------------------------------------------------------------------
# Permission string formatter tests
# ---------------------------------------------------------------------------


class TestPermissionStringFormatters:
    def test_get_database_perm(self) -> None:
        assert get_database_perm(1, "my_db") == "[my_db].(id:1)"

    def test_get_database_perm_special_chars(self) -> None:
        assert get_database_perm(42, "db with spaces") == "[db with spaces].(id:42)"

    def test_get_dataset_perm(self) -> None:
        assert get_dataset_perm(5, "my_table", "my_db") == "[my_db].[my_table](id:5)"

    def test_get_schema_perm_no_catalog(self) -> None:
        assert get_schema_perm("my_db", None, "public") == "[my_db].[public]"

    def test_get_schema_perm_with_catalog(self) -> None:
        result = get_schema_perm("my_db", "main_catalog", "public")
        assert result == "[my_db].[main_catalog].[public]"

    def test_get_schema_perm_none_schema(self) -> None:
        assert get_schema_perm("my_db", None, None) is None

    def test_get_catalog_perm(self) -> None:
        assert get_catalog_perm("my_db", "main_catalog") == "[my_db].[main_catalog]"

    def test_get_catalog_perm_none(self) -> None:
        assert get_catalog_perm("my_db", None) is None


# ---------------------------------------------------------------------------
# Helper to build mock async session with controllable query results
# ---------------------------------------------------------------------------


def _make_mock_session() -> AsyncMock:
    """Create a mock AsyncSession that supports execute/flush/add."""
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()

    # Default: execute returns empty result set
    result = MagicMock()
    result.scalars.return_value.one_or_none.return_value = None
    result.scalars.return_value.first.return_value = None
    result.scalars.return_value.all.return_value = []
    session.execute = AsyncMock(return_value=result)
    return session


def _make_mock_pvm(
    pvm_id: int = 1,
    perm_name: str = "database_access",
    vm_name: str = "[test].(id:1)",
    vm_id: int = 10,
    perm_id: int = 20,
) -> MagicMock:
    """Create a mock PermissionView."""
    pvm = MagicMock()
    pvm.id = pvm_id
    pvm.permission_id = perm_id
    pvm.view_menu_id = vm_id
    pvm.permission = MagicMock(id=perm_id, name=perm_name)
    pvm.view_menu = MagicMock(id=vm_id, name=vm_name)
    return pvm


def _make_mock_database(
    db_id: int = 1, db_name: str = "test_db"
) -> MagicMock:
    """Create a mock Database model."""
    db = MagicMock()
    db.id = db_id
    db.database_name = db_name
    return db


def _make_mock_dataset(
    ds_id: int = 1,
    table_name: str = "test_table",
    database_id: int = 1,
    schema: str | None = "public",
    catalog: str | None = None,
    perm: str | None = None,
    database: MagicMock | None = None,
) -> MagicMock:
    """Create a mock SqlaTable model."""
    ds = MagicMock()
    ds.id = ds_id
    ds.table_name = table_name
    ds.database_id = database_id
    ds.schema = schema
    ds.catalog = catalog
    ds.perm = perm
    ds.database = database
    return ds


# ---------------------------------------------------------------------------
# add_permission_view_menu tests
# ---------------------------------------------------------------------------


class TestAddPermissionViewMenu:
    @pytest.mark.asyncio
    async def test_returns_none_for_empty_view_menu_name(self) -> None:
        session = _make_mock_session()
        result = await AsyncPermissionManager.add_permission_view_menu(
            session, "database_access", None
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_for_blank_view_menu_name(self) -> None:
        session = _make_mock_session()
        result = await AsyncPermissionManager.add_permission_view_menu(
            session, "database_access", ""
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_existing_pvm(self) -> None:
        """If PVM already exists, return it without creating."""
        session = _make_mock_session()
        existing_pvm = _make_mock_pvm()

        # First call to _find_permission_view_menu returns the existing PVM
        with patch.object(
            AsyncPermissionManager,
            "_find_permission_view_menu",
            new_callable=AsyncMock,
            return_value=existing_pvm,
        ):
            result = await AsyncPermissionManager.add_permission_view_menu(
                session, DATABASE_ACCESS, "[test].(id:1)"
            )

        assert result is existing_pvm
        # Should NOT have called session.add
        session.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_creates_new_pvm_with_existing_perm_and_vm(self) -> None:
        """Creates PVM when Permission and ViewMenu exist but PVM does not."""
        session = _make_mock_session()
        mock_perm = MagicMock(id=10, name="database_access")
        mock_vm = MagicMock(id=20, name="[test].(id:1)")

        call_count = 0

        async def mock_find_pvm(s, pn, vmn):
            nonlocal call_count
            call_count += 1
            return None  # PVM does not exist

        async def mock_find_perm(s, name):
            return mock_perm

        async def mock_find_vm(s, name):
            return mock_vm

        with (
            patch.object(
                AsyncPermissionManager,
                "_find_permission_view_menu",
                side_effect=mock_find_pvm,
            ),
            patch.object(
                AsyncPermissionManager,
                "_find_permission",
                side_effect=mock_find_perm,
            ),
            patch.object(
                AsyncPermissionManager,
                "_find_view_menu",
                side_effect=mock_find_vm,
            ),
        ):
            result = await AsyncPermissionManager.add_permission_view_menu(
                session, DATABASE_ACCESS, "[test].(id:1)"
            )

        assert result is not None
        session.add.assert_called_once()
        session.flush.assert_called()

    @pytest.mark.asyncio
    async def test_creates_permission_when_missing(self) -> None:
        """Creates Permission if it does not exist yet."""
        session = _make_mock_session()
        mock_vm = MagicMock(id=20, name="[test].(id:1)")

        async def mock_find_pvm(s, pn, vmn):
            return None

        async def mock_find_perm(s, name):
            return None  # Permission does not exist

        async def mock_find_vm(s, name):
            return mock_vm

        with (
            patch.object(
                AsyncPermissionManager,
                "_find_permission_view_menu",
                side_effect=mock_find_pvm,
            ),
            patch.object(
                AsyncPermissionManager,
                "_find_permission",
                side_effect=mock_find_perm,
            ),
            patch.object(
                AsyncPermissionManager,
                "_find_view_menu",
                side_effect=mock_find_vm,
            ),
        ):
            result = await AsyncPermissionManager.add_permission_view_menu(
                session, DATABASE_ACCESS, "[test].(id:1)"
            )

        assert result is not None
        # Permission + PVM = 2 add() calls
        assert session.add.call_count == 2

    @pytest.mark.asyncio
    async def test_creates_view_menu_when_missing(self) -> None:
        """Creates ViewMenu if it does not exist yet."""
        session = _make_mock_session()
        mock_perm = MagicMock(id=10, name="database_access")

        async def mock_find_pvm(s, pn, vmn):
            return None

        async def mock_find_perm(s, name):
            return mock_perm

        async def mock_find_vm(s, name):
            return None  # ViewMenu does not exist

        with (
            patch.object(
                AsyncPermissionManager,
                "_find_permission_view_menu",
                side_effect=mock_find_pvm,
            ),
            patch.object(
                AsyncPermissionManager,
                "_find_permission",
                side_effect=mock_find_perm,
            ),
            patch.object(
                AsyncPermissionManager,
                "_find_view_menu",
                side_effect=mock_find_vm,
            ),
        ):
            result = await AsyncPermissionManager.add_permission_view_menu(
                session, DATABASE_ACCESS, "[test].(id:1)"
            )

        assert result is not None
        # ViewMenu + PVM = 2 add() calls
        assert session.add.call_count == 2


# ---------------------------------------------------------------------------
# del_permission_view_menu tests
# ---------------------------------------------------------------------------


class TestDelPermissionViewMenu:
    @pytest.mark.asyncio
    async def test_noop_when_pvm_not_found(self) -> None:
        session = _make_mock_session()

        with patch.object(
            AsyncPermissionManager,
            "_find_permission_view_menu",
            new_callable=AsyncMock,
            return_value=None,
        ):
            # Should not raise
            await AsyncPermissionManager.del_permission_view_menu(
                session, DATABASE_ACCESS, "[test].(id:1)"
            )

    @pytest.mark.asyncio
    async def test_deletes_pvm_and_associations(self) -> None:
        session = _make_mock_session()
        pvm = _make_mock_pvm()

        # After PVM deletion, no remaining PVMs reference this view_menu
        no_remaining = MagicMock()
        no_remaining.scalars.return_value.first.return_value = None

        # Set up execute to return no_remaining for the orphan check
        execute_calls = []
        original_execute = session.execute

        async def track_execute(stmt, *args, **kwargs):
            execute_calls.append(stmt)
            return no_remaining

        session.execute = AsyncMock(side_effect=track_execute)

        with patch.object(
            AsyncPermissionManager,
            "_find_permission_view_menu",
            new_callable=AsyncMock,
            return_value=pvm,
        ):
            await AsyncPermissionManager.del_permission_view_menu(
                session, DATABASE_ACCESS, "[test].(id:1)"
            )

        # Should have called execute for: delete role assoc, delete PVM,
        # check orphan, delete ViewMenu, flush
        assert session.execute.call_count >= 3


# ---------------------------------------------------------------------------
# on_database_created tests
# ---------------------------------------------------------------------------


class TestOnDatabaseCreated:
    @pytest.mark.asyncio
    async def test_creates_database_access_pvm(self) -> None:
        session = _make_mock_session()
        database = _make_mock_database(db_id=5, db_name="analytics")

        with patch.object(
            AsyncPermissionManager,
            "add_permission_view_menu",
            new_callable=AsyncMock,
        ) as mock_add:
            await AsyncPermissionManager.on_database_created(session, database)

        mock_add.assert_called_once_with(
            session, DATABASE_ACCESS, "[analytics].(id:5)"
        )


# ---------------------------------------------------------------------------
# on_database_deleted tests
# ---------------------------------------------------------------------------


class TestOnDatabaseDeleted:
    @pytest.mark.asyncio
    async def test_deletes_database_pvm_and_schema_pvms(self) -> None:
        session = _make_mock_session()
        database = _make_mock_database(db_id=5, db_name="analytics")

        # Return empty list for schema/catalog PVM query
        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = []
        session.execute = AsyncMock(return_value=result_mock)

        with patch.object(
            AsyncPermissionManager,
            "del_permission_view_menu",
            new_callable=AsyncMock,
        ) as mock_del:
            await AsyncPermissionManager.on_database_deleted(session, database)

        mock_del.assert_called_once_with(
            session, DATABASE_ACCESS, "[analytics].(id:5)"
        )


# ---------------------------------------------------------------------------
# on_database_updated tests
# ---------------------------------------------------------------------------


class TestOnDatabaseUpdated:
    @pytest.mark.asyncio
    async def test_noop_when_name_unchanged(self) -> None:
        session = _make_mock_session()
        database = _make_mock_database(db_id=5, db_name="analytics")

        with patch.object(
            AsyncPermissionManager,
            "_rename_view_menu",
            new_callable=AsyncMock,
        ) as mock_rename:
            # Same name => no-op
            await AsyncPermissionManager.on_database_updated(
                session, database, old_database_name="analytics"
            )

        mock_rename.assert_not_called()

    @pytest.mark.asyncio
    async def test_noop_when_old_name_is_none(self) -> None:
        session = _make_mock_session()
        database = _make_mock_database()

        with patch.object(
            AsyncPermissionManager,
            "_rename_view_menu",
            new_callable=AsyncMock,
        ) as mock_rename:
            await AsyncPermissionManager.on_database_updated(
                session, database, old_database_name=None
            )

        mock_rename.assert_not_called()

    @pytest.mark.asyncio
    async def test_renames_pvm_when_name_changes(self) -> None:
        session = _make_mock_session()
        database = _make_mock_database(db_id=5, db_name="analytics_v2")

        old_pvm = _make_mock_pvm(
            pvm_id=1,
            vm_name="[analytics].(id:5)",
        )

        async def mock_find_pvm(s, pn, vmn):
            if vmn == "[analytics_v2].(id:5)":
                return None  # new does not exist yet
            if vmn == "[analytics].(id:5)":
                return old_pvm
            return None

        with (
            patch.object(
                AsyncPermissionManager,
                "_find_permission_view_menu",
                side_effect=mock_find_pvm,
            ),
            patch.object(
                AsyncPermissionManager,
                "_rename_view_menu",
                new_callable=AsyncMock,
            ) as mock_rename,
            patch.object(
                AsyncPermissionManager,
                "_rename_datasource_perms_for_database",
                new_callable=AsyncMock,
            ),
        ):
            await AsyncPermissionManager.on_database_updated(
                session, database, old_database_name="analytics"
            )

        mock_rename.assert_called_once_with(
            session,
            "[analytics].(id:5)",
            "[analytics_v2].(id:5)",
        )


# ---------------------------------------------------------------------------
# on_dataset_created tests
# ---------------------------------------------------------------------------


class TestOnDatasetCreated:
    @pytest.mark.asyncio
    async def test_creates_datasource_and_schema_pvms(self) -> None:
        session = _make_mock_session()
        database = _make_mock_database(db_id=1, db_name="my_db")
        dataset = _make_mock_dataset(
            ds_id=10,
            table_name="users",
            database_id=1,
            schema="public",
            catalog=None,
            database=database,
        )

        calls: list[tuple[str, str | None]] = []

        async def mock_add(s, perm_name, vm_name):
            calls.append((perm_name, vm_name))
            return MagicMock(id=1)

        with patch.object(
            AsyncPermissionManager,
            "add_permission_view_menu",
            side_effect=mock_add,
        ):
            await AsyncPermissionManager.on_dataset_created(
                session, dataset, database=database
            )

        # Should create datasource_access + schema_access
        perm_names = [c[0] for c in calls]
        assert DATASOURCE_ACCESS in perm_names
        assert SCHEMA_ACCESS in perm_names
        # Verify permission strings
        ds_call = next(c for c in calls if c[0] == DATASOURCE_ACCESS)
        assert ds_call[1] == "[my_db].[users](id:10)"
        schema_call = next(c for c in calls if c[0] == SCHEMA_ACCESS)
        assert schema_call[1] == "[my_db].[public]"

    @pytest.mark.asyncio
    async def test_creates_catalog_pvm_when_catalog_set(self) -> None:
        session = _make_mock_session()
        database = _make_mock_database(db_id=1, db_name="trino_db")
        dataset = _make_mock_dataset(
            ds_id=20,
            table_name="events",
            database_id=1,
            schema="analytics",
            catalog="hive",
            database=database,
        )

        calls: list[tuple[str, str | None]] = []

        async def mock_add(s, perm_name, vm_name):
            calls.append((perm_name, vm_name))
            return MagicMock(id=1)

        with patch.object(
            AsyncPermissionManager,
            "add_permission_view_menu",
            side_effect=mock_add,
        ):
            await AsyncPermissionManager.on_dataset_created(
                session, dataset, database=database
            )

        perm_names = [c[0] for c in calls]
        assert DATASOURCE_ACCESS in perm_names
        assert SCHEMA_ACCESS in perm_names
        assert CATALOG_ACCESS in perm_names

        catalog_call = next(c for c in calls if c[0] == CATALOG_ACCESS)
        assert catalog_call[1] == "[trino_db].[hive]"

        schema_call = next(c for c in calls if c[0] == SCHEMA_ACCESS)
        assert schema_call[1] == "[trino_db].[hive].[analytics]"

    @pytest.mark.asyncio
    async def test_skips_schema_pvm_when_no_schema(self) -> None:
        session = _make_mock_session()
        database = _make_mock_database(db_id=1, db_name="my_db")
        dataset = _make_mock_dataset(
            ds_id=10,
            table_name="users",
            database_id=1,
            schema=None,
            catalog=None,
            database=database,
        )

        calls: list[tuple[str, str | None]] = []

        async def mock_add(s, perm_name, vm_name):
            calls.append((perm_name, vm_name))
            return MagicMock(id=1)

        with patch.object(
            AsyncPermissionManager,
            "add_permission_view_menu",
            side_effect=mock_add,
        ):
            await AsyncPermissionManager.on_dataset_created(
                session, dataset, database=database
            )

        perm_names = [c[0] for c in calls]
        assert DATASOURCE_ACCESS in perm_names
        assert SCHEMA_ACCESS not in perm_names


# ---------------------------------------------------------------------------
# on_dataset_deleted tests
# ---------------------------------------------------------------------------


class TestOnDatasetDeleted:
    @pytest.mark.asyncio
    async def test_deletes_datasource_access_pvm(self) -> None:
        session = _make_mock_session()
        database = _make_mock_database(db_id=1, db_name="my_db")
        dataset = _make_mock_dataset(
            ds_id=10,
            table_name="users",
            database_id=1,
            database=database,
        )

        with patch.object(
            AsyncPermissionManager,
            "del_permission_view_menu",
            new_callable=AsyncMock,
        ) as mock_del:
            await AsyncPermissionManager.on_dataset_deleted(
                session, dataset, database=database
            )

        mock_del.assert_called_once_with(
            session, DATASOURCE_ACCESS, "[my_db].[users](id:10)"
        )


# ---------------------------------------------------------------------------
# on_dataset_updated tests
# ---------------------------------------------------------------------------


class TestOnDatasetUpdated:
    @pytest.mark.asyncio
    async def test_noop_when_nothing_changed(self) -> None:
        session = _make_mock_session()
        database = _make_mock_database(db_id=1, db_name="my_db")
        dataset = _make_mock_dataset(
            ds_id=10,
            table_name="users",
            database_id=1,
            database=database,
        )

        with patch.object(
            AsyncPermissionManager,
            "add_permission_view_menu",
            new_callable=AsyncMock,
        ) as mock_add:
            await AsyncPermissionManager.on_dataset_updated(
                session,
                dataset,
                # No old_ values => nothing changed
            )

        # Nothing should be renamed
        mock_add.assert_not_called()

    @pytest.mark.asyncio
    async def test_renames_when_table_name_changes(self) -> None:
        session = _make_mock_session()
        database = _make_mock_database(db_id=1, db_name="my_db")
        dataset = _make_mock_dataset(
            ds_id=10,
            table_name="users_v2",
            database_id=1,
            database=database,
        )

        old_vm = MagicMock(id=100, name="[my_db].[users](id:10)")

        async def mock_find_vm(s, name):
            if name == "[my_db].[users_v2](id:10)":
                return None  # new name does not exist
            if name == "[my_db].[users](id:10)":
                return old_vm
            return None

        with (
            patch.object(
                AsyncPermissionManager,
                "_find_view_menu",
                side_effect=mock_find_vm,
            ),
            patch.object(
                AsyncPermissionManager,
                "_rename_view_menu",
                new_callable=AsyncMock,
            ) as mock_rename,
        ):
            await AsyncPermissionManager.on_dataset_updated(
                session,
                dataset,
                old_table_name="users",
            )

        mock_rename.assert_called_once_with(
            session,
            "[my_db].[users](id:10)",
            "[my_db].[users_v2](id:10)",
        )

    @pytest.mark.asyncio
    async def test_creates_new_schema_pvm_when_schema_changes(self) -> None:
        session = _make_mock_session()
        database = _make_mock_database(db_id=1, db_name="my_db")
        dataset = _make_mock_dataset(
            ds_id=10,
            table_name="users",
            database_id=1,
            schema="analytics",
            catalog=None,
            database=database,
        )

        calls: list[tuple[str, str | None]] = []

        async def mock_add(s, perm_name, vm_name):
            calls.append((perm_name, vm_name))
            return MagicMock(id=1)

        with patch.object(
            AsyncPermissionManager,
            "add_permission_view_menu",
            side_effect=mock_add,
        ):
            await AsyncPermissionManager.on_dataset_updated(
                session,
                dataset,
                old_schema="public",
            )

        perm_names = [c[0] for c in calls]
        assert SCHEMA_ACCESS in perm_names
        schema_call = next(c for c in calls if c[0] == SCHEMA_ACCESS)
        assert schema_call[1] == "[my_db].[analytics]"


# ---------------------------------------------------------------------------
# sync_database_permissions tests
# ---------------------------------------------------------------------------


class TestSyncDatabasePermissions:
    @pytest.mark.asyncio
    async def test_creates_database_and_dataset_pvms(self) -> None:
        session = _make_mock_session()
        database = _make_mock_database(db_id=1, db_name="my_db")

        ds1 = _make_mock_dataset(
            ds_id=10,
            table_name="users",
            schema="public",
            perm="[my_db].[users](id:10)",
        )
        ds2 = _make_mock_dataset(
            ds_id=20,
            table_name="orders",
            schema="public",
            perm=None,  # perm needs to be set
        )

        # Mock the dataset query to return our datasets
        dataset_result = MagicMock()
        dataset_result.scalars.return_value.all.return_value = [ds1, ds2]

        execute_results = []

        async def mock_execute(stmt, *args, **kwargs):
            # Return datasets for the SELECT query
            if len(execute_results) == 0:
                execute_results.append(True)
                return dataset_result
            # For UPDATE statements, return a default mock
            return MagicMock()

        session.execute = AsyncMock(side_effect=mock_execute)

        calls: list[tuple[str, str | None]] = []

        async def mock_add(s, perm_name, vm_name):
            calls.append((perm_name, vm_name))
            return MagicMock(id=1)

        with patch.object(
            AsyncPermissionManager,
            "add_permission_view_menu",
            side_effect=mock_add,
        ):
            result = await AsyncPermissionManager.sync_database_permissions(
                session, database
            )

        assert result["database"] == "my_db"
        assert result["datasets_scanned"] == 2

        perm_names = [c[0] for c in calls]
        assert DATABASE_ACCESS in perm_names
        assert DATASOURCE_ACCESS in perm_names
        assert SCHEMA_ACCESS in perm_names

        # Database perm
        db_call = next(c for c in calls if c[0] == DATABASE_ACCESS)
        assert db_call[1] == "[my_db].(id:1)"

        # Dataset perms
        ds_calls = [c for c in calls if c[0] == DATASOURCE_ACCESS]
        ds_vm_names = {c[1] for c in ds_calls}
        assert "[my_db].[users](id:10)" in ds_vm_names
        assert "[my_db].[orders](id:20)" in ds_vm_names

        # Schema perm (deduplicated - both datasets use 'public')
        schema_calls = [c for c in calls if c[0] == SCHEMA_ACCESS]
        assert len(schema_calls) == 1
        assert schema_calls[0][1] == "[my_db].[public]"
