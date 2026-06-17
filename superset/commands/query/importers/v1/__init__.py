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
"""Import command for saved queries (v1 bundle format)."""

from __future__ import annotations

import io
from typing import Any, TYPE_CHECKING

from superset.commands.chart.importers.v1.utils import _import_database
from superset.commands.query.importers.v1.utils import import_saved_query
from superset.exceptions import CommandInvalidError
from superset.importexport.import_base import AsyncImportModelsCommand

if TYPE_CHECKING:
    from superset.typing import CRUDDAOProtocol


class ImportSavedQueriesCommand(AsyncImportModelsCommand):
    """Import Saved Queries.

    Resolves database dependencies (each saved query references a
    ``database_uuid``), imports any new databases first, then upserts the
    saved queries with the resolved ``db_id``.
    """

    _expected_type = "SavedQuery"

    def __init__(
        self,
        contents: io.BytesIO,
        dao: "CRUDDAOProtocol | None" = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(contents, **kwargs)
        self._dao = dao

    async def _validate(self, configs: dict[str, dict[str, Any]]) -> None:
        for name, config in configs.items():
            if name.startswith("queries/"):
                for required in ("uuid", "sql", "database_uuid"):
                    if not config.get(required):
                        raise CommandInvalidError(f"Missing {required} in {name}")
                # schema max 128 chars matches the DB column; enforcing here
                # avoids a DataError 500.
                schema = config.get("schema")
                if isinstance(schema, str) and len(schema) > 128:
                    raise CommandInvalidError(
                        f"schema must be at most 128 characters in {name}"
                    )

    async def run(self) -> None:
        if self._configs is None:
            raise CommandInvalidError("validate() must be called before run()")
        if self._dao is None:
            raise CommandInvalidError("DAO not provided for import")

        configs = self._configs
        session = self._dao.session

        database_uuids: set[str] = set()
        for file_name, config in configs.items():
            if file_name.startswith("queries/") and isinstance(config, dict):
                if config.get("database_uuid"):
                    database_uuids.add(config["database_uuid"])

        database_ids: dict[str, int] = {}
        for file_name, config in configs.items():
            if (
                file_name.startswith("databases/")
                and isinstance(config, dict)
                and config.get("uuid") in database_uuids
            ):
                db = await _import_database(
                    session,
                    self._apply_password(dict(config), file_name),
                    overwrite=False,
                )
                database_ids[str(db.uuid)] = int(db.id)

        for file_name, config in configs.items():
            if (
                file_name.startswith("queries/")
                and isinstance(config, dict)
                and config.get("database_uuid") in database_ids
            ):
                cfg = dict(config)
                cfg["db_id"] = database_ids[cfg["database_uuid"]]
                await import_saved_query(session, cfg, overwrite=self._overwrite)

    async def _import_single(self, file_name: str, content: dict[str, Any]) -> None:
        pass  # run() handles full orchestration; this method is not used.
