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
"""Async port of ``superset_old/commands/dataset/warm_up_cache.py``."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from superset.commands.base import AsyncBaseCommand
from superset.commands.chart.warm_up_cache import WarmUpChartCacheCommand
from superset.commands.dataset.exceptions import WarmUpCacheTableNotFoundError
from superset.connectors.sqla.models import SqlaTable
from superset.db.daos.chart import AsyncChartDAO
from superset.models.core import Database
from superset.models.slice import Slice

if TYPE_CHECKING:
    from superset.db.daos.dataset import AsyncDatasetDAO


class WarmUpDatasetCacheCommand(AsyncBaseCommand[list[dict[str, Any]]]):
    def __init__(
        self,
        dao: AsyncDatasetDAO,
        db_name: str,
        table_name: str,
        dashboard_id: int | None = None,
        extra_filters: str | None = None,
    ) -> None:
        self._dao = dao
        self._db_name = db_name
        self._table_name = table_name
        self._dashboard_id = dashboard_id
        self._extra_filters = extra_filters
        self._charts: list[Slice] = []

    async def validate(self) -> None:
        stmt = (
            select(SqlaTable)
            .join(Database)
            .where(
                Database.database_name == self._db_name,
                SqlaTable.table_name == self._table_name,
            )
        )
        table = (await self._dao.session.execute(stmt)).scalars().one_or_none()
        if table is None:
            raise WarmUpCacheTableNotFoundError()

        charts_stmt = (
            select(Slice)
            .where(
                Slice.datasource_id == table.id,
                Slice.datasource_type == table.type,
            )
            .options(selectinload(Slice.owners))
        )
        self._charts = list(
            (await self._dao.session.execute(charts_stmt)).scalars().all()
        )

    async def run(self) -> list[dict[str, Any]]:
        await self.validate()
        chart_dao = AsyncChartDAO(self._dao.session)
        results: list[dict[str, Any]] = []
        for chart in self._charts:
            cmd = WarmUpChartCacheCommand(
                dao=chart_dao,
                chart_id=chart.id,
                dashboard_id=self._dashboard_id,
                extra_filters=self._extra_filters,
            )
            results.append(await cmd.run())
        return results
