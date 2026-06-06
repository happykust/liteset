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
"""Async port of ``superset_old/commands/chart/delete.py``."""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from superset.commands.base import AsyncBaseCommand
from superset.commands.chart.exceptions import ChartDeleteFailedReportsExistError
from superset.exceptions import CommandInvalidError, ObjectNotFoundError
from superset.tags.core import delete_tagged_objects

if TYPE_CHECKING:
    from superset.db.daos.chart import AsyncChartDAO

logger = logging.getLogger(__name__)


class DeleteChartCommand(AsyncBaseCommand[None]):
    def __init__(
        self,
        dao: AsyncChartDAO,
        chart_id: int,
        security_manager: Any | None = None,
        user_id: int | None = None,
    ) -> None:
        self._dao = dao
        self._chart_id = chart_id
        self._security_manager = security_manager
        self._user_id = user_id
        self._chart: Any | None = None

    async def validate(self) -> None:
        self._chart = await self._dao.find_by_id(self._chart_id)
        if not self._chart:
            raise ObjectNotFoundError("Chart", self._chart_id)
        # Check there are no associated ReportSchedules — 1:1 with the
        # original, which raises BEFORE the ownership check (a chart with
        # alerts/reports reports "reports exist", not "forbidden").
        from superset.db.daos.report import AsyncReportScheduleDAO

        reports = await AsyncReportScheduleDAO(self._dao.session).find_by_chart_ids(
            [self._chart_id]
        )
        if reports:
            report_names = ", ".join(report.name for report in reports)
            raise ChartDeleteFailedReportsExistError(
                f"There are associated alerts or reports: {report_names}"
            )
        if self._security_manager is not None:
            await self._security_manager.raise_for_ownership(self._chart, self._user_id)

    async def run(self) -> None:
        assert self._chart is not None
        chart_id = self._chart.id
        # Remove implicit tags before deleting (async port of ChartUpdater.after_delete)
        await delete_tagged_objects(self._dao.session, "chart", chart_id)
        await self._dao.delete([self._chart])
        await self._dao.session.flush()


class BulkDeleteChartsCommand(AsyncBaseCommand[None]):
    def __init__(
        self,
        dao: AsyncChartDAO,
        chart_ids: list[int],
        security_manager: Any | None = None,
        user_id: int | None = None,
    ) -> None:
        self._dao = dao
        self._chart_ids = chart_ids
        self._security_manager = security_manager
        self._user_id = user_id
        self._charts: list[Any] = []

    async def validate(self) -> None:
        if not self._chart_ids:
            raise CommandInvalidError("No chart IDs provided")
        self._charts = await self._dao.find_by_ids(self._chart_ids)
        found_ids = {int(c.id) for c in self._charts}
        missing = set(self._chart_ids) - found_ids
        if missing:
            # ``str({99998, 99999})`` would render as the Python set repr;
            # frontend message comparisons (and unit tests) expect a list.
            raise ObjectNotFoundError("Chart", str(sorted(missing)))
        # Check there are no associated ReportSchedules (1:1 with the original
        # ``DeleteChartCommand``; the bulk path previously skipped this check).
        from superset.db.daos.report import AsyncReportScheduleDAO

        reports = await AsyncReportScheduleDAO(self._dao.session).find_by_chart_ids(
            self._chart_ids
        )
        if reports:
            report_names = ", ".join(report.name for report in reports)
            raise ChartDeleteFailedReportsExistError(
                f"There are associated alerts or reports: {report_names}"
            )
        if self._security_manager is not None:
            for chart in self._charts:
                await self._security_manager.raise_for_ownership(chart, self._user_id)

    async def run(self) -> None:
        # Mirror ``DeleteChartCommand.run()`` which calls ``delete_tagged_objects``
        # for every chart before deleting it.  The original relied on the ORM
        # ``after_delete`` event (``ChartUpdater.after_delete`` registered via
        # ``sqla.event.listen(Slice, "after_delete", …)`` in
        # ``superset_old/tags/core.py:42``) which fires per-item because
        # ``BaseDAO.delete`` iterates and calls ``db.session.delete(item)``.
        # The async session may not have the same sync event listeners, so both
        # single-delete and bulk-delete must call ``delete_tagged_objects``
        # explicitly — 1:1 parity with the single-delete path above.
        for chart in self._charts:
            await delete_tagged_objects(self._dao.session, "chart", chart.id)
        await self._dao.delete(self._charts)
        await self._dao.session.flush()
