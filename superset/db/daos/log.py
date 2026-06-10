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
        distinct: bool = True,
        page: int = 0,
        page_size: int = 20,
    ) -> list[Log]:
        """Get recent activity logs for a user filtered by actions.

        When ``distinct`` is ``True`` (default), deduplicates by
        (dashboard_id, slice_id, action) keeping only the most-recent
        entry per combination — mirroring the subquery in
        ``superset_old/daos/log.py:LogDAO.get_recent_activity``.
        When ``False``, returns all matching rows ordered by dttm desc.
        """
        from datetime import datetime, timedelta

        from sqlalchemy import and_, func, or_

        if distinct:
            one_year_ago = datetime.today() - timedelta(days=365)
            subqry = (
                select(
                    Log.dashboard_id,
                    Log.slice_id,
                    Log.action,
                    func.max(Log.dttm).label("dttm"),
                )
                .where(
                    and_(
                        Log.action == "log",
                        Log.user_id == user_id,
                        or_(
                            *{
                                Log.json.contains(f'"event_name": "{action}"')
                                for action in actions
                            },
                        ),
                        Log.dttm > one_year_ago,
                        or_(Log.dashboard_id.isnot(None), Log.slice_id.isnot(None)),
                    )
                )
                .group_by(Log.dashboard_id, Log.slice_id, Log.action)
                .subquery()
            )
            stmt = (
                select(
                    subqry.c.dttm,
                    subqry.c.action,
                    subqry.c.dashboard_id,
                    subqry.c.slice_id,
                    Dashboard.slug.label("dashboard_slug"),
                    Dashboard.dashboard_title,
                    Slice.slice_name,
                )
                .outerjoin(Dashboard, Dashboard.id == subqry.c.dashboard_id)
                .outerjoin(Slice, Slice.id == subqry.c.slice_id)
                .where(
                    or_(
                        and_(
                            Dashboard.dashboard_title.isnot(None),
                            Dashboard.dashboard_title != "",
                        ),
                        and_(
                            Slice.slice_name.isnot(None),
                            Slice.slice_name != "",
                        ),
                    )
                )
                .order_by(subqry.c.dttm.desc())
                .limit(page_size)
                .offset(page * page_size)
            )
        else:
            stmt = (
                select(
                    Log.dttm,
                    Log.action,
                    Log.dashboard_id,
                    Log.slice_id,
                    Dashboard.slug.label("dashboard_slug"),
                    Dashboard.dashboard_title,
                    Slice.slice_name,
                )
                .outerjoin(Dashboard, Dashboard.id == Log.dashboard_id)
                .outerjoin(Slice, Slice.id == Log.slice_id)
                .where(
                    or_(
                        and_(
                            Dashboard.dashboard_title.isnot(None),
                            Dashboard.dashboard_title != "",
                        ),
                        and_(
                            Slice.slice_name.isnot(None),
                            Slice.slice_name != "",
                        ),
                    ),
                    Log.action == "log",
                    Log.user_id == user_id,
                    or_(
                        *{
                            Log.json.contains(f'"event_name": "{action}"')
                            for action in actions
                        },
                    ),
                )
                .order_by(Log.dttm.desc())
                .limit(page_size)
                .offset(page * page_size)
            )

        result = await self.session.execute(stmt)
        return list(result.all())
