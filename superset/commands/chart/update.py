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
"""Command for updating charts."""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from superset.commands.base import AsyncBaseCommand
from superset.commands.utils import compute_owner_list, update_tags, validate_tags
from superset.exceptions import (
    DashboardsNotFoundValidationError,
    DatasourceNotFoundValidationError,
    DatasourceTypeUpdateRequiredValidationError,
    ObjectNotFoundError,
)
from superset.tags.core import sync_owner_tags_after_update
from superset.tags.models import ObjectType
from superset.utils.feature_flags import feature_flag_manager

if TYPE_CHECKING:
    from superset.db.daos.chart import AsyncChartDAO
    from superset.models.slice import Slice

logger = logging.getLogger(__name__)


def is_query_context_update(properties: dict[str, Any]) -> bool:
    """Return True when the payload contains only a query_context generation update."""
    return set(properties) == {"query_context", "query_context_generation"} and bool(
        properties.get("query_context_generation")
    )


class UpdateChartCommand(AsyncBaseCommand["Slice"]):
    def __init__(
        self,
        dao: AsyncChartDAO,
        chart_id: int,
        data: dict[str, Any],
        user_id: int | None = None,
        security_manager: Any | None = None,
    ) -> None:
        self._dao = dao
        self._chart_id = chart_id
        self._data = data
        self._user_id = user_id
        self._security_manager = security_manager
        self._chart: Any | None = None

    async def validate(self) -> None:  # noqa: C901
        # Eager-load the M2M relationships that ``run()`` re-assigns so
        # that assignments don't trigger lazy reloads under asyncpg
        # (which crash with ``MissingGreenlet``).
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        from superset.models.slice import Slice

        stmt = (
            select(Slice)
            .where(Slice.id == self._chart_id)
            .options(
                selectinload(Slice.owners),
                selectinload(Slice.tags),
                selectinload(Slice.dashboards),
            )
        )
        result = await self._dao.session.execute(stmt)
        self._chart = result.scalars().unique().one_or_none()
        if not self._chart:
            raise ObjectNotFoundError("Chart", self._chart_id)

        # Skip ownership for query-context-only updates so report workers can
        # regenerate stored query_context without being chart owners.
        if (
            not is_query_context_update(self._data)
            and self._security_manager is not None
        ):
            await self._security_manager.raise_for_ownership(self._chart, self._user_id)

        if self._security_manager is not None:
            user = (
                await self._security_manager.find_user_by_id(self._user_id)
                if self._user_id is not None
                else None
            )
            await validate_tags(
                ObjectType.chart,
                list(self._chart.tags),
                self._data.get("tags"),
                self._security_manager,
                user,
            )

        datasource_id = self._data.get("datasource_id")
        if datasource_id is not None:
            datasource_type = self._data.get("datasource_type", "")
            if not datasource_type:
                raise DatasourceTypeUpdateRequiredValidationError()
            from superset.db.daos.datasource import AsyncDatasourceDAO

            datasource = await AsyncDatasourceDAO(self._dao.session).get_datasource(
                datasource_type, datasource_id
            )
            if datasource is None:
                raise DatasourceNotFoundValidationError()
            self._data["datasource_name"] = datasource.name

        dashboard_ids = self._data.get("dashboards")
        if dashboard_ids is not None:
            dashboards = await self._dao.find_dashboards_by_ids(dashboard_ids)
            if len(dashboards) != len(dashboard_ids):
                raise DashboardsNotFoundValidationError()

            existing_dashboard_ids = (
                {d.id for d in self._chart.dashboards}
                if getattr(self._chart, "dashboards", None)
                else set()
            )
            new_dashboards = [
                d for d in dashboards if d.id not in existing_dashboard_ids
            ]
            if new_dashboards and self._security_manager is not None:
                # Resolve NEW dashboards via the access-filtered list
                # (DashboardAccessFilter — published required for non-owners);
                # an id outside that scope is reported as "not found" rather
                # than "forbidden" to avoid leaking dashboard existence.
                from superset.commands.utils import filter_visible_ids
                from superset.db.filters import dashboard_access_filters
                from superset.models.dashboard import Dashboard

                visible = await filter_visible_ids(
                    self._security_manager,
                    self._user_id,
                    self._dao.session,
                    Dashboard,
                    [int(d.id) for d in new_dashboards],
                    dashboard_access_filters,
                )
                if {int(d.id) for d in new_dashboards} - visible:
                    raise DashboardsNotFoundValidationError()

            self._data["dashboards"] = dashboards

    async def run(self) -> "Slice":  # noqa: C901
        from datetime import datetime

        assert self._chart is not None

        _RELATIONSHIP_FIELDS = {"owners", "tags", "dashboards"}  # noqa: N806
        for key, value in self._data.items():
            if key in _RELATIONSHIP_FIELDS:
                continue
            if hasattr(self._chart, key):
                setattr(self._chart, key, value)

        # Skip owner recomputation for query-context-only updates: the non-admin
        # report worker would otherwise be prepended to ``owners``.
        if (
            not is_query_context_update(self._data)
            and self._security_manager is not None
        ):
            self._chart.owners = await compute_owner_list(
                self._security_manager,
                self._user_id,
                list(self._chart.owners),
                self._data.get("owners"),
            )

        tag_ids = self._data.get("tags")
        if tag_ids is not None:
            await update_tags(
                ObjectType.chart,
                self._chart.id,
                list(self._chart.tags),
                tag_ids,
                self._dao.session,
            )

        dashboards = self._data.get("dashboards")
        if dashboards is not None:
            self._chart.dashboards = list(dashboards)

        # ``last_saved_*`` is NOT bumped when a background worker regenerates
        # query_context (query_context_generation is set); only on real user edits.
        query_context_generation = self._data.get("query_context_generation")
        if self._user_id is not None:
            self._chart.changed_by_fk = self._user_id
            if query_context_generation is None:
                self._chart.last_saved_by_fk = self._user_id
        if query_context_generation is None:
            self._chart.last_saved_at = datetime.now()
        await self._dao.session.flush()

        if feature_flag_manager.is_feature_enabled("TAGGING_SYSTEM"):
            owner_ids = (
                [o.id for o in self._chart.owners]
                if hasattr(self._chart, "owners")
                else []
            )
            await sync_owner_tags_after_update(
                self._dao.session, "chart", self._chart.id, owner_ids
            )

        return self._chart
