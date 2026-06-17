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
"""Async permission management for ab_permission, ab_view_menu, ab_permission_view.

Manages RBAC permission records when Database/Dataset objects are created,
updated, or deleted.  This is the async equivalent of the SQLAlchemy event
hooks in the original Superset SecurityManager (database_after_insert,
dataset_after_insert, etc.) but is called explicitly from Commands rather
than via ORM event listeners -- this is intentional to avoid mixing sync
event hooks with async sessions.

Permission string formats (matching original Superset):
  - Database:  ``[db_name].(id:N)``
  - Dataset:   ``[db_name].[table_name](id:N)``
  - Schema:    ``[db_name].[schema]`` or ``[db_name].[catalog].[schema]``
  - Catalog:   ``[db_name].[catalog]``
"""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from superset.models.security import (
    ab_permission_view_role,
    Permission,
    PermissionView,
    ViewMenu,
)
from superset.security.permissions import (
    CATALOG_ACCESS,
    DATABASE_ACCESS,
    DATASOURCE_ACCESS,
    SCHEMA_ACCESS,
)

if TYPE_CHECKING:
    from superset.models.connectors import SqlaTable
    from superset.models.core import Database

logger = logging.getLogger(__name__)


def get_database_perm(database_id: Any, database_name: Any) -> str:
    """Format: ``[db_name].(id:N)``."""
    return f"[{database_name}].(id:{database_id})"


def get_dataset_perm(dataset_id: Any, dataset_name: Any, database_name: Any) -> str:
    """Format: ``[db_name].[table_name](id:N)``."""
    return f"[{database_name}].[{dataset_name}](id:{dataset_id})"


def get_schema_perm(
    database_name: Any,
    catalog: Any = None,
    schema: Any = None,
) -> str | None:
    """Format: ``[db].[schema]`` or ``[db].[catalog].[schema]``.

    Returns None if schema is None.
    """
    if schema is None:
        return None
    if catalog:
        return f"[{database_name}].[{catalog}].[{schema}]"
    return f"[{database_name}].[{schema}]"


def get_catalog_perm(database_name: Any, catalog: Any = None) -> str | None:
    """Format: ``[db_name].[catalog]``.

    Returns None if catalog is None.
    """
    if catalog is None:
        return None
    return f"[{database_name}].[{catalog}]"


