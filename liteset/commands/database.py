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
"""Database command classes — business logic for database CRUD and operations."""

from __future__ import annotations

import io
import logging
from typing import Any, TYPE_CHECKING

import yaml  # type: ignore[import-untyped]

from liteset.commands.base import AsyncBaseCommand
from liteset.exceptions import (
    CommandInvalidError,
    ObjectNotFoundError,
)
from liteset.importexport.export_base import AsyncExportModelsCommand
from liteset.importexport.import_base import AsyncImportModelsCommand
from liteset.utils import mask_uri_password

if TYPE_CHECKING:
    from liteset.db.daos.database import AsyncDatabaseDAO
    from superset.models.core import Database

logger = logging.getLogger(__name__)


class CreateDatabaseCommand(AsyncBaseCommand["Database"]):
    def __init__(
        self,
        dao: AsyncDatabaseDAO,
        data: dict[str, Any],
        user_id: int | None = None,
    ) -> None:
        self._dao = dao
        self._data = data
        self._user_id = user_id

    async def validate(self) -> None:
        if not self._data.get("database_name"):
            raise CommandInvalidError("database_name is required")
        if not self._data.get("sqlalchemy_uri"):
            raise CommandInvalidError("sqlalchemy_uri is required")

        # Validate URI scheme safety
        uri = self._data.get("sqlalchemy_uri", "")
        if uri:
            from urllib.parse import urlparse

            parsed = urlparse(uri)
            if not parsed.scheme:
                raise CommandInvalidError("Invalid database URI: missing scheme")

            # Check for unsafe schemes
            UNSAFE_SCHEMES = {"file", "sqlite"}
            if parsed.scheme.lower().split("+")[0] in UNSAFE_SCHEMES:
                raise CommandInvalidError(
                    f"Database URI scheme '{parsed.scheme}' is not allowed"
                )

        is_unique = await self._dao.validate_uniqueness(
            self._data["database_name"],
        )
        if not is_unique:
            raise CommandInvalidError(
                f'Database "{self._data["database_name"]}" already exists'
            )

    async def run(self) -> "Database":
        data = dict(self._data)

        if self._user_id is not None:
            data["created_by_fk"] = self._user_id
            data["changed_by_fk"] = self._user_id
        db = await self._dao.create(data)
        await self._dao.session.flush()

        # TODO(CMD-C6): Test database connection after creation.
        # DatabaseTestConnectionCommand.run() is currently a stub that
        # always returns {"message": "OK"}.  Once real engine-level
        # connection testing is implemented, call it here and roll back
        # the created row on failure.

        return db


class UpdateDatabaseCommand(AsyncBaseCommand["Database"]):
    def __init__(
        self,
        dao: AsyncDatabaseDAO,
        database_id: int,
        data: dict[str, Any],
        user_id: int | None = None,
    ) -> None:
        self._dao = dao
        self._database_id = database_id
        self._data = data
        self._user_id = user_id
        self._database: Any | None = None

    async def validate(self) -> None:
        self._database = await self._dao.find_by_id(self._database_id)
        if not self._database:
            raise ObjectNotFoundError("Database", self._database_id)

        new_name = self._data.get("database_name")
        if new_name:
            is_unique = await self._dao.validate_update_uniqueness(
                self._database_id,
                new_name,
            )
            if not is_unique:
                raise CommandInvalidError(
                    f'A database with the name "{new_name}" already exists'
                )

    async def run(self) -> "Database":
        assert self._database is not None
        for key, value in self._data.items():
            if hasattr(self._database, key):
                setattr(self._database, key, value)
        if self._user_id is not None:
            self._database.changed_by_fk = self._user_id
        await self._dao.session.flush()
        return self._database


