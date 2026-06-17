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
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from superset.common.query_status import QueryStatus
from superset.db.base_dao import BaseAsyncDAO
from superset.models.sql_lab import Query, SavedQuery
from superset.utils.dates import now_as_float

logger = logging.getLogger(__name__)


class AsyncQueryDAO(BaseAsyncDAO[Query]):
    model_cls = Query

    async def save_metadata(
        self,
        query: Query,
        payload: dict[str, Any],
    ) -> None:
        """Extract column metadata from payload and store in query.

        - default for absent ``columns`` key is ``{}`` (not ``[]``)
        - unconditionally overwrites ``column_name`` with ``name`` when present
        - keeps the ``name`` key in the dict (no pop)
        - mutates the column dicts in-place (same object used for set_extra_json_key)
        """
        columns = payload.get("columns", {})
        for col in columns:
            if "name" in col:
                col["column_name"] = col.get("name")
        self.session.add(query)
        query.set_extra_json_key("columns", columns)  # type: ignore[attr-defined]

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
        # Eager-load ``Query.database``: ``cancel_query`` reads
        # ``query.database.db_engine_spec`` from a ``to_thread`` worker
        # (no greenlet/event loop there), so a lazy load would raise
        # ``MissingGreenlet``.
        from sqlalchemy.orm import selectinload

        stmt = (
            select(Query)
            .filter_by(client_id=client_id)
            .options(selectinload(Query.database))
        )
        result = await self.session.execute(stmt)
        query = result.scalars().one_or_none()
        if not query:
            return None

        # Skip if already in terminal state — STOPPED is NOT included so that
        # a repeated stop retries cancellation against the driver
        # (potentially raising SupersetCancelQueryException).
        terminal_states = {
            QueryStatus.FAILED,
            QueryStatus.SUCCESS,
            QueryStatus.TIMED_OUT,
        }
        if query.status in terminal_states:
            logger.warning(
                "Query with client_id could not be stopped: query already complete",
            )
            return query

        from superset.exceptions import SupersetCancelQueryException
        from superset.tasks.sql_lab import cancel_query

        if not await asyncio.to_thread(cancel_query, query):
            raise SupersetCancelQueryException("Could not cancel query")

        query.status = QueryStatus.STOPPED  # type: ignore[assignment]
        query.end_time = now_as_float()  # type: ignore[assignment]
        return query


class AsyncSavedQueryDAO(BaseAsyncDAO[SavedQuery]):
    model_cls = SavedQuery