class AsyncPermissionManager:
    """Manages ab_permission, ab_view_menu, ab_permission_view records.

    All methods accept an AsyncSession and operate within the caller's
    transaction boundary (flush, not commit).  This keeps the permission
    manager composable with Command-level transaction management.
    """

    # ------------------------------------------------------------------
    # Low-level helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def _find_permission(session: AsyncSession, name: str) -> Permission | None:
        """Find a Permission by name."""
        result = await session.execute(
            select(Permission).where(Permission.name == name)
        )
        return result.scalars().one_or_none()

    @staticmethod
    async def _find_view_menu(session: AsyncSession, name: str) -> ViewMenu | None:
        """Find a ViewMenu by name."""
        result = await session.execute(select(ViewMenu).where(ViewMenu.name == name))
        return result.scalars().one_or_none()

    @staticmethod
    async def _find_permission_view_menu(
        session: AsyncSession,
        permission_name: str,
        view_menu_name: str,
    ) -> PermissionView | None:
        """Find a PermissionView by permission + view_menu names."""
        result = await session.execute(
            select(PermissionView)
            .join(Permission, PermissionView.permission_id == Permission.id)
            .join(ViewMenu, PermissionView.view_menu_id == ViewMenu.id)
            .where(
                Permission.name == permission_name,
                ViewMenu.name == view_menu_name,
            )
        )
        return result.scalars().one_or_none()

    @staticmethod
    async def _find_permission_view_by_id(
        session: AsyncSession,
        permission_id: int,
        view_menu_id: int,
    ) -> PermissionView | None:
        """Find a PermissionView by foreign key IDs."""
        result = await session.execute(
            select(PermissionView).where(
                PermissionView.permission_id == permission_id,
                PermissionView.view_menu_id == view_menu_id,
            )
        )
        return result.scalars().one_or_none()

    # ------------------------------------------------------------------
    # Public PVM CRUD
    # ------------------------------------------------------------------

    @staticmethod
    async def add_permission_view_menu(
        session: AsyncSession,
        permission_name: str,
        view_menu_name: str | None,
    ) -> PermissionView | None:
        """Create or find Permission + ViewMenu, create PermissionView if missing.

        Returns the existing or newly created PermissionView, or None if
        ``view_menu_name`` is None/empty.
        """
        if not view_menu_name:
            return None

        # Check for existing PVM
        existing = await AsyncPermissionManager._find_permission_view_menu(
            session, permission_name, view_menu_name
        )
        if existing is not None:
            return existing

        # Find or create Permission
        permission = await AsyncPermissionManager._find_permission(
            session, permission_name
        )
        if permission is None:
            permission = Permission(name=permission_name)
            session.add(permission)
            await session.flush()

        # Find or create ViewMenu
        view_menu = await AsyncPermissionManager._find_view_menu(
            session, view_menu_name
        )
        if view_menu is None:
            view_menu = ViewMenu(name=view_menu_name)
            session.add(view_menu)
            await session.flush()

        # Create PermissionView
        pvm = PermissionView(
            permission_id=permission.id,
            view_menu_id=view_menu.id,
        )
        session.add(pvm)
        await session.flush()

        logger.info(
            "Created PVM: permission=%s, view_menu=%s (pvm_id=%s)",
            permission_name,
            view_menu_name,
            pvm.id,
        )
        return pvm

    @staticmethod
    async def del_permission_view_menu(
        session: AsyncSession,
        permission_name: str,
        view_menu_name: str,
    ) -> None:
        """Delete PermissionView + role associations + orphaned ViewMenu.

        1. Remove all role associations (ab_permission_view_role)
        2. Delete the PermissionView row
        3. Delete the orphaned ViewMenu row
        """
        pvm = await AsyncPermissionManager._find_permission_view_menu(
            session, permission_name, view_menu_name
        )
        if pvm is None:
            return

        await AsyncPermissionManager._delete_pvm(session, pvm)

    @staticmethod
    async def _delete_pvm(session: AsyncSession, pvm: PermissionView) -> None:
        """Delete a specific PermissionView and its role associations."""
        view_menu_id = pvm.view_menu_id

        # 1. Delete role associations
        await session.execute(
            delete(ab_permission_view_role).where(
                ab_permission_view_role.c.permission_view_id == pvm.id
            )
        )

        # 2. Delete the PVM itself
        await session.execute(delete(PermissionView).where(PermissionView.id == pvm.id))

        # 3. Delete orphaned ViewMenu (if no other PVMs reference it)
        remaining = await session.execute(
            select(PermissionView.id).where(PermissionView.view_menu_id == view_menu_id)
        )
        if remaining.scalars().first() is None:
            await session.execute(delete(ViewMenu).where(ViewMenu.id == view_menu_id))

        await session.flush()

        logger.info("Deleted PVM id=%s (view_menu_id=%s)", pvm.id, view_menu_id)

    @staticmethod
    async def _rename_view_menu(
        session: AsyncSession,
        old_name: str,
        new_name: str,
    ) -> ViewMenu | None:
        """Rename a ViewMenu entry. Returns the updated ViewMenu or None."""
        vm = await AsyncPermissionManager._find_view_menu(session, old_name)
        if vm is None:
            return None

        # Check if target name already exists
        existing = await AsyncPermissionManager._find_view_menu(session, new_name)
        if existing is not None:
            logger.warning(
                "Target ViewMenu '%s' already exists; cannot rename from '%s'",
                new_name,
                old_name,
            )
            return existing

        await session.execute(
            update(ViewMenu).where(ViewMenu.id == vm.id).values(name=new_name)
        )
        await session.flush()

        logger.info("Renamed ViewMenu '%s' -> '%s'", old_name, new_name)
        # Re-fetch to get updated state
        return await AsyncPermissionManager._find_view_menu(session, new_name)

    # ------------------------------------------------------------------
    # Database lifecycle
    # ------------------------------------------------------------------

    @staticmethod
    async def on_database_created(session: AsyncSession, database: Database) -> None:
        """Create database_access PVM when a database is created.

        Equivalent of ``database_after_insert`` in original Superset.
        """
        perm = get_database_perm(database.id, database.database_name)
        await AsyncPermissionManager.add_permission_view_menu(
            session, DATABASE_ACCESS, perm
        )
        logger.info(
            "on_database_created: created PVM for database '%s' (id=%d)",
            database.database_name,
            database.id,
        )

    @staticmethod
    async def on_database_deleted(session: AsyncSession, database: Database) -> None:
        """Delete database_access PVM + schema/catalog PVMs when a database is deleted.

        Equivalent of ``database_after_delete`` -> ``_delete_vm_database_access``
        in original Superset.
        """
        db_perm = get_database_perm(database.id, database.database_name)

        # Delete the database_access PVM
        await AsyncPermissionManager.del_permission_view_menu(
            session, DATABASE_ACCESS, db_perm
        )

        # Delete all schema_access and catalog_access PVMs for this database
        # Pattern: [db_name].[...] matches both schema and catalog perms
        db_prefix = f"[{database.database_name}].[%]"
        schema_catalog_pvms = await session.execute(
            select(PermissionView)
            .join(Permission, PermissionView.permission_id == Permission.id)
            .join(ViewMenu, PermissionView.view_menu_id == ViewMenu.id)
            .where(
                or_(
                    Permission.name == SCHEMA_ACCESS,
                    Permission.name == CATALOG_ACCESS,
                )
            )
            .where(ViewMenu.name.like(db_prefix))
        )
        for pvm in schema_catalog_pvms.scalars().all():
            await AsyncPermissionManager._delete_pvm(session, pvm)

        logger.info(
            "on_database_deleted: removed PVMs for database '%s' (id=%d)",
            database.database_name,
            database.id,
        )

    @staticmethod
    async def on_database_updated(
        session: AsyncSession,
        database: Database,
        old_database_name: str | None = None,
    ) -> None:
        """Rename database_access + datasource_access PVMs when database name changes.

        Equivalent of ``database_after_update`` in original Superset.
        If ``old_database_name`` is None, this is a no-op (name did not change).
        """
        if old_database_name is None:
            return
        if old_database_name == database.database_name:
            return

        new_name = database.database_name

        # 1. Rename database_access ViewMenu
        old_db_perm = get_database_perm(database.id, old_database_name)
        new_db_perm = get_database_perm(database.id, new_name)

        existing_new = await AsyncPermissionManager._find_permission_view_menu(
            session, DATABASE_ACCESS, new_db_perm
        )
        if existing_new:
            # Target already exists -- delete the old database_access PVM AND all
            # schema_access / catalog_access PVMs that reference the old database name.
            logger.info(
                "New database perm '%s' already exists; deleting old '%s'",
                new_db_perm,
                old_db_perm,
            )
            await AsyncPermissionManager.del_permission_view_menu(
                session, DATABASE_ACCESS, old_db_perm
            )
            # Clean up stale schema_access and catalog_access PVMs for the old name
            db_prefix = f"[{old_database_name}].[%]"
            schema_catalog_pvms = await session.execute(
                select(PermissionView)
                .join(Permission, PermissionView.permission_id == Permission.id)
                .join(ViewMenu, PermissionView.view_menu_id == ViewMenu.id)
                .where(
                    or_(
                        Permission.name == SCHEMA_ACCESS,
                        Permission.name == CATALOG_ACCESS,
                    )
                )
                .where(ViewMenu.name.like(db_prefix))
            )
            for pvm in schema_catalog_pvms.scalars().all():
                await AsyncPermissionManager._delete_pvm(session, pvm)
        else:
            old_pvm = await AsyncPermissionManager._find_permission_view_menu(
                session, DATABASE_ACCESS, old_db_perm
            )
            if old_pvm:
                await AsyncPermissionManager._rename_view_menu(
                    session, old_db_perm, new_db_perm
                )
            else:
                logger.warning(
                    "Could not find old database perm '%s'; creating new",
                    old_db_perm,
                )
                await AsyncPermissionManager.add_permission_view_menu(
                    session, DATABASE_ACCESS, new_db_perm
                )

        # 2. Rename all datasource_access ViewMenus that reference this database
        await AsyncPermissionManager._rename_datasource_perms_for_database(
            session, database, old_database_name
        )

        logger.info(
            "on_database_updated: renamed PVMs from '%s' to '%s'",
            old_database_name,
            new_name,
        )

    @staticmethod
    async def _rename_datasource_perms_for_database(
        session: AsyncSession,
        database: "Database",
        old_database_name: str,
    ) -> None:
        """Rename all datasource_access ViewMenus when a database name changes.

        Also updates the ``perm`` field on SqlaTable and Slice rows.
        Equivalent of ``_update_vm_datasources_access`` in original Superset.
        """
        from superset.models.connectors import SqlaTable
        from superset.models.slice import Slice

        new_database_name = database.database_name

        # Find all datasets belonging to this database
        datasets_result = await session.execute(
            select(SqlaTable).where(SqlaTable.database_id == database.id)
        )
        datasets = datasets_result.scalars().all()

        for dataset in datasets:
            old_vm_name = get_dataset_perm(
                dataset.id, dataset.table_name, old_database_name
            )
            new_vm_name = get_dataset_perm(
                dataset.id, dataset.table_name, new_database_name
            )

            # Check if new name already exists
            existing_new = await AsyncPermissionManager._find_view_menu(
                session, new_vm_name
            )
            if existing_new:
                continue

            # Rename ViewMenu
            await AsyncPermissionManager._rename_view_menu(
                session, old_vm_name, new_vm_name
            )

            # Update SqlaTable.perm
            await session.execute(
                update(SqlaTable)
                .where(
                    SqlaTable.id == dataset.id,
                    SqlaTable.perm == old_vm_name,
                )
                .values(perm=new_vm_name)
            )

            # Update Slice.perm for charts using this dataset
            await session.execute(
                update(Slice).where(Slice.perm == old_vm_name).values(perm=new_vm_name)
            )

        await session.flush()

    # ------------------------------------------------------------------
    # Dataset lifecycle
    # ------------------------------------------------------------------

    @staticmethod
    async def on_dataset_created(
        session: AsyncSession,
        dataset: SqlaTable,
        database: Database | None = None,
    ) -> None:
        """Create datasource_access + schema_access + catalog_access PVMs.

        Equivalent of ``dataset_after_insert`` in original Superset.
        Also sets the ``perm``, ``schema_perm``, and ``catalog_perm`` fields
        on the dataset row.
        """
        from superset.models.connectors import SqlaTable
        from superset.models.core import Database as DatabaseModel

        # Resolve database
        if database is None:
            db_result = await session.execute(
                select(DatabaseModel).where(DatabaseModel.id == dataset.database_id)
            )
            database = db_result.scalars().one_or_none()
            if database is None:
                logger.error(
                    "on_dataset_created: database_id=%d not found for dataset %d",
                    dataset.database_id,
                    dataset.id,
                )
                return

        db_name = database.database_name

        # 1. Create datasource_access PVM
        dataset_perm = get_dataset_perm(dataset.id, dataset.table_name, db_name)
        await AsyncPermissionManager.add_permission_view_menu(
            session, DATASOURCE_ACCESS, dataset_perm
        )

        # Update perm field on dataset
        update_values: dict[str, Any] = {}
        if getattr(dataset, "perm", None) != dataset_perm:
            update_values["perm"] = dataset_perm

        # 2. Create schema_access PVM if schema is set
        schema = getattr(dataset, "schema", None)
        catalog = getattr(dataset, "catalog", None)
        if schema:
            schema_perm = get_schema_perm(db_name, catalog, schema)
            if schema_perm:
                await AsyncPermissionManager.add_permission_view_menu(
                    session, SCHEMA_ACCESS, schema_perm
                )
                update_values["schema_perm"] = schema_perm

        # 3. Create catalog_access PVM if catalog is set
        if catalog:
            catalog_perm_str = get_catalog_perm(db_name, catalog)
            if catalog_perm_str:
                await AsyncPermissionManager.add_permission_view_menu(
                    session, CATALOG_ACCESS, catalog_perm_str
                )
                update_values["catalog_perm"] = catalog_perm_str

        # Batch-update dataset fields
        if update_values:
            await session.execute(
                update(SqlaTable)
                .where(SqlaTable.id == dataset.id)
                .values(**update_values)
            )
            await session.flush()

        logger.info(
            "on_dataset_created: created PVMs for dataset '%s' (id=%d, db='%s')",
            dataset.table_name,
            dataset.id,
            db_name,
        )

    @staticmethod
    async def on_dataset_deleted(
        session: AsyncSession,
        dataset: SqlaTable,
        database: Database | None = None,
    ) -> None:
        """Delete datasource_access PVM when a dataset is deleted.

        Equivalent of ``dataset_after_delete`` in original Superset.
        Schema and catalog PVMs are NOT deleted because they may be shared
        by other datasets.
        """
        from superset.models.core import Database as DatabaseModel

        # Resolve database name
        if database is None:
            db = getattr(dataset, "database", None)
            if db is None:
                db_result = await session.execute(
                    select(DatabaseModel).where(DatabaseModel.id == dataset.database_id)
                )
                db = db_result.scalars().one_or_none()
            database = db

        if database is None:
            logger.warning(
                "on_dataset_deleted: could not resolve database for dataset %d",
                dataset.id,
            )
            return

        db_name = database.database_name
        dataset_perm = get_dataset_perm(dataset.id, dataset.table_name, db_name)
        await AsyncPermissionManager.del_permission_view_menu(
            session, DATASOURCE_ACCESS, dataset_perm
        )

        logger.info(
            "on_dataset_deleted: removed PVM for dataset '%s' (id=%d)",
            dataset.table_name,
            dataset.id,
        )

    @staticmethod
    async def on_dataset_updated(
        session: AsyncSession,
        dataset: SqlaTable,
        *,
        old_table_name: str | None = None,
        old_database_id: int | None = None,
        old_database_name: str | None = None,
        old_schema: str | None = None,
        old_catalog: str | None = None,
    ) -> None:
        """Rename PVMs if dataset name, schema, database, or catalog changed.

        Equivalent of ``dataset_before_update`` in original Superset.
        Also propagates perm/schema_perm/catalog_perm changes to the
        SqlaTable and Slice (chart) rows.

        Callers should pass the old values of any field that changed.
        """
        from superset.models.connectors import SqlaTable
        from superset.models.core import Database as DatabaseModel
        from superset.models.slice import Slice

        # Resolve current database
        db = getattr(dataset, "database", None)
        if db is None:
            db_result = await session.execute(
                select(DatabaseModel).where(DatabaseModel.id == dataset.database_id)
            )
            db = db_result.scalars().one_or_none()
        if db is None:
            logger.error(
                "on_dataset_updated: cannot resolve database for dataset %d",
                dataset.id,
            )
            return

        current_db_name = db.database_name
        current_table_name = dataset.table_name

        # Determine effective old values
        eff_old_db_name = old_database_name or current_db_name
        eff_old_table_name = old_table_name or current_table_name

        # ------------------------------------------------------------------
        # 1. Rename datasource_access PVM if database or table name changed
        # ------------------------------------------------------------------
        db_changed = (
            old_database_id is not None and old_database_id != dataset.database_id
        )
        table_name_changed = (
            old_table_name is not None and old_table_name != current_table_name
        )

        if db_changed or table_name_changed:
            old_perm = get_dataset_perm(dataset.id, eff_old_table_name, eff_old_db_name)
            new_perm = get_dataset_perm(dataset.id, current_table_name, current_db_name)

            # When the target ViewMenu already exists there is nothing to do —
            # skip all subsequent rename/update work.
            existing_new_vm = await AsyncPermissionManager._find_view_menu(
                session, new_perm
            )
            if existing_new_vm:
                pass  # New ViewMenu already in place — no rename or perm-field updates.
            else:
                old_vm = await AsyncPermissionManager._find_view_menu(session, old_perm)
                if old_vm:
                    await AsyncPermissionManager._rename_view_menu(
                        session, old_perm, new_perm
                    )

                    # Update SqlaTable.perm — only on the RENAME path; when the
                    # old ViewMenu is missing the early-return skips perm/Slice.perm.
                    await session.execute(
                        update(SqlaTable)
                        .where(SqlaTable.id == dataset.id)
                        .values(perm=new_perm)
                    )

                    # Update Slice.perm for charts using this dataset
                    await session.execute(
                        update(Slice)
                        .where(
                            Slice.datasource_id == dataset.id,
                            Slice.datasource_type == "table",
                        )
                        .values(perm=new_perm)
                    )
                else:
                    logger.warning(
                        "Could not find old dataset perm '%s'; creating new",
                        old_perm,
                    )
                    await AsyncPermissionManager.add_permission_view_menu(
                        session, DATASOURCE_ACCESS, new_perm
                    )

        # ------------------------------------------------------------------
        # 2. Update schema/catalog PVMs if schema, catalog, or database changed
        # ------------------------------------------------------------------
        current_schema = getattr(dataset, "schema", None)
        current_catalog = getattr(dataset, "catalog", None)

        schema_changed = old_schema is not None and old_schema != current_schema
        catalog_changed = old_catalog is not None and old_catalog != current_catalog

        if db_changed or schema_changed or catalog_changed:
            # Create new schema/catalog PVMs (idempotent)
            new_catalog_perm = get_catalog_perm(current_db_name, current_catalog)
            new_schema_perm = get_schema_perm(
                current_db_name, current_catalog, current_schema
            )

            if new_catalog_perm:
                await AsyncPermissionManager.add_permission_view_menu(
                    session, CATALOG_ACCESS, new_catalog_perm
                )
            if new_schema_perm:
                await AsyncPermissionManager.add_permission_view_menu(
                    session, SCHEMA_ACCESS, new_schema_perm
                )

            # Update dataset perm fields
            await session.execute(
                update(SqlaTable)
                .where(SqlaTable.id == dataset.id)
                .values(
                    catalog_perm=new_catalog_perm,
                    schema_perm=new_schema_perm,
                )
            )

            # Update charts
            await session.execute(
                update(Slice)
                .where(
                    Slice.datasource_id == dataset.id,
                    Slice.datasource_type == "table",
                )
                .values(
                    catalog_perm=new_catalog_perm,
                    schema_perm=new_schema_perm,
                )
            )

        await session.flush()

        logger.info(
            "on_dataset_updated: updated PVMs for dataset '%s' (id=%d)",
            dataset.table_name,
            dataset.id,
        )

    # ------------------------------------------------------------------
    # Bulk sync (for SyncPermissionsCommand)
    # ------------------------------------------------------------------

    @staticmethod
    async def sync_database_permissions(  # noqa: C901
        session: AsyncSession, database: Database
    ) -> dict[str, Any]:
        """Ensure all PVMs exist for a database and its datasets.

        Creates any missing PVMs without deleting existing ones.
        Returns a summary dict with counts.
        """
        from superset.models.connectors import SqlaTable

        db_name = database.database_name
        created_count = 0

        # 1. Ensure database_access PVM exists
        db_perm = get_database_perm(database.id, db_name)
        pvm = await AsyncPermissionManager.add_permission_view_menu(
            session, DATABASE_ACCESS, db_perm
        )
        if pvm is not None:
            created_count += 1

        # 2. Ensure all dataset PVMs exist
        datasets_result = await session.execute(
            select(SqlaTable).where(SqlaTable.database_id == database.id)
        )
        datasets = datasets_result.scalars().all()

        schemas_seen: set[str] = set()
        catalogs_seen: set[str] = set()

        for dataset in datasets:
            # datasource_access
            ds_perm = get_dataset_perm(dataset.id, dataset.table_name, db_name)
            pvm = await AsyncPermissionManager.add_permission_view_menu(
                session, DATASOURCE_ACCESS, ds_perm
            )
            if pvm is not None:
                created_count += 1

            # Update perm field if needed
            if getattr(dataset, "perm", None) != ds_perm:
                await session.execute(
                    update(SqlaTable)
                    .where(SqlaTable.id == dataset.id)
                    .values(perm=ds_perm)
                )

            # schema_access
            schema = getattr(dataset, "schema", None)
            catalog = getattr(dataset, "catalog", None)
            if schema:
                schema_key = get_schema_perm(db_name, catalog, schema) or ""
                if schema_key and schema_key not in schemas_seen:
                    schemas_seen.add(schema_key)
                    pvm = await AsyncPermissionManager.add_permission_view_menu(
                        session, SCHEMA_ACCESS, schema_key
                    )
                    if pvm is not None:
                        created_count += 1

            # catalog_access
            if catalog:
                catalog_key = get_catalog_perm(db_name, catalog) or ""
                if catalog_key and catalog_key not in catalogs_seen:
                    catalogs_seen.add(catalog_key)
                    pvm = await AsyncPermissionManager.add_permission_view_menu(
                        session, CATALOG_ACCESS, catalog_key
                    )
                    if pvm is not None:
                        created_count += 1

        await session.flush()

        return {
            "database": db_name,
            "datasets_scanned": len(datasets),
            "pvms_ensured": created_count,
            "schemas": len(schemas_seen),
            "catalogs": len(catalogs_seen),
        }
