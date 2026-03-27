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
"""SqlLab controller — 6 endpoints for SQL execution, formatting, results, etc."""

from __future__ import annotations

from typing import Any

from litestar import Controller, get, post
from litestar.di import Provide
from litestar.response import Response

from liteset.commands.sqllab import (
    EstimateQueryCostCommand,
    ExecuteSQLCommand,
    FormatSQLCommand,
    GetSQLResultsCommand,
)
from liteset.guards.rbac import require_permission
from liteset.params.rison import provide_rison_query
from liteset.providers import provide_query_dao
from liteset.schemas.sqllab import (
    EstimateQueryCostSchema,
    ExecutePayloadSchema,
    FormatSQLSchema,
    SQLLabBootstrap,
)
from liteset.events import event_logger
from liteset.typing import QueryDAOProtocol, UserProtocol


class SqlLabController(Controller):
    path = "/api/v1/sqllab"
    tags = ["SqlLab"]
    dependencies = {
        "dao": Provide(provide_query_dao, sync_to_thread=False),
        "rison_params": Provide(provide_rison_query),
    }

    @get(
        "/",
        guards=[require_permission("can_read", "SQLLab")],
    )
    async def bootstrap(self, current_user: UserProtocol) -> SQLLabBootstrap:
        """GET /api/v1/sqllab/ — bootstrap data for SqlLab UI."""
        event_logger.log("sqllab.bootstrap", user_id=current_user.id)
        return SQLLabBootstrap(
            user={"id": current_user.id},
        )

    @post(
        "/estimate/",
        guards=[require_permission("can_read", "SQLLab")],
    )
    async def estimate(self, data: EstimateQueryCostSchema) -> dict[str, Any]:
        cmd = EstimateQueryCostCommand(
            database_id=data.database_id,
            sql=data.sql,
            schema=data.schema,
        )
        result = await cmd.execute()
        event_logger.log("sqllab.estimate")
        return {"result": result}

    @post(
        "/format_sql/",
        guards=[require_permission("can_read", "SQLLab")],
    )
    async def format_sql(self, data: FormatSQLSchema) -> dict[str, str]:
        cmd = FormatSQLCommand(sql=data.sql, engine=data.engine)
        formatted = await cmd.execute()
        event_logger.log("sqllab.format_sql")
        return {"result": formatted}

    @get(
        "/export/{client_id:str}/",
        guards=[require_permission("can_read", "SQLLab")],
        media_type="text/csv",
    )
    async def export_csv(self, client_id: str, dao: QueryDAOProtocol) -> Response[str]:
        """Export query results as CSV."""
        # Retrieve results by client_id and convert to CSV
        event_logger.log("sqllab.export", extra={"client_id": client_id})
        return Response(
            content="",
            status_code=200,
            media_type="text/csv",
            headers={
                "Content-Disposition": f"attachment; filename=query_{client_id}.csv"
            },
        )

    @get(
        "/results/",
        guards=[require_permission("can_read", "SQLLab")],
    )
    async def results(self, rison_params: dict[str, Any] | None) -> dict[str, Any]:
        key = (rison_params or {}).get("key", "")
        rows = (rison_params or {}).get("rows")
        cmd = GetSQLResultsCommand(key=key, rows=rows)
        result = await cmd.execute()
        event_logger.log("sqllab.results")
        return result

    @post(
        "/execute/",
        guards=[require_permission("can_sqllab", "Superset")],
    )
    async def execute(
        self,
        data: ExecutePayloadSchema,
        dao: QueryDAOProtocol,
        current_user: UserProtocol,
    ) -> dict[str, Any]:
        cmd = ExecuteSQLCommand(
            dao=dao,
            database_id=data.database_id,
            sql=data.sql,
            schema=data.schema,
            catalog=data.catalog,
            select_as_cta=data.select_as_cta,
            ctas_method=data.ctas_method,
            tmp_table_name=data.tmp_table_name,
            query_limit=data.queryLimit,
            run_async=data.runAsync,
            client_id=data.client_id,
            user_id=current_user.id,
        )
        result = await cmd.execute()
        event_logger.log("sqllab.execute", user_id=current_user.id)
        return result
