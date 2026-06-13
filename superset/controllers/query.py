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
"""Query controller — endpoints for query status, stop, list, and metadata."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from litestar import Controller, get, post
from litestar.di import Provide

from superset.commands.query.stop import StopQueryCommand
from superset.controllers.base import (
    build_rison_query_params,
    get_distinct_payload,
    get_info_payload,
    get_related_payload,
    serialize_list_response,
)
from superset.events import event_logger
from superset.exceptions import SupersetException
from superset.guards.rbac import require_permission
from superset.params.rison import provide_rison_query
from superset.providers import provide_query_dao
from superset.schemas.query import StopQuerySchema
from superset.typing import QueryDAOProtocol, SecurityManagerProtocol, UserProtocol

logger = logging.getLogger(__name__)

# ``GET /api/v1/query/`` list columns — ported 1:1 from
# ``superset_old/queries/api.py::QueryRestApi.list_columns`` (the full Query
# History row). The frontend Query Search page keys off these exact field
# names, so the set and the dotted nested paths must match verbatim.
QUERY_LIST_COLUMNS = [
    "id",
    "changed_on",
    "client_id",
    "database.id",
    "database.database_name",
    "executed_sql",
    "error_message",
    "limit",
    "limiting_factor",
    "progress",
    "rows",
    "schema",
    "select_as_cta",
    "sql",
    "sql_editor_id",
    "sql_tables",
    "status",
    "tab_name",
    "user.first_name",
    "user.id",
    "user.last_name",
    "start_time",
    "start_running_time",
    "end_time",
    "tmp_table_name",
    "tracking_url",
    "results_key",
]

# Mirrors ``QueryRestApi.order_columns``.
QUERY_ORDER_COLUMNS = [
    "changed_on",
    "database.database_name",
    "rows",
    "schema",
    "start_time",
    "sql",
    "tab_name",
    "user.first_name",
]


def _query_sql_tables(query: Any) -> list[dict[str, Any]]:
    """Best-effort port of ``Query.sql_tables`` for the list response.

    The original model exposes ``sql_tables`` as a property that runs the
    SQL through Jinja + sqlglot and returns the referenced tables, falling
    back to ``[]`` on any parse/security/template error. The new ``Query``
    model does not carry that property, so we recompute it here from the
    eager-loaded ``database`` relationship, serialising each ``Table``
    dataclass to ``{table, schema, catalog}`` (the shape Superset's JSON
    encoder produced for the original dataclass).
    """
    sql = getattr(query, "sql", None)
    database = getattr(query, "database", None)
    if not sql or database is None:
        return []
    try:
        from superset.sql.parse import process_jinja_sql

        tables = process_jinja_sql(sql, database).tables
    except Exception:  # noqa: BLE001 — original swallows parse/security/template errors
        return []
    return [
        {"table": t.table, "schema": t.schema, "catalog": t.catalog} for t in tables
    ]


class QueryController(Controller):
    path = "/api/v1/query"
    tags = ["Queries"]
    dependencies = {
        "dao": Provide(provide_query_dao, sync_to_thread=False),
        "rison_params": Provide(provide_rison_query),
    }

    @get(
        "/{pk:int}",
        guards=[require_permission("can_read", "Query")],
    )
    async def get_single(
        self,
        pk: int,
        dao: QueryDAOProtocol,
        current_user: UserProtocol,
        security_manager: Any,
    ) -> dict[str, Any]:
        """GET /api/v1/query/{pk} — get a single query by ID."""
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        from superset.db.filters import query_access_filters
        from superset.exceptions import ObjectNotFoundError
        from superset.models.sql_lab import Query

        # Apply ownership filter — mirrors ``QueryFilter.apply`` in the original:
        # non-admins without ``can_access_all_queries`` may only see their own
        # queries.  ``base_filters`` is applied to every single-record GET via
        # ``datamodel.get(pk, self._base_filters)`` in the original upstream
        # REST layer.
        base_filters = await query_access_filters(security_manager, current_user)

        # Eager-load ``database`` so the response build below doesn't
        # trigger a sync lazy-load (MissingGreenlet under asyncpg).
        stmt = select(Query).where(Query.id == pk).options(selectinload(Query.database))
        for f in base_filters:
            stmt = stmt.where(f)
        query_result = await dao.session.execute(stmt)
        query = query_result.scalars().one_or_none()
        if query is None:
            raise ObjectNotFoundError("Query", pk)

        # Serialize using show_columns matching the original API
        show_columns = [
            "id",
            "changed_on",
            "client_id",
            "end_result_backend_time",
            "end_time",
            "error_message",
            "executed_sql",
            "limit",
            "progress",
            "results_key",
            "rows",
            "schema",
            "select_as_cta",
            "select_as_cta_used",
            "select_sql",
            "sql",
            "sql_editor_id",
            "start_running_time",
            "start_time",
            "status",
            "tab_name",
            "tmp_schema_name",
            "tmp_table_name",
            "tracking_url",
        ]
        result: dict[str, Any] = {}
        for col in show_columns:
            val = getattr(query, col, None)
            if hasattr(val, "isoformat"):
                val = val.isoformat()  # type: ignore[union-attr]
            result[col] = val

        # Include nested database info — original show_columns has "database.id"
        # (dotted path) which upstream serialises as {"database": {"id": ...}} with no
        # other database fields (superset_old/queries/api.py:104).
        # "database_id" (flat integer) is NOT in show_columns, and "database_name"
        # is only in order_columns/list_columns, not show_columns.
        db_obj = getattr(query, "database", None)
        if db_obj is not None:
            result["database"] = {"id": db_obj.id}

        await event_logger.alog_with_context("query.get", user_id=current_user.id)
        return {"id": pk, "result": result}

    @get(
        "/",
        guards=[require_permission("can_read", "Query")],
    )
    async def get_list(
        self,
        dao: QueryDAOProtocol,
        rison_params: dict[str, Any] | None,
        current_user: UserProtocol,
        security_manager: Any,
    ) -> dict[str, Any]:
        """GET /api/v1/query/ — list queries.

        Returns the full Query History row (``QUERY_LIST_COLUMNS``), matching
        ``superset_old/queries/api.py::QueryRestApi`` 1:1 — including the
        ``changed_on desc`` ``base_order``, rison-based filtering via
        ``search_columns`` (changed_on, database, sql, status, user,
        start_time, sql_editor_id, uuid), rison ordering via
        ``order_columns``, and the dotted ``database`` / ``user`` nested
        objects the frontend Query Search page expects.
        """
        from sqlalchemy.orm import selectinload

        from superset.db.filters import query_access_filters
        from superset.models.sql_lab import Query

        rison_filters, order_by, page, page_size = build_rison_query_params(
            Query,
            rison_params,
            # Upstream ``ModelRestApi.page_size = 20`` — QueryRestApi does not
            # override it, so the original list default is 20, not 25.
            default_page_size=20,
        )
        if not order_by:
            # 1:1 with ``base_order = ("changed_on", "desc")`` in original
            order_by = [Query.changed_on.desc()]

        base_filters = await query_access_filters(security_manager, current_user)
        all_filters = (base_filters or []) + rison_filters

        # Eager-load ``database``/``user`` so the dotted-path serialisation
        # (and ``sql_tables`` recomputation) doesn't trigger a sync lazy-load
        # (MissingGreenlet under asyncpg).
        queries = await dao.find_all(
            filters=all_filters or None,
            page=page,
            page_size=page_size,
            order_by=order_by,
            options=[selectinload(Query.database), selectinload(Query.user)],
        )
        total = await dao.count(filters=all_filters or None)
        await event_logger.alog_with_context("query.list", user_id=current_user.id)

        response = serialize_list_response(
            queries,
            total,
            QUERY_LIST_COLUMNS,
            list_title="List Query",
            order_columns=QUERY_ORDER_COLUMNS,
        )
        # Post-process columns the SA model can't resolve via plain getattr:
        #  - ``tracking_url`` lives on the ``tracking_url_raw`` column attr
        #  - ``sql_tables`` is a property in the original model (recomputed)
        #  - ``limiting_factor`` is an enum → serialise to its value
        from superset.models.sql_lab import LimitingFactor

        for row, query in zip(response["result"], queries, strict=True):
            row["sql_tables"] = _query_sql_tables(query)
            lf = row.get("limiting_factor")
            if isinstance(lf, LimitingFactor):
                row["limiting_factor"] = lf.value
        return response

    @get(
        "/_info",
        guards=[require_permission("can_read", "Query")],
    )
    async def info(
        self,
        dao: QueryDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
        rison_params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        return await get_info_payload(
            dao=dao,
            model_name="Query",
            permissions=["can_read", "can_write"],
            security_manager=security_manager,
            current_user=current_user,
            class_permission_name="Query",
            rison_params=rison_params,
        )

    @get(
        "/related/{column_name:str}",
        guards=[require_permission("can_read", "Query")],
    )
    async def related(
        self,
        column_name: str,
        dao: QueryDAOProtocol,
        security_manager: Any,
        current_user: UserProtocol,
        rison_params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        from superset.db.filters import query_access_filters

        base_filters = await query_access_filters(security_manager, current_user)
        return await get_related_payload(
            dao=dao,
            column_name=column_name,
            rison_params=rison_params,
            allowed_fields=frozenset({"database", "user"}),
            base_filters=base_filters or None,
        )

    @get(
        "/distinct/{column_name:str}",
        guards=[require_permission("can_read", "Query")],
    )
    async def distinct(
        self,
        column_name: str,
        dao: QueryDAOProtocol,
        security_manager: Any,
        current_user: UserProtocol,
        rison_params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        from superset.db.filters import query_access_filters

        base_filters = await query_access_filters(security_manager, current_user)
        return await get_distinct_payload(
            dao=dao,
            column_name=column_name,
            rison_params=rison_params,
            allowed_fields=frozenset({"status"}),
            base_filters=base_filters or None,
        )

    @get(
        "/updated_since",
        guards=[require_permission("can_read", "Query")],
    )
    async def updated_since(
        self,
        dao: QueryDAOProtocol,
        rison_params: dict[str, Any] | None,
        current_user: UserProtocol,
    ) -> dict[str, Any]:
        last_updated_ms = (rison_params or {}).get("last_updated_ms", 0)
        user_id = current_user.id
        queries = await dao.get_queries_changed_after(
            user_id=user_id, last_updated_ms=last_updated_ms
        )
        await event_logger.alog_with_context(
            "query.updated_since", user_id=current_user.id
        )
        return {
            "result": [
                q.to_dict()
                if hasattr(q, "to_dict")
                else {
                    col: getattr(q, col, None)
                    for col in (
                        "id",
                        "client_id",
                        "database_id",
                        "tab_name",
                        "sql_editor_id",
                        "sql",
                        "status",
                        "schema",
                        "user_id",
                        "progress",
                        "rows",
                        "error_message",
                        "results_key",
                        "start_time",
                        "end_time",
                        "changed_on",
                        "tmp_table_name",
                        "tmp_schema_name",
                        "tracking_url",
                        "limit",
                    )
                }
                for q in (queries or [])
            ]
        }

    @post(
        "/stop",
        guards=[require_permission("can_read", "Query")],
    )
    async def stop_query(
        self, data: StopQuerySchema, dao: QueryDAOProtocol
    ) -> dict[str, str]:
        # 1:1 with ``superset_old/queries/api.py:234-241``:
        # ``@backoff.on_exception(backoff.constant, Exception, interval=1,
        # on_backoff=lambda ...: db.session.rollback(),
        # on_giveup=lambda ...: db.session.rollback(), max_tries=5)``
        # Async equivalent: retry up to 5 times with 1-second intervals,
        # rolling back the session on each retry and on final failure.
        max_tries = 5
        last_exc: Exception | None = None
        for attempt in range(max_tries):
            try:
                cmd = StopQueryCommand(dao=dao, client_id=data.client_id)  # type: ignore[arg-type]
                await cmd.execute()
                await event_logger.alog_with_context(
                    "query.stop", extra={"client_id": data.client_id}
                )
                return {"result": "OK"}
            except Exception as ex:  # noqa: BLE001
                if isinstance(ex, SupersetException):
                    # domain error — never retry (mirrors original: the
                    # SupersetException is caught inside the original fn body,
                    # so the @backoff decorator never sees it — zero retries)
                    raise
                last_exc = ex
                await dao.session.rollback()
                if attempt < max_tries - 1:
                    await asyncio.sleep(1)
        # Giveup: rollback already happened on the last iteration.
        # Re-raise the last exception so the generic handler maps it
        # to the appropriate HTTP status (404 / 422 / 500).
        raise last_exc  # type: ignore[misc]
