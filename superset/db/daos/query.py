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
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from superset.common.query_status import QueryStatus
from superset.db.base_dao import BaseAsyncDAO
from superset.models.sql_lab import Query, SavedQuery
from superset.utils.dates import now_as_float


class AsyncQueryDAO(BaseAsyncDAO[Query]):
    model_cls = Query

    async def save_metadata(
        self,
        query: Query,
        payload: dict[str, Any],
    ) -> None:
        """Extract column metadata from payload and store in query."""
        columns = payload.get("columns", [])
        processed = []
        for col in columns:
            processed_col = dict(col)
            if "name" in processed_col and "column_name" not in processed_col:
                processed_col["column_name"] = processed_col.pop("name")
            processed.append(processed_col)

        query.set_extra_json_key("columns", processed)  # type: ignore[attr-defined]
        self.session.add(query)

    async def get_queries_changed_after(
        self,
        user_id: int,
        last_updated_ms: float | int,
    ) -> list[Query]:
        """Get user's queries modified after a timestamp (in milliseconds).

        ``Query.changed_on`` is stored as ``TIMESTAMP WITHOUT TIME ZONE`` and
        populated with naive UTC (``datetime.utcnow``).  asyncpg refuses to
        compare it against a tz-aware datetime, so we build a naive UTC
        value here — identical to the original Superset DAO's
        ``datetime.utcfromtimestamp(last_updated_ms / 1000)``.
        """
        last_updated_dt = datetime.fromtimestamp(
            last_updated_ms / 1000, tz=timezone.utc
        ).replace(tzinfo=None)
        stmt = select(Query).where(
            Query.user_id == user_id,
            Query.changed_on >= last_updated_dt,
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def stop_query(self, client_id: str) -> Query | None:
        """Stop a running query by client_id.

        Calls cancel_query driver before setting STOPPED status.
        Returns the query if found and stopped, None if not found.
        """
        query = await self.find_one_or_none(client_id=client_id)
        if not query:
            return None

        # Skip if already in terminal state
        terminal_states = {
            QueryStatus.FAILED,
            QueryStatus.SUCCESS,
            QueryStatus.TIMED_OUT,
            QueryStatus.STOPPED,
        }
        if query.status in terminal_states:
            return query

        # 1:1 with ``superset_old/daos/query.py::stop_query``: attempt to
        # cancel via the engine and *raise* ``SupersetCancelQueryException`` if
        # the cancel fails — only set STOPPED on a successful cancel. The sync
        # ``cancel_query`` (1:1 port in ``tasks/sql_lab.py``) opens a synchronous
        # analytical connection, so it runs in a worker thread.
        from superset.exceptions import SupersetCancelQueryException
        from superset.tasks.sql_lab import cancel_query

        if not await asyncio.to_thread(cancel_query, query):
            raise SupersetCancelQueryException("Could not cancel query")

        query.status = QueryStatus.STOPPED  # type: ignore[assignment]
        query.end_time = now_as_float()  # type: ignore[assignment]
        return query


class AsyncSavedQueryDAO(BaseAsyncDAO[SavedQuery]):
    model_cls = SavedQuery
