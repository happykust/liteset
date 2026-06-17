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
"""Commands for creating and retrieving datasets."""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from sqlalchemy.exc import SQLAlchemyError

from superset.commands.base import AsyncBaseCommand
from superset.tags.core import add_implicit_tags_after_insert

if TYPE_CHECKING:
    from superset.db.daos.dataset import AsyncDatasetDAO
    from superset.models.connectors import SqlaTable

logger = logging.getLogger(__name__)


class CreateDatasetCommand(AsyncBaseCommand["SqlaTable"]):
    def __init__(
        self,
        dao: AsyncDatasetDAO,
        data: dict[str, Any],
        user_id: int | None = None,
        security_manager: Any | None = None,
    ) -> None:
        self._dao = dao
        self._data = data
        self._user_id = user_id
        self._security_manager = security_manager
        self._database: Any | None = None

    async def validate(self) -> None:  # noqa: C901
        from superset.commands.dataset.exceptions import (
            DatabaseNotFoundValidationError,
            DatasetDataAccessIsNotAllowed,
            DatasetExistsValidationError,
            DatasetInvalidError,
            DatasetValidationError,
            TableNotFoundValidationError,
        )
        from superset.sql.parse import Table

        exceptions: list[DatasetValidationError] = []
        table_name = self._data.get("table_name")
        database_id = self._data.get("database")
        catalog = self._data.get("catalog")
        schema = self._data.get("schema")
        sql = self._data.get("sql")

        if not table_name:
            exceptions.append(
                DatasetValidationError(
                    "table_name is required", field_name="table_name"
                )
            )
        if not database_id:
            exceptions.append(
                DatasetValidationError("database is required", field_name="database")
            )

        self._database = (
            await self._dao.get_database_by_id(database_id) if database_id else None
        )
        if database_id and not self._database:
            exceptions.append(DatabaseNotFoundValidationError())

        table: Table | None = None
        if self._database is not None and table_name:
            if not catalog and hasattr(self._database, "get_default_catalog"):
                catalog = self._database.get_default_catalog()
                self._data["catalog"] = catalog
            table = Table(table_name, schema, catalog)
            is_unique = await self._dao.validate_uniqueness(
                database_id=database_id,
                table_name=table_name,
                schema=schema,
                catalog=catalog,
            )
            if not is_unique:
                exceptions.append(DatasetExistsValidationError(table))

            if not sql:
                import asyncio

                from sqlalchemy.exc import SQLAlchemyError

                try:
                    exists = await asyncio.to_thread(self._database.has_table, table)
                except SQLAlchemyError as ex:
                    logger.warning(
                        "Got an error %s validating table: %s", str(ex), table
                    )
                    exists = False
                if not exists:
                    exceptions.append(TableNotFoundValidationError(table))

        if sql and self._security_manager is not None and self._database is not None:
            from superset.exceptions import (
                SupersetParseError,
                SupersetSecurityException,
            )

            user = (
                await self._security_manager.find_user_by_id(self._user_id)
                if self._user_id is not None
                else None
            )
            try:
                await self._security_manager.raise_for_access(
                    user=user,
                    database=self._database,
                    sql=sql,
                    catalog=catalog,
                    schema=schema,
                )
            except SupersetSecurityException as ex:
                message = ex.error.message if getattr(ex, "error", None) else str(ex)
                exceptions.append(DatasetDataAccessIsNotAllowed(message))
            except SupersetParseError as ex:
                message = ex.error.message if getattr(ex, "error", None) else str(ex)
                exceptions.append(
                    DatasetValidationError(f"Invalid SQL: {message}", field_name="sql")
                )

        if self._security_manager is not None:
            from superset.commands.dataset.exceptions import (
                OwnersNotFoundValidationError,
            )
            from superset.commands.utils import populate_owner_list
            from superset.exceptions import (
                OwnersNotFoundValidationError as GenericOwnersNotFoundError,
            )

            try:
                self._owners = await populate_owner_list(
                    self._security_manager,
                    self._user_id,
                    self._data.get("owners"),
                    default_to_user=True,
                )
            except GenericOwnersNotFoundError:
                exceptions.append(OwnersNotFoundValidationError())

        if exceptions:
            raise DatasetInvalidError(exceptions=exceptions)

    async def run(self) -> "SqlaTable":
        from superset.models.connectors import SqlaTable

        catalog = self._data.get("catalog")
        if not catalog and self._database is not None:
            if hasattr(self._database, "get_default_catalog"):
                catalog = self._database.get_default_catalog()

        dataset = SqlaTable(
            table_name=self._data["table_name"],
            database_id=self._data["database"],
            schema=self._data.get("schema"),
            sql=self._data.get("sql"),
            **({"catalog": catalog} if catalog else {}),
            is_managed_externally=self._data.get("is_managed_externally", False),
            external_url=self._data.get("external_url"),
            normalize_columns=self._data.get("normalize_columns", False),
            always_filter_main_dttm=self._data.get("always_filter_main_dttm", False),
            template_params=self._data.get("template_params"),
        )
        if self._user_id is not None:
            dataset.created_by_fk = self._user_id
            dataset.changed_by_fk = self._user_id

        # Assign the full list (not append) before flush to avoid a sync lazy-load
        # on the transient instance (asyncpg MissingGreenlet on first attribute touch).
        # Reuse owners resolved during validate(); fall back if validate() didn't run.
        dataset.owners = []
        if getattr(self, "_owners", None) is not None:
            dataset.owners = self._owners
        elif self._security_manager is not None:
            from superset.commands.utils import populate_owner_list

            dataset.owners = await populate_owner_list(
                self._security_manager,
                self._user_id,
                self._data.get("owners"),
                default_to_user=True,
            )
        try:
            self._dao.session.add(dataset)
            await self._dao.session.flush()

            await self._dao.fetch_metadata(dataset)

            await self._dao.session.refresh(dataset, ["owners"])
            owner_ids = (
                [o.id for o in dataset.owners] if hasattr(dataset, "owners") else []
            )
            await add_implicit_tags_after_insert(
                self._dao.session, "dataset", dataset.id, owner_ids
            )
        except SQLAlchemyError as ex:
            from superset.commands.dataset.exceptions import (
                DatasetCreateFailedError,
            )

            logger.warning("dataset create failed", exc_info=True)
            raise DatasetCreateFailedError(exceptions=[ex]) from ex

        return dataset


