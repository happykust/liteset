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
"""Async port of ``superset_old/commands/chart/unfave.py``."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from superset.commands.base import AsyncBaseCommand
from superset.exceptions import ObjectNotFoundError

if TYPE_CHECKING:
    from superset.db.daos.chart import AsyncChartDAO

logger = logging.getLogger(__name__)


class RemoveFavoriteChartCommand(AsyncBaseCommand[None]):
    """Remove a chart from a user's favorites.

    Ported 1:1 from superset_old/commands/chart/unfave.py.
    The original validates chart existence and ownership, then delegates
    to ChartDAO.remove_favorite.
    """

    def __init__(
        self,
        dao: AsyncChartDAO,
        chart_id: int,
        user_id: int,
    ) -> None:
        self._dao = dao
        self._chart_id = chart_id
        self._user_id = user_id

    async def validate(self) -> None:
        chart = await self._dao.find_by_id(self._chart_id)
        if not chart:
            raise ObjectNotFoundError("Chart", self._chart_id)

    async def run(self) -> None:
        await self._dao.remove_favorite(self._chart_id, user_id=self._user_id)
