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
"""Query and SavedQuery command classes."""

from __future__ import annotations

import io
from typing import Any, TYPE_CHECKING

import yaml  # type: ignore[import-untyped]

from superset.commands.base import AsyncBaseCommand
from superset.exceptions import CommandInvalidError, ObjectNotFoundError
from superset.importexport.export_base import AsyncExportModelsCommand
from superset.importexport.import_base import AsyncImportModelsCommand
from superset.tags.core import (
    add_implicit_tags_after_insert,
    delete_tagged_objects,
    sync_owner_tags_after_update,
)
from superset.utils import mask_uri_password

if TYPE_CHECKING:
    from superset.db.daos.query import AsyncQueryDAO, AsyncSavedQueryDAO
    from superset.models.sql_lab import SavedQuery
    from superset.typing import CRUDDAOProtocol


class StopQueryCommand(AsyncBaseCommand[None]):
    def __init__(self, dao: AsyncQueryDAO, client_id: str) -> None:
        self._dao = dao
        self._client_id = client_id

    async def validate(self) -> None:
        if not self._client_id:
            raise CommandInvalidError("client_id is required")

    async def run(self) -> None:
        query = await self._dao.stop_query(self._client_id)
        if query is None:
            raise ObjectNotFoundError("Query", self._client_id)


class BulkDeleteSavedQueriesCommand(AsyncBaseCommand[None]):
    def __init__(
        self,
        dao: AsyncSavedQueryDAO,
        ids: list[int],
        security_manager: Any | None = None,
        user_id: int | None = None,
    ) -> None:
        self._dao = dao
        self._ids = ids
        self._security_manager = security_manager
        self._user_id = user_id
        self._queries: list[Any] = []

    async def validate(self) -> None:
        if not self._ids:
            raise CommandInvalidError("No saved query IDs provided")
        self._queries = await self._dao.find_by_ids(self._ids)
        found_ids = {int(q.id) for q in self._queries}
        missing = set(self._ids) - found_ids
        if missing:
            raise ObjectNotFoundError("SavedQuery", str(missing))
        if self._security_manager is not None:
            for query in self._queries:
                await self._security_manager.raise_for_ownership(query, self._user_id)

    async def run(self) -> None:
        for q in self._queries:
            await self._dao.session.delete(q)
        await self._dao.session.flush()


class ExportSavedQueriesCommand(AsyncExportModelsCommand):
    def __init__(
        self, model_ids: list[int], dao: "CRUDDAOProtocol | None" = None
    ) -> None:
        super().__init__(model_ids)
        self._dao = dao

    async def _export_single(self, model_id: int) -> list[tuple[str, str]]:
        if self._dao is None:
            raise CommandInvalidError("DAO not provided")
        query = await self._dao.find_by_id(model_id)
        if not query:
            raise ObjectNotFoundError("SavedQuery", model_id)
        db = getattr(query, "database", None)
        db_slug = getattr(db, "database_name", "unknown") if db else "unknown"
        schema_slug = getattr(query, "schema", "default") or "default"
        label = getattr(query, "label", "unknown")
        data = {
            "label": label,
            "sql": getattr(query, "sql", ""),
            "schema": getattr(query, "schema", None),
            "uuid": str(query.uuid) if getattr(query, "uuid", None) else None,
            "database_uuid": str(db.uuid) if db and getattr(db, "uuid", None) else None,
        }
        files: list[tuple[str, str]] = [
            (
                f"queries/{db_slug}/{schema_slug}/{label}.yaml",
                yaml.safe_dump(data, sort_keys=False),
            ),
        ]
        # Bundle database YAML
        if db:
            db_data = {
                "database_name": db.database_name,
                "sqlalchemy_uri": mask_uri_password(getattr(db, "sqlalchemy_uri", "")),
                "uuid": str(db.uuid) if getattr(db, "uuid", None) else None,
            }
            files.append(
                (
                    f"databases/{db.database_name}.yaml",
                    yaml.safe_dump(db_data, sort_keys=False),
                )
            )
        return files


class ImportSavedQueriesCommand(AsyncImportModelsCommand):
    def __init__(
        self, contents: io.BytesIO, dao: "CRUDDAOProtocol | None" = None, **kwargs: Any
    ) -> None:
        super().__init__(contents, **kwargs)
        self._dao = dao

    async def _validate(self, configs: dict[str, dict[str, Any]]) -> None:
        pass

    async def _import_single(self, file_name: str, content: dict[str, Any]) -> None:
        if not file_name.startswith("queries/"):
            return
        if self._dao is None:
            raise CommandInvalidError("DAO not provided for import")
        await self._dao.create(content)
        await self._dao.session.flush()


class CreateSavedQueryCommand(AsyncBaseCommand["SavedQuery"]):
    def __init__(
        self,
        dao: AsyncSavedQueryDAO,
        data: dict[str, Any],
        user_id: int | None = None,
    ) -> None:
        self._dao = dao
        self._data = data
        self._user_id = user_id

    async def validate(self) -> None:
        if not self._data.get("label"):
            raise CommandInvalidError("label is required")
        if not self._data.get("sql"):
            raise CommandInvalidError("sql is required")

    async def run(self) -> "SavedQuery":
        from superset.models.sql_lab import SavedQuery

        query = SavedQuery(**self._data)
        if self._user_id is not None:
            query.created_by_fk = self._user_id
            query.changed_by_fk = self._user_id
        self._dao.session.add(query)
        await self._dao.session.flush()

        # Add implicit type: and owner: tags (async port of QueryUpdater.after_insert)
        owner_ids = [self._user_id] if self._user_id is not None else []
        await add_implicit_tags_after_insert(
            self._dao.session,
            "query",
            int(query.id),
            owner_ids,
        )

        return query


class UpdateSavedQueryCommand(AsyncBaseCommand["SavedQuery"]):
    def __init__(
        self,
        dao: AsyncSavedQueryDAO,
        query_id: int,
        data: dict[str, Any],
        user_id: int | None = None,
    ) -> None:
        self._dao = dao
        self._query_id = query_id
        self._data = data
        self._user_id = user_id
        self._query: Any | None = None

    async def validate(self) -> None:
        self._query = await self._dao.find_by_id(self._query_id)
        if not self._query:
            raise ObjectNotFoundError("SavedQuery", self._query_id)

    async def run(self) -> "SavedQuery":
        assert self._query is not None
        for key, value in self._data.items():
            if hasattr(self._query, key):
                setattr(self._query, key, value)
        if self._user_id is not None:
            self._query.changed_by_fk = self._user_id
        await self._dao.session.flush()

        # Sync implicit owner: tags (async port of QueryUpdater.after_update)
        query_user_id = getattr(self._query, "user_id", None)
        owner_ids = [query_user_id] if query_user_id is not None else []
        await sync_owner_tags_after_update(
            self._dao.session, "query", self._query.id, owner_ids
        )

        return self._query


class DeleteSavedQueryCommand(AsyncBaseCommand[None]):
    def __init__(self, dao: AsyncSavedQueryDAO, query_id: int) -> None:
        self._dao = dao
        self._query_id = query_id
        self._query: Any | None = None

    async def validate(self) -> None:
        self._query = await self._dao.find_by_id(self._query_id)
        if not self._query:
            raise ObjectNotFoundError("SavedQuery", self._query_id)

    async def run(self) -> None:
        assert self._query is not None
        query_id = self._query.id
        # Remove implicit tags before deleting (async port of QueryUpdater.after_delete)
        await delete_tagged_objects(self._dao.session, "query", query_id)
        await self._dao.session.delete(self._query)
        await self._dao.session.flush()
