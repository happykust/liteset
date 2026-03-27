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

import asyncio
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from superset.common.query_status import QueryStatus
from superset.db.base_dao import BaseAsyncDAO
from superset.models.sql_lab import Query, SavedQuery


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

        query.set_extra_json_key("columns", processed)
        self.session.add(query)

    async def get_queries_changed_after(
        self,
        user_id: int,
        last_updated_ms: float | int,
    ) -> list[Query]:
        """Get user's queries modified after a timestamp (in milliseconds)."""
        last_updated_dt = datetime.fromtimestamp(
            last_updated_ms / 1000, tz=timezone.utc
        )
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

        # Attempt to cancel the query via the database engine
        try:
            db = query.database
            engine_spec = db.db_engine_spec
            if (
                hasattr(engine_spec, "has_implicit_cancel")
                and engine_spec.has_implicit_cancel()
            ):
                pass  # engine handles cancellation implicitly
            elif hasattr(engine_spec, "cancel_query"):
                cancel_query_id = query.extra.get("cancel_query")
                if cancel_query_id is not None:
                    await asyncio.to_thread(
                        self._cancel_via_engine,
                        db, engine_spec,
                        query, cancel_query_id,
                    )
        except Exception as ex:  # noqa: BLE001
            if isinstance(ex, (KeyboardInterrupt, SystemExit)):
                raise
            import logging

            logging.getLogger(__name__).warning(
                "Failed to cancel query %s: %s", client_id, ex,
            )

        query.status = QueryStatus.STOPPED  # type: ignore[assignment]
        query.end_time = datetime.now(tz=timezone.utc).timestamp()  # type: ignore[assignment]
        return query

    @staticmethod
    def _cancel_via_engine(
        db: Any,
        engine_spec: Any,
        query: Query,
        cancel_query_id: str,
    ) -> bool:
        """Cancel a running query via the database engine (runs in thread)."""
        from contextlib import closing

        with db.get_sqla_engine(
            catalog=query.catalog,
            schema=query.schema,
        ) as engine:
            with closing(engine.raw_connection()) as conn:
                with closing(conn.cursor()) as cursor:
                    return engine_spec.cancel_query(cursor, query, cancel_query_id)  # type: ignore[no-any-return]


class AsyncSavedQueryDAO(BaseAsyncDAO[SavedQuery]):
    model_cls = SavedQuery
