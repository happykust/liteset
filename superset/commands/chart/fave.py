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
"""Async port of ``superset_old/commands/chart/fave.py``."""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from superset.commands.base import AsyncBaseCommand
from superset.exceptions import (
    ForbiddenError,
    ObjectNotFoundError,
    SupersetSecurityException,
)

if TYPE_CHECKING:
    from superset.db.daos.chart import AsyncChartDAO

logger = logging.getLogger(__name__)


class AddFavoriteChartCommand(AsyncBaseCommand[None]):
    """Add a chart to a user's favorites.

    1:1 port of ``superset_old/commands/chart/fave.py``.
    The original calls ``security_manager.raise_for_ownership(chart)`` to
    ensure the requesting user is an owner of the chart before favoriting
    (raises 403 if not).  This check is preserved here.
    """

    def __init__(
        self,
        dao: AsyncChartDAO,
        chart_id: int,
        user_id: int,
        security_manager: Any | None = None,
        user: Any | None = None,
    ) -> None:
        self._dao = dao
        self._chart_id = chart_id
        self._user_id = user_id
        self._security_manager = security_manager
        self._user = user

    async def validate(self) -> None:
        chart = await self._dao.find_by_id(self._chart_id)
        if not chart:
            raise ObjectNotFoundError("Chart", self._chart_id)

        # 1:1 with original: ``security_manager.raise_for_ownership(chart)``
        # takes the user *id* positionally and raises ``SupersetSecurityException``
        # when the caller is not an owner — caught and re-raised as a 403.
        # (The previous ``raise_for_ownership(chart, user=...)`` passed the wrong
        # kwarg → ``TypeError``, which the over-broad ``except Exception`` masked
        # as a 403 for *everyone*, including the chart's own owner.)
        if self._security_manager is not None:
            try:
                await self._security_manager.raise_for_ownership(
                    chart, self._user_id
                )
            except SupersetSecurityException as ex:
                raise ForbiddenError(
                    f"User is not an owner of chart {self._chart_id}"
                ) from ex

        self._chart = chart

    async def run(self) -> None:
        await self._dao.add_favorite(self._chart_id, user_id=self._user_id)
