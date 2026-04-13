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

from sqlalchemy import select

from superset.db.base_dao import BaseAsyncDAO
from superset.models.core import Log
from superset.models.dashboard import Dashboard
from superset.models.slice import Slice


class AsyncLogDAO(BaseAsyncDAO[Log]):
    model_cls = Log

    async def get_dashboard_titles(self, ids: set[int]) -> dict[int, str]:
        """Return a mapping of dashboard ID to dashboard_title for the given IDs."""
        if not ids:
            return {}
        stmt = select(Dashboard.id, Dashboard.dashboard_title).where(
            Dashboard.id.in_(ids)
        )
        rows = (await self.session.execute(stmt)).all()
        return {int(r[0]): r[1] or "" for r in rows}

    async def get_slice_names(self, ids: set[int]) -> dict[int, str]:
        """Return a mapping of slice ID to slice_name for the given IDs."""
        if not ids:
            return {}
        stmt = select(Slice.id, Slice.slice_name).where(Slice.id.in_(ids))
        rows = (await self.session.execute(stmt)).all()
        return {int(r[0]): r[1] or "" for r in rows}

    async def create_log(
        self,
        attributes: dict[str, Any],
    ) -> Log:
        """Create a Log record from a dict of column values.

        Delegates to :meth:`BaseAsyncDAO.create` which calls
        ``session.add()`` internally.  The caller (or the
        ``provide_async_session`` dependency) is responsible for
        committing.
        """
        return await self.create(attributes)

    async def get_recent_activity(
        self,
        user_id: int,
        actions: list[str],
        page: int = 0,
        page_size: int = 25,
    ) -> list[Log]:
        """Get recent activity logs for a user filtered by actions."""
        stmt = (
            select(Log)
            .where(
                Log.user_id == user_id,
                Log.action.in_(actions),
            )
            .order_by(Log.dttm.desc())
            .offset(page * page_size)
            .limit(page_size)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())
