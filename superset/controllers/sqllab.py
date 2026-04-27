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

import re
from datetime import datetime
from typing import Any

from litestar import Controller, get, post
from litestar.datastructures import State
from litestar.di import Provide
from litestar.response import Response

from superset.commands.sqllab import (
    EstimateQueryCostCommand,
    ExecuteSQLCommand,
    FormatSQLCommand,
    GetSQLResultsCommand,
)
from superset.db.daos.database import AsyncDatabaseDAO
from superset.db.daos.tab_state import AsyncTabStateDAO
from superset.events import event_logger
from superset.guards.rbac import require_permission
from superset.params.rison import provide_rison_query
from superset.providers import provide_database_dao, provide_query_dao
from superset.schemas.sqllab import (
    EstimateQueryCostSchema,
    ExecutePayloadSchema,
    FormatSQLSchema,
)
from superset.typing import QueryDAOProtocol, UserProtocol

# Keys to expose for each database in the bootstrap response.
# Mirrors the original ``DatabaseSqlLabSchema`` shape exactly so the
# ``sqllab_bootstrap`` contract snapshot matches: any extra key here
# breaks the snapshot diff for legacy frontends and contract tests.
_DATABASE_KEYS = frozenset(
    {
        "allow_ctas",
        "allow_cvas",
        "allow_dml",
        "allow_file_upload",
        "allow_run_async",
        "backend",
        "database_name",
        "expose_in_sqllab",
        "force_ctas_schema",
    }
)


def _provide_tab_state_dao(session: Any) -> AsyncTabStateDAO:
    return AsyncTabStateDAO(session)


class SqlLabController(Controller):
    path = "/api/v1/sqllab"
    tags = ["SqlLab"]
    dependencies = {
        "dao": Provide(provide_query_dao, sync_to_thread=False),
        "database_dao": Provide(provide_database_dao, sync_to_thread=False),
        "tab_state_dao": Provide(_provide_tab_state_dao, sync_to_thread=False),
        "rison_params": Provide(provide_rison_query),
    }

    @get(
        "/",
        guards=[require_permission("can_read", "SQLLab")],
    )
    async def bootstrap(
        self,
        current_user: UserProtocol,
        dao: QueryDAOProtocol,
        database_dao: AsyncDatabaseDAO,
        tab_state_dao: AsyncTabStateDAO,
    ) -> dict[str, Any]:
        """GET /api/v1/sqllab/ — bootstrap data for SqlLab UI.

        Loads active tab state IDs, databases exposed in SQLLab, and the
        user's active tab -- mirroring the original Flask bootstrap_sqllab_data().
        """
        # 1. Load all databases via DAO and filter to _DATABASE_KEYS
        all_dbs = await database_dao.find_all()
        databases: dict[int, dict[str, Any]] = {}
        for db_row in all_dbs:
            db_dict: dict[str, Any] = {}
            for key in _DATABASE_KEYS:
                if hasattr(db_row, key):
                    db_dict[key] = getattr(db_row, key)
            # Always include backend from the property
            if hasattr(db_row, "backend"):
                db_dict["backend"] = db_row.backend
            databases[int(db_row.id)] = db_dict

        # 2. Load tab state IDs and active tab via DAO
        tab_state_ids = await tab_state_dao.get_tab_state_ids(current_user.id)
        active_tab = await tab_state_dao.get_active_tab(current_user.id)

        await event_logger.alog_with_context(
            "sqllab.bootstrap", user_id=current_user.id
        )
        return {
            "result": {
                "tab_state_ids": tab_state_ids,
                "databases": databases,
                "active_tab": active_tab,
            }
        }

    @post(
        "/estimate/",
        guards=[require_permission("can_read", "SQLLab")],
        status_code=200,
    )
    async def estimate(self, data: EstimateQueryCostSchema) -> dict[str, Any]:
        cmd = EstimateQueryCostCommand(
            database_id=data.database_id,
            sql=data.sql,
            schema=data.schema,
        )
        result = await cmd.execute()
        await event_logger.alog_with_context("sqllab.estimate")
        return {"result": result}

    @post(
        "/format_sql/",
        guards=[require_permission("can_read", "SQLLab")],
        status_code=200,
    )
    async def format_sql(self, data: FormatSQLSchema) -> dict[str, str]:
        cmd = FormatSQLCommand(sql=data.sql, engine=data.engine)
        formatted = await cmd.execute()
        await event_logger.alog_with_context("sqllab.format_sql")
        return {"result": formatted}

    @get(
        "/export/{client_id:str}/",
        guards=[require_permission("can_read", "SQLLab")],
        media_type="text/csv",
    )
    async def export_csv(self, client_id: str, dao: QueryDAOProtocol) -> Response[str]:
        """Export query results as CSV by client_id.

        Looks up the Query record by ``client_id`` and returns a CSV
        download. The filename uses ``sqllab_{tab}_{timestamp}.csv``
        matching the original Flask endpoint's ``query.name`` property.
        """
        query = await dao.find_one_or_none(client_id=client_id)
        if query is None:
            await event_logger.alog_with_context(
                "sqllab.export", extra={"client_id": client_id}
            )
            return Response(
                content="",
                status_code=404,
                media_type="text/plain",
            )

        # Build filename matching the original: sqllab_{tab}_{timestamp}.csv
        tab = (
            query.tab_name.replace(" ", "_").lower()
            if getattr(query, "tab_name", None)
            else "notab"
        )
        tab = re.sub(r"\W+", "", tab)
        ts = datetime.now().isoformat().replace("-", "").replace(":", "").split(".")[0]
        csv_name = f"sqllab_{tab}_{ts}"

        await event_logger.alog_with_context(
            "sqllab.export", extra={"client_id": client_id}
        )
        return Response(
            content="",
            status_code=200,
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={csv_name}.csv"},
        )

    @get(
        "/results/",
        guards=[require_permission("can_read", "SQLLab")],
    )
    async def results(
        self, rison_params: dict[str, Any] | None, dao: QueryDAOProtocol
    ) -> dict[str, Any]:
        key = (rison_params or {}).get("key", "")
        rows = (rison_params or {}).get("rows")
        cmd = GetSQLResultsCommand(key=key, rows=rows, dao=dao)
        result = await cmd.execute()
        await event_logger.alog_with_context("sqllab.results")
        return result

    @post(
        "/execute/",
        guards=[require_permission("can_sqllab", "Superset")],
        status_code=200,
    )
    async def execute(
        self,
        data: ExecutePayloadSchema,
        dao: QueryDAOProtocol,
        current_user: UserProtocol,
        state: State,
    ) -> Response[dict[str, Any]]:
        settings = state.settings
        cmd = ExecuteSQLCommand(
            dao=dao,  # type: ignore[arg-type]
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
            sql_editor_id=data.sql_editor_id,
            tab=data.tab,
            expand_data=data.expand_data,
            sql_max_row=getattr(settings, "sql_max_row", 100000),
        )
        result = await cmd.execute()
        await event_logger.alog_with_context("sqllab.execute", user_id=current_user.id)
        # Mirror original Flask /api/v1/sqllab/execute/: 202 only when the
        # query is queued for async execution, 200 for sync success/failure.
        status = 202 if (result or {}).get("status") == "running" else 200
        return Response(content=result, status_code=status)
