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
"""Command for listing tables and views accessible in a database schema.

* ``get_all_table_names_in_schema`` and ``get_all_view_names_in_schema``
  go through the model so cached results are honoured (cache-key shape:
  ``db:{id}:catalog:{c}:schema:{s}:{table_list,view_list}``);
* ``force`` / ``cache`` / ``cache_timeout`` are forwarded as kwargs;
* the catalog is defaulted via ``self._model.get_default_catalog()``
  when the caller didn't supply one;
* each ``(table, schema, catalog)`` triple is wrapped as a
  :class:`DatasourceName` tuple before being filtered by the
  per-user permission helper.
"""

from __future__ import annotations

import logging
from typing import Any

from superset.commands.base import AsyncBaseCommand
from superset.commands.database.exceptions import (
    DatabaseNotFoundError,
    DatabaseTablesUnexpectedError,
)
from superset.exceptions import SupersetException
from superset.utils.core import DatasourceName

logger = logging.getLogger(__name__)


class TablesDatabaseCommand(AsyncBaseCommand[dict[str, Any]]):
    """List tables / views accessible to the user for a given database."""

    _model: Any

    def __init__(
        self,
        dao: Any,
        db_id: int,
        catalog_name: str | None,
        schema_name: str,
        force: bool,
        security_manager: Any | None = None,
        user: Any | None = None,
    ) -> None:
        self._dao = dao
        self._db_id = db_id
        self._catalog_name = catalog_name
        self._schema_name = schema_name
        self._force = force
        self._security_manager = security_manager
        self._user = user
        self._model = None

    async def validate(self) -> None:
        self._model = await self._dao.find_by_id(self._db_id)
        if not self._model:
            raise DatabaseNotFoundError(self._db_id)

    async def run(self) -> dict[str, Any]:
        assert self._model is not None
        self._catalog_name = self._catalog_name or self._model.get_default_catalog()
        try:
            raw_tables = await self._model.get_all_table_names_in_schema(
                catalog=self._catalog_name,
                schema=self._schema_name,
                force=self._force,
                cache=self._model.table_cache_enabled,
                cache_timeout=self._model.table_cache_timeout,
            )
            raw_views = await self._model.get_all_view_names_in_schema(
                catalog=self._catalog_name,
                schema=self._schema_name,
                force=self._force,
                cache=self._model.table_cache_enabled,
                cache_timeout=self._model.table_cache_timeout,
            )

            tables = sorted(
                DatasourceName(*datasource_name) for datasource_name in raw_tables
            )
            views = sorted(
                DatasourceName(*datasource_name) for datasource_name in raw_views
            )

            # Guard ``filter_datasources_by_perms``: AsyncSecurityManager has a
            # different signature (returns perm strings, not filtered names).
            if self._security_manager is not None and hasattr(
                self._security_manager, "filter_datasources_by_perms"
            ):
                tables = await self._security_manager.filter_datasources_by_perms(
                    database=self._model,
                    catalog=self._catalog_name,
                    schema=self._schema_name,
                    datasource_names=tables,
                    user=self._user,
                )
                views = await self._security_manager.filter_datasources_by_perms(
                    database=self._model,
                    catalog=self._catalog_name,
                    schema=self._schema_name,
                    datasource_names=views,
                    user=self._user,
                )

            extra_lookup: dict[str, Any] = {}
            all_names = {t.table for t in tables}
            if all_names:
                extra_lookup = await self._dao.get_table_extra_lookup(
                    database_id=self._db_id,
                    table_names=all_names,
                    schema=self._schema_name,
                    catalog=self._catalog_name,
                )

            options = sorted(
                [
                    {
                        "value": table.table,
                        "type": "table",
                        "extra": extra_lookup.get(table.table, None),
                    }
                    for table in tables
                ]
                + [
                    {
                        "value": view.table,
                        "type": "view",
                    }
                    for view in views
                ],
                key=lambda item: str(item["value"] or ""),
            )

            return {"count": len(tables) + len(views), "result": options}
        except SupersetException:
            raise
        except Exception as ex:
            logger.warning(
                "Failed to fetch tables for database %s: %s", self._db_id, ex
            )
            raise DatabaseTablesUnexpectedError(str(ex)) from ex