class GetOrCreateDatasetCommand(AsyncBaseCommand["SqlaTable"]):
    def __init__(
        self,
        dao: AsyncDatasetDAO,
        data: dict[str, Any],
        user_id: int | None = None,
        security_manager: Any | None = None,
    ) -> None:
        self._dao = dao
        self._data = data
        self._user_id = user_id
        self._security_manager = security_manager

    async def validate(self) -> None:
        from superset.commands.dataset.exceptions import (
            DatasetInvalidError,
            DatasetValidationError,
        )

        exceptions: list[DatasetValidationError] = []
        if not self._data.get("table_name"):
            exceptions.append(
                DatasetValidationError(
                    "table_name is required", field_name="table_name"
                )
            )
        if not self._data.get("database_id"):
            exceptions.append(
                DatasetValidationError(
                    "database_id is required", field_name="database_id"
                )
            )
        if exceptions:
            raise DatasetInvalidError(exceptions=exceptions)

    async def run(self) -> "SqlaTable":
        existing = await self._dao.find_one_or_none(
            table_name=self._data["table_name"],
            database_id=self._data["database_id"],
        )
        if existing:
            return existing

        create_data = {
            "table_name": self._data["table_name"],
            "database": self._data["database_id"],
            "schema": self._data.get("schema"),
            "catalog": self._data.get("catalog"),
            "template_params": self._data.get("template_params"),
            "normalize_columns": self._data.get("normalize_columns", False),
            "always_filter_main_dttm": self._data.get("always_filter_main_dttm", False),
        }
        create_cmd = CreateDatasetCommand(
            dao=self._dao,
            data=create_data,
            user_id=self._user_id,
            security_manager=self._security_manager,
        )
        return await create_cmd.execute()
