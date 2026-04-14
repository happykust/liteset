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
from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from superset.db.base_dao import BaseAsyncDAO
from superset.db.daos.favorites_mixin import FavoriteMixin
from superset.models.core import FavStarClassName
from superset.models.slice import Slice


class AsyncChartDAO(FavoriteMixin, BaseAsyncDAO[Slice]):
    model_cls = Slice
    _fav_class_name = FavStarClassName.CHART

    async def get_by_id_or_uuid(self, id_or_uuid: int | str) -> Slice | None:
        """Find a chart by integer ID or UUID string."""
        try:
            chart_id = int(id_or_uuid)
            return await self.find_by_id(chart_id)
        except (ValueError, TypeError):
            pass

        # Try UUID lookup
        try:
            uuid_val = UUID(str(id_or_uuid))
        except ValueError:
            return None

        return await self.find_one_or_none(uuid=uuid_val)

    async def find_by_id_with_options(
        self,
        chart_id: int,
        options: list[Any] | None = None,
    ) -> Slice | None:
        """Find a chart by id with optional eager-load ``options``.

        Used when the caller needs to serialize relationship collections
        (owners, dashboards, tags, …) in the same async context, to avoid
        MissingGreenlet errors on lazy load.
        """
        stmt = select(Slice).where(Slice.id == chart_id)
        if options:
            stmt = stmt.options(*options)
        result = await self.session.execute(stmt)
        return result.scalars().one_or_none()

    async def find_dashboards_by_ids(self, dashboard_ids: list[int]) -> list[Any]:
        """Resolve dashboard IDs to Dashboard model instances.

        Used by ``UpdateChartCommand.run`` to reassign the
        ``Slice.dashboards`` M2M collection when the frontend sends
        ``{"dashboards": [id, ...]}`` on PUT ``/api/v1/chart/<id>``.
        Without this method the save-to-dashboard flow silently drops
        the dashboard list and the ``count-crosslinks`` column in the
        chart list stays empty.
        """
        if not dashboard_ids:
            return []
        from superset.models.dashboard import Dashboard

        stmt = (
            select(Dashboard)
            .where(Dashboard.id.in_(dashboard_ids))
            .options(selectinload(Dashboard.owners))
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().unique().all())
