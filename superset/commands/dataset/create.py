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
"""Async port of ``superset_old/commands/dataset/create.py``."""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from superset.commands.base import AsyncBaseCommand
from superset.exceptions import CommandInvalidError
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
        if not self._data.get("table_name"):
            raise CommandInvalidError("table_name is required")
        if not self._data.get("database"):
            raise CommandInvalidError("database is required")
        self._database = await self._dao.get_database_by_id(self._data["database"])
        if not self._database:
            raise CommandInvalidError("Database not found")
        is_unique = await self._dao.validate_uniqueness(
            database_id=self._data["database"],
            table_name=self._data["table_name"],
            schema=self._data.get("schema"),
        )
        if not is_unique:
            raise CommandInvalidError(
                "Dataset with this table_name/schema/database already exists"
            )
        # Validate table exists in the database (for physical datasets)
        sql = self._data.get("sql")
        if not sql and hasattr(self._database, "has_table"):
            import asyncio

            table_name = self._data["table_name"]
            schema = self._data.get("schema")
            try:
                exists = await asyncio.to_thread(
                    self._database.has_table, table_name, schema=schema
                )
                if not exists:
                    raise CommandInvalidError(
                        f"Table '{table_name}' does not exist in database"
                    )
            except CommandInvalidError:
                raise
            except Exception:  # noqa: S110
                pass  # Skip check if has_table is not available
        # Check schema access if security manager is available
        if self._security_manager is not None:
            schema = self._data.get("schema")
            if schema:
                try:
                    await self._security_manager.raise_for_access(
                        database=self._database,
                        schema=schema,
                        user=self._user_id,
                    )
                except Exception as exc:
                    raise CommandInvalidError(
                        f"Access denied to schema '{schema}'"
                    ) from exc
        # Validate SQL access for virtual datasets
        sql = self._data.get("sql")
        if sql and self._security_manager is not None and self._database is not None:
            if hasattr(self._security_manager, "raise_for_access"):
                try:
                    await self._security_manager.raise_for_access(
                        database=self._database,
                        schema=self._data.get("schema"),
                        sql=sql,
                        user=self._user_id,
                    )
                except Exception as exc:
                    raise CommandInvalidError(
                        "Access denied: insufficient SQL access"
                    ) from exc

    async def run(self) -> "SqlaTable":
        from superset.models.connectors import SqlaTable

        # Resolve catalog: use provided value or fall back to database default
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
        )
        if self._user_id is not None:
            dataset.created_by_fk = self._user_id
            dataset.changed_by_fk = self._user_id
        self._dao.session.add(dataset)
        await self._dao.session.flush()

        import asyncio

        if hasattr(dataset, "fetch_metadata"):
            try:
                await asyncio.to_thread(dataset.fetch_metadata)
            except Exception:
                logger.warning("fetch_metadata failed for new dataset", exc_info=True)

        # Add implicit type: and owner: tags (async port of DatasetUpdater.after_insert)
        await self._dao.session.refresh(dataset, ["owners"])
        owner_ids = [o.id for o in dataset.owners] if hasattr(dataset, "owners") else []
        await add_implicit_tags_after_insert(
            self._dao.session, "dataset", dataset.id, owner_ids
        )

        return dataset


class GetOrCreateDatasetCommand(AsyncBaseCommand["SqlaTable"]):
    def __init__(
        self,
        dao: AsyncDatasetDAO,
        data: dict[str, Any],
        user_id: int | None = None,
    ) -> None:
        self._dao = dao
        self._data = data
        self._user_id = user_id

    async def validate(self) -> None:
        if not self._data.get("table_name"):
            raise CommandInvalidError("table_name is required")
        if not self._data.get("database_id"):
            raise CommandInvalidError("database_id is required")

    async def run(self) -> "SqlaTable":
        from superset.models.connectors import SqlaTable

        existing = await self._dao.find_one_or_none(
            table_name=self._data["table_name"],
            database_id=self._data["database_id"],
        )
        if existing:
            return existing
        dataset = SqlaTable(
            table_name=self._data["table_name"],
            database_id=self._data["database_id"],
            schema=self._data.get("schema"),
            template_params=self._data.get("template_params"),
            normalize_columns=self._data.get("normalize_columns", False),
            always_filter_main_dttm=self._data.get("always_filter_main_dttm", False),
        )
        if self._user_id is not None:
            dataset.created_by_fk = self._user_id
            dataset.changed_by_fk = self._user_id
        self._dao.session.add(dataset)
        await self._dao.session.flush()
        return dataset
