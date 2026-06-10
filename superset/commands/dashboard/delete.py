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
"""Async port of ``superset_old/commands/dashboard/delete.py``."""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from superset.commands.base import AsyncBaseCommand
from superset.commands.dashboard.exceptions import (
    DashboardDeleteFailedReportsExistError,
)
from superset.exceptions import CommandInvalidError, ObjectNotFoundError
from superset.tags.core import delete_tagged_objects
from superset.utils.feature_flags import feature_flag_manager

if TYPE_CHECKING:
    from superset.db.daos.dashboard import AsyncDashboardDAO, AsyncEmbeddedDashboardDAO

logger = logging.getLogger(__name__)


class DeleteDashboardCommand(AsyncBaseCommand[None]):
    def __init__(
        self,
        dao: AsyncDashboardDAO,
        dashboard_id: int,
        security_manager: Any | None = None,
        user_id: int | None = None,
    ) -> None:
        self._dao = dao
        self._dashboard_id = dashboard_id
        self._security_manager = security_manager
        self._user_id = user_id
        self._dashboard: Any | None = None

    async def validate(self) -> None:
        self._dashboard = await self._dao.find_by_id(self._dashboard_id)
        if not self._dashboard:
            raise ObjectNotFoundError("Dashboard", self._dashboard_id)
        # Check there are no associated ReportSchedules — 1:1 with the
        # original, which raises BEFORE the ownership check (a dashboard with
        # alerts/reports reports "reports exist", not "forbidden").
        from superset.db.daos.report import AsyncReportScheduleDAO

        reports = await AsyncReportScheduleDAO(self._dao.session).find_by_dashboard_ids(
            [self._dashboard_id]
        )
        if reports:
            report_names = ", ".join(report.name for report in reports)
            raise DashboardDeleteFailedReportsExistError(
                f"There are associated alerts or reports: {report_names}"
            )
        if self._security_manager is not None:
            await self._security_manager.raise_for_ownership(
                self._dashboard, self._user_id
            )

    async def run(self) -> None:
        assert self._dashboard is not None
        dashboard_id = self._dashboard.id
        # Remove implicit tags before deleting — 1:1 with
        # ``DashboardUpdater.after_delete`` which fires only when the
        # TAGGING_SYSTEM feature flag is enabled (listeners are only registered
        # when the flag is on; see ``superset_old/app.py:158``).
        if feature_flag_manager.is_feature_enabled("TAGGING_SYSTEM"):
            await delete_tagged_objects(self._dao.session, "dashboard", dashboard_id)
        await self._dao.delete([self._dashboard])
        await self._dao.session.flush()


class BulkDeleteDashboardsCommand(AsyncBaseCommand[None]):
    def __init__(
        self,
        dao: AsyncDashboardDAO,
        dashboard_ids: list[int],
        security_manager: Any | None = None,
        user_id: int | None = None,
    ) -> None:
        self._dao = dao
        self._dashboard_ids = dashboard_ids
        self._security_manager = security_manager
        self._user_id = user_id
        self._dashboards: list[Any] = []

    async def validate(self) -> None:
        if not self._dashboard_ids:
            raise CommandInvalidError("No dashboard IDs provided")
        self._dashboards = await self._dao.find_by_ids(self._dashboard_ids)
        found_ids = {int(d.id) for d in self._dashboards}
        missing = set(self._dashboard_ids) - found_ids
        if missing:
            raise ObjectNotFoundError("Dashboard", str(sorted(missing)))
        # Check there are no associated ReportSchedules — 1:1 with the
        # original ``DeleteDashboardCommand``, which raises BEFORE the
        # ownership check.
        from superset.db.daos.report import AsyncReportScheduleDAO

        reports = await AsyncReportScheduleDAO(self._dao.session).find_by_dashboard_ids(
            self._dashboard_ids
        )
        if reports:
            report_names = ", ".join(report.name for report in reports)
            raise DashboardDeleteFailedReportsExistError(
                f"There are associated alerts or reports: {report_names}"
            )
        # Ownership check
        if self._security_manager is not None:
            for dashboard in self._dashboards:
                await self._security_manager.raise_for_ownership(
                    dashboard, self._user_id
                )

    async def run(self) -> None:
        # Remove implicit tags before deleting — 1:1 with
        # ``DeleteDashboardCommand.run()`` which ports
        # ``DashboardUpdater.after_delete`` (fires per-row when TAGGING_SYSTEM
        # is enabled; see ``superset_old/app.py:158``).
        if feature_flag_manager.is_feature_enabled("TAGGING_SYSTEM"):
            for dashboard in self._dashboards:
                await delete_tagged_objects(
                    self._dao.session, "dashboard", dashboard.id
                )
        await self._dao.delete(self._dashboards)
        await self._dao.session.flush()


class DeleteEmbeddedDashboardCommand(AsyncBaseCommand[None]):
    def __init__(
        self,
        dao: AsyncDashboardDAO,
        embedded_dao: AsyncEmbeddedDashboardDAO,
        dashboard_id: int,
    ) -> None:
        self._dao = dao
        self._embedded_dao = embedded_dao
        self._dashboard_id = dashboard_id
        self._dashboard: Any | None = None

    async def validate(self) -> None:
        self._dashboard = await self._dao.get_by_id_or_slug(self._dashboard_id)
        if not self._dashboard:
            raise ObjectNotFoundError("Dashboard", self._dashboard_id)

    async def run(self) -> None:
        assert self._dashboard is not None
        embedded = await self._embedded_dao.find_by_dashboard_id(self._dashboard.id)
        if embedded:
            await self._embedded_dao.session.delete(embedded)
            await self._embedded_dao.session.flush()
