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
from litestar.di import Provide
from litestar.response import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from superset.commands.sqllab import (
    EstimateQueryCostCommand,
    ExecuteSQLCommand,
    FormatSQLCommand,
    GetSQLResultsCommand,
)
from superset.events import event_logger
from superset.guards.rbac import require_permission
from superset.params.rison import provide_rison_query
from superset.providers import provide_query_dao
from superset.schemas.sqllab import (
    EstimateQueryCostSchema,
    ExecutePayloadSchema,
    FormatSQLSchema,
)
from superset.typing import QueryDAOProtocol, UserProtocol

# Keys to expose for each database in the bootstrap response.
_DATABASE_KEYS = frozenset(
    {
        "allow_file_upload",
        "allow_ctas",
        "allow_cvas",
        "allow_dml",
        "allow_run_async",
        "allows_subquery",
        "backend",
        "database_name",
        "expose_in_sqllab",
        "force_ctas_schema",
        "id",
        "disable_data_preview",
        "disable_drill_to_detail",
        "allow_multi_catalog",
    }
)


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
    async def bootstrap(
        self,
        current_user: UserProtocol,
        dao: QueryDAOProtocol,
    ) -> dict[str, Any]:
        """GET /api/v1/sqllab/ — bootstrap data for SqlLab UI.

        Loads active tab state IDs, databases exposed in SQLLab, and the
        user's active tab -- mirroring the original Flask bootstrap_sqllab_data().
        """
        from superset.models.core import Database
        from superset.models.sql_lab import TabState

        session: AsyncSession = dao.session

        # 1. Load all databases and filter to _DATABASE_KEYS
        db_result = await session.execute(select(Database))
        databases: dict[int, dict[str, Any]] = {}
        for db_row in db_result.scalars().all():
            db_dict: dict[str, Any] = {}
            for key in _DATABASE_KEYS:
                if hasattr(db_row, key):
                    db_dict[key] = getattr(db_row, key)
            # Always include backend from the property
            if hasattr(db_row, "backend"):
                db_dict["backend"] = db_row.backend
            databases[int(db_row.id)] = db_dict

        # 2. Load tab state IDs for the current user
        tab_stmt = select(TabState.id, TabState.label).where(
            TabState.user_id == current_user.id
        )
        tab_result = await session.execute(tab_stmt)
        tab_state_ids: list[dict[str, Any]] = [
            {"id": row.id, "label": row.label} for row in tab_result.all()
        ]

        # 3. Load the active tab (first active, or first available)
        #    Eager-load relationships to avoid MissingGreenlet on to_dict().
        from sqlalchemy.orm import selectinload

        active_tab_stmt = (
            select(TabState)
            .where(TabState.user_id == current_user.id)
            .order_by(TabState.active.desc())
            .limit(1)
            .options(
                selectinload(TabState.table_schemas),
                selectinload(TabState.latest_query),
                selectinload(TabState.saved_query),
            )
        )
        active_tab_result = await session.execute(active_tab_stmt)
        active_tab_row = active_tab_result.scalars().first()
        active_tab: dict[str, Any] | None = None
        if active_tab_row is not None:
            active_tab = (
                active_tab_row.to_dict()
                if hasattr(active_tab_row, "to_dict")
                else {"id": active_tab_row.id, "label": active_tab_row.label}
            )

        event_logger.log("sqllab.bootstrap", user_id=current_user.id)
        return {
            "result": {
                "tab_state_ids": tab_state_ids,
                "databases": databases,
                "active_tab": active_tab,
                "user": {"id": current_user.id},
            }
        }

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
        """Export query results as CSV by client_id.

        Looks up the Query record by ``client_id`` and returns a CSV
        download. The filename uses ``sqllab_{tab}_{timestamp}.csv``
        matching the original Flask endpoint's ``query.name`` property.
        """
        query = await dao.find_one_or_none(client_id=client_id)
        if query is None:
            event_logger.log("sqllab.export", extra={"client_id": client_id})
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

        event_logger.log("sqllab.export", extra={"client_id": client_id})
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
        )
        result = await cmd.execute()
        event_logger.log("sqllab.execute", user_id=current_user.id)
        return result
