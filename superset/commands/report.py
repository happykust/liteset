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
"""Report Schedule command classes — business logic for report CRUD."""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from superset.commands.base import AsyncBaseCommand
from superset.exceptions import CommandInvalidError, ObjectNotFoundError

try:
    from croniter import croniter
except ImportError:
    croniter = None  # type: ignore[assignment, misc]

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from superset.db.daos.report import AsyncReportScheduleDAO
    from superset.models.reports import ReportSchedule


class CreateReportScheduleCommand(AsyncBaseCommand["ReportSchedule"]):
    def __init__(
        self,
        dao: AsyncReportScheduleDAO,
        data: dict[str, Any],
        user_id: int | None = None,
    ) -> None:
        self._dao = dao
        self._data = data
        self._user_id = user_id

    async def validate(self) -> None:
        name = self._data.get("name")
        if not name or not str(name).strip():
            raise CommandInvalidError("name is required")

        report_type = self._data.get("type")
        if not report_type:
            raise CommandInvalidError("type is required")

        # Validate crontab expression
        crontab = self._data.get("crontab", "")
        if crontab and croniter is not None:
            try:
                croniter(crontab)
            except (ValueError, KeyError) as e:
                raise CommandInvalidError(f"Invalid crontab expression: {e}") from e

        # Validate chart/dashboard relation — mirrors original
        # ``BaseReportScheduleCommand.validate_chart_dashboard`` at
        # superset_old/commands/report/base.py:50-81. A new schedule must
        # reference exactly one of chart/dashboard (not both, not neither).
        chart_id = self._data.get("chart")
        dashboard_id = self._data.get("dashboard")
        if chart_id and dashboard_id:
            raise CommandInvalidError("Choose a chart or dashboard, not both")
        if not chart_id and not dashboard_id:
            raise CommandInvalidError("Must choose either a chart or a dashboard")

        # Validate name + type uniqueness
        is_unique = await self._dao.validate_update_uniqueness(
            name=name, report_type=report_type
        )
        if not is_unique:
            raise CommandInvalidError(
                f"A report schedule with name '{name}' "
                f"and type '{report_type}' already exists"
            )

    async def run(self) -> "ReportSchedule":
        # Map schema fields to model fields
        create_data = {**self._data}
        if "chart" in create_data:
            create_data["chart_id"] = create_data.pop("chart")
        if "dashboard" in create_data:
            create_data["dashboard_id"] = create_data.pop("dashboard")
        if "database" in create_data:
            create_data["database_id"] = create_data.pop("database")

        # Remove relationship fields handled separately
        create_data.pop("owners", None)

        if self._user_id is not None:
            create_data["created_by_fk"] = self._user_id
            create_data["changed_by_fk"] = self._user_id

        report = await self._dao.create(create_data)
        return report


class UpdateReportScheduleCommand(AsyncBaseCommand["ReportSchedule"]):
    def __init__(
        self,
        dao: AsyncReportScheduleDAO,
        pk: int,
        data: dict[str, Any],
        user_id: int | None = None,
    ) -> None:
        self._dao = dao
        self._pk = pk
        self._data = data
        self._user_id = user_id
        self._report: Any | None = None

    async def validate(self) -> None:
        self._report = await self._dao.find_by_id(self._pk)
        if not self._report:
            raise ObjectNotFoundError("ReportSchedule", self._pk)

        # Validate uniqueness if name or type changed
        name = self._data.get("name", self._report.name)
        report_type = self._data.get("type", self._report.type)
        if "name" in self._data or "type" in self._data:
            is_unique = await self._dao.validate_update_uniqueness(
                name=name, report_type=report_type, report_id=self._pk
            )
            if not is_unique:
                raise CommandInvalidError(
                    f"A report schedule with name '{name}' and type "
                    f"'{report_type}' already exists"
                )

        # Validate crontab if provided
        crontab = self._data.get("crontab")
        if crontab and croniter is not None:
            try:
                croniter(crontab)
            except (ValueError, KeyError) as e:
                raise CommandInvalidError(f"Invalid crontab expression: {e}") from e

    async def run(self) -> "ReportSchedule":
        assert self._report is not None

        update_data = {**self._data}
        if "chart" in update_data:
            update_data["chart_id"] = update_data.pop("chart")
        if "dashboard" in update_data:
            update_data["dashboard_id"] = update_data.pop("dashboard")
        if "database" in update_data:
            update_data["database_id"] = update_data.pop("database")

        # Remove relationship fields handled separately
        update_data.pop("owners", None)

        if self._user_id is not None:
            update_data["changed_by_fk"] = self._user_id

        report = await self._dao.update(self._report, update_data)
        await self._dao.session.flush()
        return report


class DeleteReportScheduleCommand(AsyncBaseCommand[None]):
    def __init__(
        self,
        dao: AsyncReportScheduleDAO,
        pk: int,
    ) -> None:
        self._dao = dao
        self._pk = pk
        self._report: Any | None = None

    async def validate(self) -> None:
        self._report = await self._dao.find_by_id(self._pk)
        if not self._report:
            raise ObjectNotFoundError("ReportSchedule", self._pk)

    async def run(self) -> None:
        assert self._report is not None
        await self._dao.delete([self._report])
        await self._dao.session.flush()


class BulkDeleteReportScheduleCommand(AsyncBaseCommand[None]):
    def __init__(
        self,
        dao: AsyncReportScheduleDAO,
        ids: list[int],
    ) -> None:
        self._dao = dao
        self._ids = ids
        self._reports: list[Any] = []

    async def validate(self) -> None:
        if not self._ids:
            raise CommandInvalidError("No report schedule IDs provided")
        self._reports = await self._dao.find_by_ids(self._ids)
        found_ids = {int(r.id) for r in self._reports}
        missing = set(self._ids) - found_ids
        if missing:
            raise ObjectNotFoundError("ReportSchedule", str(missing))

    async def run(self) -> None:
        await self._dao.delete(self._reports)
        await self._dao.session.flush()
