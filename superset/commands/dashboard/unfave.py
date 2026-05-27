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
"""Async port of ``superset_old/commands/dashboard/unfave.py``."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from superset.commands.base import AsyncBaseCommand
from superset.commands.dashboard.exceptions import DashboardAccessDeniedError
from superset.exceptions import ObjectNotFoundError

if TYPE_CHECKING:
    from superset.db.daos.dashboard import AsyncDashboardDAO


class RemoveFavoriteDashboardCommand(AsyncBaseCommand[None]):
    """Remove a dashboard from a user's favorites.

    Ported 1:1 from superset_old/commands/dashboard/unfave.py: the original
    loads via the access-aware ``DashboardDAO.get_by_id_or_slug``. The async
    port reproduces that access check explicitly via ``can_access_dashboard``.
    """

    def __init__(
        self,
        dao: AsyncDashboardDAO,
        dashboard_id: int,
        user_id: int,
        security_manager: Any | None = None,
        user: Any | None = None,
    ) -> None:
        self._dao = dao
        self._dashboard_id = dashboard_id
        self._user_id = user_id
        self._security_manager = security_manager
        self._user = user

    async def validate(self) -> None:
        dashboard = await self._dao.get_full_by_id_or_slug(self._dashboard_id)
        if not dashboard:
            raise ObjectNotFoundError("Dashboard", self._dashboard_id)
        if self._security_manager is not None and self._user is not None:
            if not await self._security_manager.can_access_dashboard(
                dashboard, user=self._user
            ):
                raise DashboardAccessDeniedError()

    async def run(self) -> None:
        await self._dao.remove_favorite(self._dashboard_id, user_id=self._user_id)
