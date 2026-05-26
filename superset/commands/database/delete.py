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
from superset.commands.database.exceptions import (
    DatabaseDeleteDatasetsExistFailedError,
    DatabaseDeleteFailedReportsExistError,
)
from superset.exceptions import ObjectNotFoundError

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
        # Check there are no associated ReportSchedules — 1:1 with the
        # original ``DeleteDatabaseCommand``, which raises this BEFORE the
        # dataset check.
        from superset.db.daos.report import AsyncReportScheduleDAO

        reports = await AsyncReportScheduleDAO(
            self._dao.session
        ).find_by_database_ids([self._database_id])
        if reports:
            report_names = ", ".join(report.name for report in reports)
            raise DatabaseDeleteFailedReportsExistError(
                f"There are associated alerts or reports: {report_names}"
            )
        # Check if there are datasets for this database — 1:1 with the
        # original ``if self._model.tables:`` truthiness check.
        if await self._dao.has_dependent_datasets(self._database_id):
            raise DatabaseDeleteDatasetsExistFailedError()

    async def run(self) -> None:
        assert self._database is not None
        await self._dao.session.delete(self._database)
        await self._dao.session.flush()
