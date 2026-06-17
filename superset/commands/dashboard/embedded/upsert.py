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
"""Upsert command for embedded dashboards."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from superset.commands.base import AsyncBaseCommand
from superset.exceptions import ObjectNotFoundError

if TYPE_CHECKING:
    from superset.db.daos.dashboard import AsyncDashboardDAO, AsyncEmbeddedDashboardDAO
    from superset.models.embedded_dashboard import EmbeddedDashboard


class UpsertEmbeddedDashboardCommand(AsyncBaseCommand["EmbeddedDashboard"]):
    def __init__(
        self,
        dao: AsyncDashboardDAO,
        embedded_dao: AsyncEmbeddedDashboardDAO,
        dashboard_id: int,
        allowed_domains: list[str],
    ) -> None:
        self._dao = dao
        self._embedded_dao = embedded_dao
        self._dashboard_id = dashboard_id
        self._allowed_domains = allowed_domains
        self._dashboard: Any | None = None

    async def validate(self) -> None:
        self._dashboard = await self._dao.get_by_id_or_slug(self._dashboard_id)
        if not self._dashboard:
            raise ObjectNotFoundError("Dashboard", self._dashboard_id)

    async def run(self) -> "EmbeddedDashboard":
        assert self._dashboard is not None
        embedded = await self._embedded_dao.upsert(
            self._dashboard.id,
            self._allowed_domains,
        )
        await self._embedded_dao.session.flush()
        return embedded
