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
"""Async port of ``superset_old/commands/chart/create.py``."""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from superset.commands.base import AsyncBaseCommand
from superset.commands.utils import populate_owner_list
from superset.exceptions import (
    CommandInvalidError,
    DashboardsForbiddenError,
    DashboardsNotFoundValidationError,
    DatasourceNotFoundValidationError,
)
from superset.tags.core import add_implicit_tags_after_insert
from superset.utils.feature_flags import feature_flag_manager

if TYPE_CHECKING:
    from superset.db.daos.chart import AsyncChartDAO
    from superset.models.slice import Slice

logger = logging.getLogger(__name__)


class CreateChartCommand(AsyncBaseCommand["Slice"]):
    def __init__(
        self,
        dao: AsyncChartDAO,
        data: dict[str, Any],
        user_id: int | None = None,
        security_manager: Any | None = None,
    ) -> None:
        self._dao = dao
        self._data = data
        self._user_id = user_id
        self._security_manager = security_manager

    async def validate(self) -> None:  # noqa: C901
        slice_name = self._data.get("slice_name")
        if not slice_name or not slice_name.strip():
            raise CommandInvalidError("slice_name is required")
        # ``viz_type`` is optional in original Superset (charts/schemas.py:199)
        # — charts can be saved without a chosen visualization.

        # Validate/Populate datasource — 1:1 with
        # ``superset_old/commands/chart/create.py`` which calls
        # ``get_datasource_by_id`` and stores ``datasource_name``.
        datasource_id = self._data.get("datasource_id")
        datasource_type = self._data.get("datasource_type", "table")
        if datasource_id:
            from superset.db.daos.datasource import AsyncDatasourceDAO

            datasource = await AsyncDatasourceDAO(self._dao.session).get_datasource(
                datasource_type, datasource_id
            )
            if datasource is None:
                raise DatasourceNotFoundValidationError()
            self._data["datasource_name"] = datasource.name

        # Validate/Populate dashboards — ported 1:1 from
        # ``superset_old/commands/chart/create.py::CreateChartCommand.validate``.
        # All requested dashboards must exist AND the current user must be
        # an owner of each of them (admins are treated as owners of all
        # resources, mirroring ``SecurityManager.raise_for_ownership``),
        # otherwise creation is rejected.
        dashboard_ids = self._data.get("dashboards", []) or []
        if dashboard_ids:
            dashboards = await self._dao.find_dashboards_by_ids(dashboard_ids)
            if len(dashboards) != len(dashboard_ids):
                raise DashboardsNotFoundValidationError()
            # Visibility scope — upstream resolves the ids via the FILTERED
            # ``DashboardDAO.find_by_ids`` (DashboardAccessFilter), so a
            # dashboard the user can't see reads as "not found" (422), never
            # 403 — which would disclose its existence (R14-06).
            from superset.commands.utils import filter_visible_ids
            from superset.db.filters import dashboard_access_filters
            from superset.models.dashboard import Dashboard

            visible = await filter_visible_ids(
                self._security_manager,
                self._user_id,
                self._dao.session,
                Dashboard,
                [int(d.id) for d in dashboards],
                dashboard_access_filters,
            )
            if {int(d.id) for d in dashboards} - visible:
                raise DashboardsNotFoundValidationError()
            if self._security_manager is not None:
                from superset.exceptions import SupersetSecurityException

                for dash in dashboards:
                    try:
                        await self._security_manager.raise_for_ownership(
                            dash, self._user_id
                        )
                    except SupersetSecurityException as ex:
                        raise DashboardsForbiddenError() from ex
            # Store resolved Dashboard objects so ``run()`` can assign directly
            # without a second DAO round-trip.
            self._data["dashboards"] = dashboards

    async def run(self) -> "Slice":
        from datetime import datetime

        from superset.models.slice import Slice

        # Filter out relationship fields to avoid passing raw IDs to model constructor
        create_data = {
            k: v
            for k, v in self._data.items()
            if k not in ("owners", "tags", "dashboards")
        }
        chart = Slice(**create_data)
        if self._user_id is not None:
            chart.created_by_fk = self._user_id
            chart.changed_by_fk = self._user_id
            chart.last_saved_by_fk = self._user_id  # type: ignore[assignment]
        chart.last_saved_at = datetime.now()  # type: ignore[assignment]

        # Resolve owners — defaults to the current user when none provided
        resolved_owner_ids: list[int] = []
        if self._security_manager is not None:
            owners = await populate_owner_list(
                self._security_manager,
                self._user_id,
                self._data.get("owners"),
                default_to_user=True,
            )
            chart.owners = owners
            resolved_owner_ids = [o.id for o in owners]

        # Assign dashboards M2M BEFORE attaching the chart to the
        # session.  ``validate()`` already resolved the requested ids to
        # ``Dashboard`` instances and enforced ownership.  Assigning
        # after ``session.add`` + ``flush`` would make SQLAlchemy try to
        # lazy-load the "existing" M2M state for diffing, which blows up
        # under asyncpg (``MissingGreenlet``).  On a brand-new transient
        # instance the collection is empty, so no load is needed and the
        # assignment is just an in-memory write.
        dashboards = self._data.get("dashboards") or []
        if dashboards:
            chart.dashboards = list(dashboards)

        self._dao.session.add(chart)
        await self._dao.session.flush()

        # Add implicit type: and owner: tags — 1:1 with
        # ``ChartUpdater.after_insert`` which only fires when the
        # TAGGING_SYSTEM feature flag is enabled (listeners are only
        # registered when the flag is on; see ``superset_old/app.py:158``).
        if feature_flag_manager.is_feature_enabled("TAGGING_SYSTEM"):
            owner_ids = resolved_owner_ids
            await add_implicit_tags_after_insert(
                self._dao.session, "chart", chart.id, owner_ids
            )

        return chart
