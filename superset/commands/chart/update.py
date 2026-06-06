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
"""Async port of ``superset_old/commands/chart/update.py``."""

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

if TYPE_CHECKING:
    from superset.db.daos.chart import AsyncChartDAO
    from superset.models.slice import Slice

logger = logging.getLogger(__name__)


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

        # If only query_context is being updated, skip ownership validation
        is_query_context_update = set(self._data.keys()) <= {
            "query_context",
            "query_context_generation",
        }
        if not is_query_context_update and self._security_manager is not None:
            await self._security_manager.raise_for_ownership(self._chart, self._user_id)

        # Validate tags — 1:1 with
        # ``superset_old/commands/chart/update.py::UpdateChartCommand.validate``
        # (lines 130-134). Checks the caller has permission to manage tags
        # and that every new tag id exists.  Raises ``TagForbiddenError``
        # (403) / ``TagNotFoundValidationError`` (422).
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

        # Validate/Populate datasource — 1:1 with
        # ``superset_old/commands/chart/update.py``: ``datasource_type`` is
        # required when ``datasource_id`` is updated, the datasource must
        # exist, and its name is stored on the chart (``datasource_name``).
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

        # Validate/Populate dashboards — ported 1:1 from
        # ``superset_old/commands/chart/update.py::UpdateChartCommand.validate``
        # (lines 144-156).
        #
        # Only runs when ``dashboards`` is present in the payload (``None``
        # vs empty list matters — omitted means "don't touch", empty list
        # means "clear").  Every requested id must resolve to a real
        # dashboard; if any don't, we raise ``DashboardsNotFoundValidationError``
        # just like the sync original.  For any *new* association (id not
        # already on the chart) the user must additionally have access to
        # the dashboard — existing associations are preserved to maintain
        # chart ownership rights, matching ``_validate_new_dashboard_access``
        # in the original.
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
                user = (
                    await self._security_manager.find_user_by_id(self._user_id)
                    if self._user_id
                    else None
                )
                if user is not None and hasattr(
                    self._security_manager, "can_access_dashboard"
                ):
                    for dash in new_dashboards:
                        has_access = await self._security_manager.can_access_dashboard(
                            dash, user=user
                        )
                        if not has_access:
                            # Mirror original behaviour: inaccessible new
                            # dashboards are reported as "not found" rather
                            # than "forbidden" to avoid leaking their
                            # existence to users without access.
                            raise DashboardsNotFoundValidationError()

            # Store resolved Dashboard objects so ``run()`` can assign
            # directly without a second DAO round-trip.
            self._data["dashboards"] = dashboards

    async def run(self) -> "Slice":
        from datetime import datetime

        assert self._chart is not None

        # Relationship fields must be resolved separately, not set via setattr
        _RELATIONSHIP_FIELDS = {"owners", "tags", "dashboards"}  # noqa: N806
        for key, value in self._data.items():
            if key in _RELATIONSHIP_FIELDS:
                continue
            if hasattr(self._chart, key):
                setattr(self._chart, key, value)

        # Resolve owners — ``validate()`` already pre-loaded the
        # collection via ``selectinload`` so the assignment below will
        # not trigger a lazy load.
        if self._security_manager is not None:
            self._chart.owners = await compute_owner_list(
                self._security_manager,
                self._user_id,
                list(self._chart.owners),
                self._data.get("owners"),
            )

        # Update tags — 1:1 with
        # ``superset_old/commands/chart/update.py::UpdateChartCommand.run``
        # (lines 66-67): apply the add/remove of custom tags on the chart.
        tag_ids = self._data.get("tags")
        if tag_ids is not None:
            await update_tags(
                ObjectType.chart,
                self._chart.id,
                list(self._chart.tags),
                tag_ids,
                self._dao.session,
            )

        # Assign dashboards — ``validate()`` already resolved the
        # requested ids to ``Dashboard`` instances and validated access
        # for any new associations.
        dashboards = self._data.get("dashboards")
        if dashboards is not None:
            self._chart.dashboards = list(dashboards)

        # ``changed_by_fk`` always tracks the acting user (consistent with the
        # SA ``changed_by`` listener). ``last_saved_*`` mirrors upstream
        # ``UpdateChartCommand.run`` (superset_old/commands/chart/update.py:69-71):
        # only bumped on a real user save, NOT when a background report/cache
        # worker regenerates the stored ``query_context``
        # (``query_context_generation`` truthy).
        query_context_generation = self._data.get("query_context_generation")
        if self._user_id is not None:
            self._chart.changed_by_fk = self._user_id
            if not query_context_generation:
                self._chart.last_saved_by_fk = self._user_id
        if not query_context_generation:
            self._chart.last_saved_at = datetime.now()
        await self._dao.session.flush()

        # Sync implicit owner: tags (async port of ChartUpdater.after_update).
        # Owners are already loaded from ``validate()``.
        owner_ids = (
            [o.id for o in self._chart.owners] if hasattr(self._chart, "owners") else []
        )
        await sync_owner_tags_after_update(
            self._dao.session, "chart", self._chart.id, owner_ids
        )

        return self._chart
