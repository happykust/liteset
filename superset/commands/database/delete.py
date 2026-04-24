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
"""Async port of ``superset_old/commands/database/delete.py``."""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from superset.commands.base import AsyncBaseCommand
from superset.exceptions import CommandInvalidError, ObjectNotFoundError

if TYPE_CHECKING:
    from superset.db.daos.database import AsyncDatabaseDAO

logger = logging.getLogger(__name__)


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
            from superset.models.connectors import SqlaTable
        except (ImportError, ModuleNotFoundError):
            SqlaTable = None  # type: ignore[assignment,misc]  # noqa: N806
        if SqlaTable is not None:
            from sqlalchemy import func, select

            count = await self._dao.session.scalar(
                select(func.count()).where(SqlaTable.database_id == self._database_id)
            )
            if count and count > 0:
                has_datasets = True
        elif hasattr(self._dao, "has_dependent_datasets"):
            has_datasets = await self._dao.has_dependent_datasets(self._database_id)
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
