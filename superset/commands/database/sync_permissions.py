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
# mypy: ignore-errors
"""Async port of ``superset_old/commands/database/sync_permissions.py``.

Syncs catalog/schema permissions for a database connection.  Faithful to the
original ``SyncPermissionsCommand``: it (a) creates ``catalog_access`` /
``schema_access`` permissions for newly discovered catalogs/schemas, and (b)
when the connection was *renamed* (``old_db_connection_name`` differs from the
current name) renames the existing name-based view-menus and the dependent
``catalog_perm`` / ``schema_perm`` columns on datasets and charts.

It runs either inline (sync mode) or via the ``sync_database_permissions``
Celery task (when ``SYNC_DB_PERMISSIONS_IN_ASYNC_MODE`` is enabled), exactly
like the original ``run`` method.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any, TYPE_CHECKING

from superset.commands.base import AsyncBaseCommand
from superset.exceptions import OAuth2RedirectError

if TYPE_CHECKING:
    from superset.db.daos.database import AsyncDatabaseDAO

logger = logging.getLogger(__name__)


class SyncPermissionsCommand(AsyncBaseCommand[dict[str, Any]]):
    """Sync database permissions (catalog/schema), incl. rename handling."""

    def __init__(
        self,
        dao: "AsyncDatabaseDAO",
        database_id: int,
        security_manager: Any | None = None,
        username: str | None = None,
        old_db_connection_name: str | None = None,
        db_connection: Any | None = None,
        ssh_tunnel: Any | None = None,
    ) -> None:
        self._dao = dao
        self._database_id = database_id
        self._security_manager = security_manager
        self._username = username
        self._old_db_connection_name = old_db_connection_name
        self._database = db_connection
        self._ssh_tunnel = ssh_tunnel

    @property
    def old_db_connection_name(self) -> str:
        """Name the existing (pre-rename) permissions are keyed on.

        Defaults to the current database name, i.e. "no rename" — mirrors the
        original ``old_db_connection_name`` property.
        """
        return (
            self._old_db_connection_name
            if self._old_db_connection_name is not None
            else self._database.database_name
        )

    async def validate(self) -> None:
        import asyncio
        from contextlib import closing

        from superset.commands.database.exceptions import (
            DatabaseConnectionFailedError,
            DatabaseNotFoundError,
            MissingOAuth2TokenError,
            UserNotFoundInSessionError,
        )

        if self._database is None:
            self._database = await self._dao.find_by_id(self._database_id)
        if not self._database:
            raise DatabaseNotFoundError()

        if self._ssh_tunnel is None:
            self._ssh_tunnel = await self._dao.get_ssh_tunnel(self._database_id)

        # Need user info to impersonate for OAuth2 connections.
        user = None
        if self._username and self._security_manager is not None:
            user = await self._security_manager.dao.get_user_by_username(self._username)
        if not self._username or user is None:
            raise UserNotFoundInSessionError()

        # Pre-flight connectivity check — mirrors the original ``validate``'s
        # ``ping(engine)``, SSH-tunnelled connections included: the engine is
        # built through the tunnel via ``override_ssh_tunnel``.
        database = self._database
        ssh_tunnel = self._ssh_tunnel

        def _ping() -> bool:
            with database.get_sqla_engine(override_ssh_tunnel=ssh_tunnel) as engine:
                with closing(engine.raw_connection()) as conn:
                    return engine.dialect.do_ping(conn)

        try:
            alive = await asyncio.to_thread(_ping)
        except Exception as err:  # noqa: BLE001
            if database.is_oauth2_enabled() and (
                database.db_engine_spec.needs_oauth2(err)
            ):
                raise MissingOAuth2TokenError() from err
            raise DatabaseConnectionFailedError() from err
        if not alive:
            raise DatabaseConnectionFailedError()

    async def run(self) -> dict[str, Any]:
        """Trigger the perm sync in sync or async mode (mirrors original ``run``)."""
        if self._security_manager is None:
            return {"message": "Security manager not provided"}

        if self._is_async_mode():
            from superset.tasks.sync_database_permissions import (
                sync_database_permissions_task,
            )

            sync_database_permissions_task.delay(
                self._database_id,
                self._username,
                self.old_db_connection_name,
            )
            return {"message": "Permission sync dispatched asynchronously"}

        return await self.sync_database_permissions()

    @staticmethod
    def _is_async_mode() -> bool:
        try:
            from superset.config import SupersetSettings

            settings = SupersetSettings()  # type: ignore[call-arg]
            return bool(getattr(settings, "sync_db_permissions_in_async_mode", False))
        except Exception:  # noqa: BLE001
            return False

    async def sync_database_permissions(self) -> dict[str, Any]:  # noqa: C901
        """Sync the permissions for a DB connection (the inline worker path)."""
        sm = self._security_manager
        db_name = self._database.database_name
        old_name = self.old_db_connection_name
        catalog_perm_count = 0
        schema_perm_count = 0

        for catalog in await self._get_catalog_names():
            try:
                schemas = await self._get_schema_names(catalog)

                if catalog:
                    # The existing catalog permission is keyed on the OLD name.
                    perm = sm.get_catalog_perm(old_name, catalog)
                    existing_pvm = await sm.find_permission_view_menu(
                        "catalog_access",
                        perm,
                    )
                    if not existing_pvm:
                        # New catalog — add catalog + schema perms (new name).
                        await sm.add_permission_view_menu(
                            "catalog_access",
                            sm.get_catalog_perm(db_name, catalog),
                        )
                        catalog_perm_count += 1
                        for schema in schemas:
                            await sm.add_permission_view_menu(
                                "schema_access",
                                sm.get_schema_perm(db_name, schema, catalog=catalog),
                            )
                            schema_perm_count += 1
                        continue
            except OAuth2RedirectError:
                # raise OAuth2 exceptions as-is
                raise
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Error processing catalog %s",
                    catalog or "(default)",
                    exc_info=True,
                )
                continue

            # add possible new schemas in catalog
            schema_perm_count += await self._refresh_schemas(catalog, schemas)

            if old_name != db_name:
                await self._rename_database_in_permissions(catalog, schemas)

        await self._dao.session.flush()

        return {
            "message": "OK",
            "catalog_permissions_added": catalog_perm_count,
            "schema_permissions_added": schema_perm_count,
        }

    async def _get_catalog_names(self) -> set[str | None]:
        """Load catalog names. Mirrors original ``_get_catalog_names``.

        The new ``Database`` model exposes ``get_inspector`` (sync) rather than
        ``get_all_catalog_names``; we replicate the original logic and run the
        blocking inspector calls in a thread.  OAuth2 redirects propagate as-is.
        """
        import asyncio

        db = self._database
        if not getattr(db.db_engine_spec, "supports_catalog", False):
            return {None}

        try:
            # Adding permissions to all catalogs (and their schemas) can be slow.
            # If the database does not support cross-catalog queries and the
            # multi-catalog feature is not enabled, only the default catalog
            # needs permissions.
            if not (
                getattr(db.db_engine_spec, "supports_cross_catalog_queries", False)
                or getattr(db, "allow_multi_catalog", False)
            ):
                default_catalog = await asyncio.to_thread(db.get_default_catalog)
                return {default_catalog}

            def _fetch_catalogs() -> set[str]:
                with db.get_inspector() as inspector:
                    return db.db_engine_spec.get_catalog_names(db, inspector)

            return await asyncio.to_thread(_fetch_catalogs)
        except OAuth2RedirectError:
            # raise OAuth2 exceptions as-is
            raise
        except Exception:  # noqa: BLE001
            logger.warning("Failed to get catalog names", exc_info=True)
            return {None}

    async def _get_schema_names(self, catalog: str | None) -> set[str]:
        """Load schema names for a catalog. Mirrors original ``_get_schema_names``."""
        import asyncio

        db = self._database

        try:

            def _fetch_schemas() -> set[str]:
                with db.get_inspector(catalog=catalog) as inspector:
                    return db.db_engine_spec.get_schema_names(inspector)

            return await asyncio.to_thread(_fetch_schemas)
        except OAuth2RedirectError:
            # raise OAuth2 exceptions as-is
            raise
        except Exception:  # noqa: BLE001
            logger.warning(
                "Failed to get schema names for catalog %s",
                catalog or "(default)",
                exc_info=True,
            )
            return set()

    async def _refresh_schemas(
        self, catalog: str | None, schemas: Iterable[str]
    ) -> int:
        """Add new schemas that don't have permissions yet.

        "Existing" is keyed on the OLD connection name, exactly like the
        original ``_refresh_schemas``.  Returns the number of perms added.
        """
        sm = self._security_manager
        added = 0
        for schema in schemas:
            perm = sm.get_schema_perm(
                self.old_db_connection_name, schema, catalog=catalog
            )
            existing_pvm = await sm.find_permission_view_menu("schema_access", perm)
            if not existing_pvm:
                new_name = sm.get_schema_perm(
                    self._database.database_name, schema, catalog=catalog
                )
                await sm.add_permission_view_menu("schema_access", new_name)
                added += 1
        return added

    async def _rename_database_in_permissions(
        self, catalog: str | None, schemas: Iterable[str]
    ) -> None:
        """Rename name-based perms + dependent dataset/chart perm columns.

        Mirrors the original ``_rename_database_in_permissions``.  The catalog
        permission is re-pointed to a find-or-created new-named view-menu (via
        ``view_menu_id`` to avoid lazy-loading ``PermissionView.view_menu``);
        the schema view-menu is renamed in place exactly as the original does
        (``existing_pvm.view_menu.name = new``).  Both reach the same end state:
        the ``catalog_access`` / ``schema_access`` permission keyed on the new
        name, plus the dependent dataset/chart ``catalog_perm`` / ``schema_perm``.
        """
        from superset.db.daos.dataset import AsyncDatasetDAO

        sm = self._security_manager
        db_name = self._database.database_name
        old_name = self.old_db_connection_name

        new_catalog_perm_name = (
            sm.get_catalog_perm(db_name, catalog) if catalog else None
        )

        # rename existing catalog permission: find-or-create the new-named
        # view-menu and re-point the ``catalog_access`` permission-view at it.
        # Mirrors the original's ``add_vm(new)`` + ``existing_pvm.view_menu = new``;
        # we set ``view_menu_id`` to avoid lazy-loading the relationship under
        # AsyncSession, and find-or-create avoids a UNIQUE(``ab_view_menu.name``)
        # collision when a view-menu with the new name already exists.
        if catalog:
            new_catalog_vm = await sm.add_view_menu(new_catalog_perm_name)
            old_catalog_perm = sm.get_catalog_perm(old_name, catalog)
            existing_pvm = await sm.find_permission_view_menu(
                "catalog_access", old_catalog_perm
            )
            if existing_pvm and new_catalog_vm:
                existing_pvm.view_menu_id = new_catalog_vm.id

        dataset_dao = AsyncDatasetDAO(self._dao.session)
        for schema in schemas:
            new_schema_perm_name = sm.get_schema_perm(
                db_name, schema, catalog=catalog
            )

            # rename existing schema view-menu
            old_schema_perm = sm.get_schema_perm(old_name, schema, catalog=catalog)
            existing_vm = await sm.find_view_menu(old_schema_perm)
            if existing_vm:
                existing_vm.name = new_schema_perm_name

            # rename permissions on datasets and charts
            datasets = await self._dao.get_datasets(
                self._database_id,
                catalog=catalog,
                schema=schema,
            )
            for dataset in datasets:
                dataset.catalog_perm = new_catalog_perm_name
                dataset.schema_perm = new_schema_perm_name
                related = await dataset_dao.get_related_objects(dataset.id)
                for chart in related.get("charts", []):
                    chart.catalog_perm = new_catalog_perm_name
                    chart.schema_perm = new_schema_perm_name
