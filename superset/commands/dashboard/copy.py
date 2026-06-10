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
"""Async port of ``superset_old/commands/dashboard/copy.py``."""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from superset.commands.base import AsyncBaseCommand
from superset.exceptions import CommandInvalidError, ObjectNotFoundError

if TYPE_CHECKING:
    from superset.db.daos.dashboard import AsyncDashboardDAO
    from superset.models.dashboard import Dashboard

logger = logging.getLogger(__name__)


class CopyDashboardCommand(AsyncBaseCommand["Dashboard"]):
    def __init__(
        self,
        dao: AsyncDashboardDAO,
        dashboard_id: int,
        data: dict[str, Any],
        current_user: Any | None = None,
        security_manager: Any | None = None,
    ) -> None:
        self._dao = dao
        self._dashboard_id = dashboard_id
        self._data = data
        self._current_user = current_user
        self._security_manager = security_manager
        self._dashboard: Any | None = None

    async def validate(self) -> None:
        # Eager-load ``owners`` (via the full loader) so the DASHBOARD_RBAC
        # ``is_owner`` check below reads the M2M without a sync lazy-load
        # (MissingGreenlet) on the async session.
        self._dashboard = await self._dao.get_full_by_id_or_slug(self._dashboard_id)
        if not self._dashboard:
            raise ObjectNotFoundError("Dashboard", self._dashboard_id)
        if not self._data.get("dashboard_title"):
            raise CommandInvalidError("dashboard_title is required for copy")
        if not self._data.get("json_metadata"):
            raise CommandInvalidError("json_metadata is required for copy")
        # Validate ``json_metadata`` is parseable JSON now so the user gets a
        # proper 422 — otherwise it bubbles all the way to
        # ``AsyncDashboardDAO.copy_dashboard``'s ``loads(...)`` as an
        # uncaught ``JSONDecodeError`` → 500.
        try:
            from superset.utils.json import loads as _json_loads

            _json_loads(self._data["json_metadata"])
        except Exception as ex:  # noqa: BLE001
            raise CommandInvalidError(f"json_metadata is not valid JSON: {ex}") from ex

        # 1:1 with original superset_old/commands/dashboard/copy.py:
        # When DASHBOARD_RBAC is enabled, only owners of the original dashboard
        # are allowed to create a copy.  Raises CommandInvalidError (→ 422) to
        # match the original DashboardForbiddenError surface.
        try:
            from superset.utils.feature_flags import feature_flag_manager

            if feature_flag_manager.is_feature_enabled("DASHBOARD_RBAC"):
                if (
                    self._security_manager is not None
                    and self._current_user is not None
                ):
                    is_owner = self._security_manager.is_owner(
                        self._dashboard, self._current_user
                    )
                    if not is_owner:
                        from superset.exceptions import ForbiddenError

                        raise ForbiddenError("You are not an owner of this dashboard")
        except (ImportError, ModuleNotFoundError):
            pass

    async def run(self) -> "Dashboard":
        assert self._dashboard is not None
        new_dash = await self._dao.copy_dashboard(
            self._dashboard,
            self._data,
            current_user=self._current_user,
        )
        await self._dao.session.flush()
        return new_dash
