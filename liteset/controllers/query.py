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

from typing import Any

from litestar import Controller, get, post
from litestar.di import Provide

from liteset.commands.query import StopQueryCommand
from liteset.controllers.base import (
    extract_pagination,
    get_distinct_payload,
    get_info_payload,
    get_related_payload,
    serialize_list_response,
)
from liteset.guards.rbac import require_permission
from liteset.params.rison import provide_rison_query
from liteset.providers import provide_query_dao
from liteset.schemas.query import StopQueryBody
from liteset.events import event_logger
from liteset.typing import QueryDAOProtocol, UserProtocol


class QueryController(Controller):
    path = "/api/v1/query"
    tags = ["Queries"]
    dependencies = {
        "dao": Provide(provide_query_dao, sync_to_thread=False),
        "rison_params": Provide(provide_rison_query),
    }

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
        """GET /api/v1/query/ — list queries."""
        from liteset.db.filters import query_access_filters

        page, page_size = extract_pagination(rison_params)
        base_filters = await query_access_filters(security_manager, current_user)
        queries = await dao.find_all(
            filters=base_filters or None, page=page, page_size=page_size
        )
        total = await dao.count(filters=base_filters or None)
        event_logger.log("query.list", user_id=current_user.id)
        return serialize_list_response(queries, total, ["id", "status", "sql"])

    @get(
        "/_info",
        guards=[require_permission("can_read", "Query")],
    )
    async def info(self, dao: QueryDAOProtocol) -> dict[str, Any]:
        return await get_info_payload(
            dao=dao, model_name="Query", permissions=["can_read", "can_write"]
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
        from liteset.db.filters import query_access_filters

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
        from liteset.db.filters import query_access_filters

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
        event_logger.log("query.updated_since", user_id=current_user.id)
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
        guards=[require_permission("can_write", "Query")],
    )
    async def stop_query(
        self, data: StopQueryBody, dao: QueryDAOProtocol
    ) -> dict[str, str]:
        cmd = StopQueryCommand(dao=dao, client_id=data.client_id)
        await cmd.execute()
        event_logger.log("query.stop", extra={"client_id": data.client_id})
        return {"result": "OK"}
