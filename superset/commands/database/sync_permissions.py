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
"""Async port of ``superset_old/commands/database/sync_permissions.py``."""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from superset.commands.base import AsyncBaseCommand
from superset.exceptions import ObjectNotFoundError

if TYPE_CHECKING:
    from superset.db.daos.database import AsyncDatabaseDAO

logger = logging.getLogger(__name__)


class SyncPermissionsCommand(AsyncBaseCommand[dict[str, Any]]):
    """Sync database permissions.

    Ported from superset_old/commands/database/sync_permissions.py.
    Syncs catalog and schema permissions from the database to the
    security manager, creating new permission entries as needed.
    """

    def __init__(
        self,
        dao: AsyncDatabaseDAO,
        database_id: int,
        security_manager: Any | None = None,
        username: str | None = None,
    ) -> None:
        self._dao = dao
        self._database_id = database_id
        self._security_manager = security_manager
        self._username = username
        self._database: Any | None = None

    async def validate(self) -> None:
        self._database = await self._dao.find_by_id(self._database_id)
        if not self._database:
            raise ObjectNotFoundError("Database", self._database_id)

    async def run(self) -> dict[str, Any]:  # noqa: C901
        if self._security_manager is None:
            return {"message": "Security manager not provided"}

        catalog_perm_count = 0
        schema_perm_count = 0

        # Get catalog names from the database
        catalogs = await self._get_catalog_names()

        for catalog in catalogs:
            try:
                schemas = await self._get_schema_names(catalog)

                # Process catalog permissions
                if catalog:
                    perm = self._security_manager.get_catalog_perm(
                        self._database.database_name,
                        catalog,
                    )
                    existing_pvm = self._security_manager.find_permission_view_menu(
                        "catalog_access",
                        perm,
                    )
                    if not existing_pvm:
                        # New catalog - add permission
                        self._security_manager.add_permission_view_menu(
                            "catalog_access",
                            perm,
                        )
                        catalog_perm_count += 1

                        # Add schema permissions for this catalog
                        for schema in schemas:
                            schema_perm = self._security_manager.get_schema_perm(
                                self._database.database_name,
                                catalog,
                                schema,
                            )
                            existing_schema_pvm = (
                                self._security_manager.find_permission_view_menu(
                                    "schema_access",
                                    schema_perm,
                                )
                            )
                            if not existing_schema_pvm:
                                self._security_manager.add_permission_view_menu(
                                    "schema_access",
                                    schema_perm,
                                )
                                schema_perm_count += 1
                        continue

                # Add new schemas that don't have permissions yet
                for schema in schemas:
                    schema_perm = self._security_manager.get_schema_perm(
                        self._database.database_name,
                        catalog,
                        schema,
                    )
                    existing_schema_pvm = (
                        self._security_manager.find_permission_view_menu(
                            "schema_access",
                            schema_perm,
                        )
                    )
                    if not existing_schema_pvm:
                        self._security_manager.add_permission_view_menu(
                            "schema_access",
                            schema_perm,
                        )
                        schema_perm_count += 1

            except Exception:
                logger.warning(
                    "Error processing catalog %s",
                    catalog or "(default)",
                    exc_info=True,
                )
                continue

        await self._dao.session.flush()

        return {
            "message": "OK",
            "catalog_permissions_added": catalog_perm_count,
            "schema_permissions_added": schema_perm_count,
        }

    async def _get_catalog_names(self) -> set[str | None]:
        """Get all catalog names from the database."""
        if not getattr(self._database.db_engine_spec, "supports_catalog", False):
            return {None}

        try:
            # If the database doesn't support cross-catalog queries or
            # multi-catalog is not enabled, only use the default catalog
            if getattr(
                self._database.db_engine_spec, "supports_cross_catalog_queries", False
            ) or getattr(self._database, "allow_multi_catalog", False):
                return self._database.get_all_catalog_names(force=True)
            else:
                return {self._database.get_default_catalog()}
        except Exception:
            logger.warning(
                "Failed to get catalog names",
                exc_info=True,
            )
            return {None}

    async def _get_schema_names(self, catalog: str | None) -> set[str]:
        """Get all schema names for a catalog."""
        try:
            return self._database.get_all_schema_names(
                force=True,
                catalog=catalog,
            )
        except Exception:
            logger.warning(
                "Failed to get schema names for catalog %s",
                catalog or "(default)",
                exc_info=True,
            )
            return set()