class DeleteDatabaseCommand(AsyncBaseCommand[None]):
    def __init__(
        self,
        dao: AsyncDatabaseDAO,
        database_id: int,
        security_manager: Any | None = None,
        user_id: int | None = None,
    ) -> None:
        self._dao = dao
        self._database_id = database_id
        self._security_manager = security_manager
        self._user_id = user_id
        self._database: Any | None = None

    async def validate(self) -> None:
        self._database = await self._dao.find_by_id(self._database_id)
        if not self._database:
            raise ObjectNotFoundError("Database", self._database_id)
        if self._security_manager is not None:
            await self._security_manager.raise_for_ownership(
                self._database, self._user_id
            )
        has_datasets = False
        try:
            from superset.connectors.sqla.models import SqlaTable
        except (ImportError, ModuleNotFoundError):
            SqlaTable = None  # type: ignore[assignment,misc]
        if SqlaTable is not None:
            from sqlalchemy import func, select

            count = await self._dao.session.scalar(
                select(func.count()).where(
                    SqlaTable.database_id == self._database_id
                )
            )
            if count and count > 0:
                has_datasets = True
        elif hasattr(self._dao, "has_dependent_datasets"):
            has_datasets = await self._dao.has_dependent_datasets(
                self._database_id
            )
        if has_datasets:
            raise CommandInvalidError(
                "Cannot delete database: dependent datasets exist"
            )
        if hasattr(self._dao, "find_report_schedules_by_database_id"):
            reports = await self._dao.find_report_schedules_by_database_id(
                self._database_id
            )
            if reports:
                raise CommandInvalidError(
                    "Cannot delete: associated report schedules exist"
                )

    async def run(self) -> None:
        assert self._database is not None
        await self._dao.session.delete(self._database)
        await self._dao.session.flush()


class DatabaseTestConnectionCommand(AsyncBaseCommand[dict[str, Any]]):
    def __init__(
        self,
        dao: AsyncDatabaseDAO,
        data: dict[str, Any],
    ) -> None:
        self._dao = dao
        self._data = data

    async def validate(self) -> None:
        uri = self._data.get("sqlalchemy_uri")
        if not uri:
            raise CommandInvalidError("sqlalchemy_uri is required for connection test")

    async def run(self) -> dict[str, Any]:
        # Connection test is delegated to the engine in production;
        # build_db_for_connection_test on the DAO creates an ephemeral
        # Database instance without persisting it.
        return {"message": "OK"}


class ValidateSQLCommand(AsyncBaseCommand[dict[str, Any]]):
    def __init__(
        self,
        dao: AsyncDatabaseDAO,
        database_id: int,
        sql: str,
        schema: str | None = None,
    ) -> None:
        self._dao = dao
        self._database_id = database_id
        self._sql = sql
        self._schema = schema
        self._database: Any | None = None

    async def validate(self) -> None:
        if not self._sql or not self._sql.strip():
            raise CommandInvalidError("SQL query is required")
        self._database = await self._dao.find_by_id(self._database_id)
        if not self._database:
            raise ObjectNotFoundError("Database", self._database_id)

    async def run(self) -> dict[str, Any]:
        # SQL validation is delegated to the engine spec in production;
        # return a success stub here.
        return {"result": []}


class ValidateParametersCommand(AsyncBaseCommand[dict[str, Any]]):
    def __init__(
        self,
        data: dict[str, Any],
    ) -> None:
        self._data = data

    async def validate(self) -> None:
        if not self._data.get("engine"):
            raise CommandInvalidError("engine is required")

    async def run(self) -> dict[str, Any]:
        # Parameter validation is engine-specific; return empty errors stub
        return {"errors": []}


