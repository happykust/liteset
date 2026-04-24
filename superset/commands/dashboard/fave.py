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
"""Async port of ``superset_old/commands/dashboard/fave.py``."""

from __future__ import annotations

from typing import TYPE_CHECKING

from superset.commands.base import AsyncBaseCommand
from superset.exceptions import ObjectNotFoundError

if TYPE_CHECKING:
    from superset.db.daos.dashboard import AsyncDashboardDAO


class AddFavoriteDashboardCommand(AsyncBaseCommand[None]):
    """Add a dashboard to a user's favorites.

    Ported 1:1 from superset_old/commands/dashboard/fave.py.
    The original validates dashboard existence, then delegates
    to DashboardDAO.add_favorite.
    """

    def __init__(
        self,
        dao: AsyncDashboardDAO,
        dashboard_id: int,
        user_id: int,
    ) -> None:
        self._dao = dao
        self._dashboard_id = dashboard_id
        self._user_id = user_id

    async def validate(self) -> None:
        dashboard = await self._dao.get_by_id_or_slug(self._dashboard_id)
        if not dashboard:
            raise ObjectNotFoundError("Dashboard", self._dashboard_id)

    async def run(self) -> None:
        await self._dao.add_favorite(self._dashboard_id, user_id=self._user_id)