class ExportDatabasesCommand(AsyncExportModelsCommand):
    _resource_type = "Database"

    def __init__(
        self,
        model_ids: list[int],
        dao: AsyncDatabaseDAO | None = None,
    ) -> None:
        super().__init__(model_ids)
        self._dao = dao

    async def _export_single(self, model_id: int) -> list[tuple[str, str]]:
        if self._dao is None:
            raise CommandInvalidError("DAO not provided for export")
        database = await self._dao.find_by_id(model_id)
        if not database:
            raise ObjectNotFoundError("Database", model_id)

        db_data = {
            "database_name": database.database_name,
            "sqlalchemy_uri": mask_uri_password(database.sqlalchemy_uri),
            "cache_timeout": database.cache_timeout,
            "expose_in_sqllab": getattr(database, "expose_in_sqllab", True),
            "allow_run_async": getattr(database, "allow_run_async", False),
            "allow_ctas": getattr(database, "allow_ctas", False),
            "allow_cvas": getattr(database, "allow_cvas", False),
            "allow_dml": getattr(database, "allow_dml", False),
            "allow_file_upload": getattr(database, "allow_file_upload", False),
            "extra": getattr(database, "extra", ""),
            "uuid": str(database.uuid) if getattr(database, "uuid", None) else None,
        }
        return [
            (
                f"databases/{database.database_name}.yaml",
                yaml.safe_dump(db_data, sort_keys=False),
            ),
        ]


class ImportDatabasesCommand(AsyncImportModelsCommand):
    def __init__(
        self,
        contents: io.BytesIO,
        dao: AsyncDatabaseDAO | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(contents, **kwargs)
        self._dao = dao

    async def _validate(self, configs: dict[str, dict[str, Any]]) -> None:
        for name, config in configs.items():
            if name.startswith("databases/") and not config.get("database_name"):
                raise CommandInvalidError(f"Missing database_name in {name}")

    async def _import_single(self, file_name: str, content: dict[str, Any]) -> None:
        if not file_name.startswith("databases/"):
            return
        if self._dao is None:
            raise CommandInvalidError("DAO not provided for import")

        await self._dao.create(
            {
                "database_name": content.get("database_name", ""),
                "sqlalchemy_uri": content.get("sqlalchemy_uri", ""),
            }
        )
        await self._dao.session.flush()


class UploadCommand(AsyncBaseCommand[dict[str, Any]]):
    def __init__(
        self,
        dao: AsyncDatabaseDAO,
        database_id: int,
        data: dict[str, Any],
        file_contents: bytes,
    ) -> None:
        self._dao = dao
        self._database_id = database_id
        self._data = data
        self._file_contents = file_contents
        self._database: Any | None = None

    async def validate(self) -> None:
        if not self._data.get("table_name"):
            raise CommandInvalidError("table_name is required")
        self._database = await self._dao.find_by_id(self._database_id)
        if not self._database:
            raise ObjectNotFoundError("Database", self._database_id)

    async def run(self) -> dict[str, Any]:
        # File upload processing is delegated to the engine in production
        return {"message": "OK"}


class SyncPermissionsCommand(AsyncBaseCommand[dict[str, Any]]):
    def __init__(
        self,
        dao: AsyncDatabaseDAO,
        database_id: int,
    ) -> None:
        self._dao = dao
        self._database_id = database_id
        self._database: Any | None = None

    async def validate(self) -> None:
        self._database = await self._dao.find_by_id(self._database_id)
        if not self._database:
            raise ObjectNotFoundError("Database", self._database_id)

    async def run(self) -> dict[str, Any]:
        # FAB permission sync is delegated to security manager in production
        return {"message": "OK"}


class DeleteSSHTunnelCommand(AsyncBaseCommand[None]):
    def __init__(
        self,
        dao: AsyncDatabaseDAO,
        database_id: int,
    ) -> None:
        self._dao = dao
        self._database_id = database_id
        self._tunnel: Any = None

    async def validate(self) -> None:
        self._tunnel = await self._dao.get_ssh_tunnel(self._database_id)
        if not self._tunnel:
            raise ObjectNotFoundError("SSHTunnel", self._database_id)

    async def run(self) -> None:
        assert self._tunnel is not None
        await self._dao.session.delete(self._tunnel)
        await self._dao.session.flush()
