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

import logging
import re
from datetime import datetime
from typing import Any
from urllib import parse as urllib_parse

from litestar import Controller, get, post, Request
from litestar.datastructures import State
from litestar.di import Provide
from litestar.response import Response

from superset.commands.sqllab import (
    EstimateQueryCostCommand,
    ExecuteSQLCommand,
    FormatSQLCommand,
    GetSQLResultsCommand,
    SqlResultExportCommand,
)
from superset.common.query_status import QueryStatus
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

logger = logging.getLogger(__name__)

# Keys to expose for each database in the bootstrap response so the
# frontend's "Show full schema preview", multi-catalog dropdown, and
# similar flags continue to render correctly.
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
        """GET /api/v1/sqllab/ — bootstrap data for SqlLab UI."""
        all_dbs = await database_dao.find_all()
        databases: dict[int, dict[str, Any]] = {}
        for db_row in all_dbs:
            db_dict: dict[str, Any] = {}
            for key in _DATABASE_KEYS:
                if hasattr(db_row, key):
                    try:
                        db_dict[key] = getattr(db_row, key)
                    except Exception:  # noqa: BLE001
                        # Some derived properties (e.g. ``allows_subquery``)
                        # call into the engine spec which may fail without
                        # an established connection. Skip the key.
                        logger.debug("Failed to read db key %s", key, exc_info=True)
                        continue
            if hasattr(db_row, "backend"):
                try:
                    db_dict["backend"] = db_row.backend
                except Exception:  # noqa: BLE001
                    logger.debug("Failed to read db backend", exc_info=True)
            databases[int(db_row.id)] = db_dict

        # Tab states are only loaded when ``SQLLAB_BACKEND_PERSISTENCE`` is enabled.
        from superset.utils.feature_flags import feature_flag_manager

        tab_state_ids: Any = []
        active_tab: Any = None
        if feature_flag_manager.is_feature_enabled("SQLLAB_BACKEND_PERSISTENCE"):
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
        guards=[require_permission("can_estimate_query_cost", "SQL Lab")],
        status_code=200,
    )
    async def estimate(
        self,
        data: EstimateQueryCostSchema,
        database_dao: AsyncDatabaseDAO,
    ) -> dict[str, Any]:
        cmd = EstimateQueryCostCommand(
            database_id=data.database_id,
            sql=data.sql,
            schema=data.schema,
            catalog=data.catalog,
            template_params=data.template_params,
            dao=database_dao,
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
        guards=[require_permission("can_export_csv", "SQLLab")],
        media_type="text/csv",
    )
    async def export_csv(
        self,
        client_id: str,
        dao: QueryDAOProtocol,
        current_user: UserProtocol,
        security_manager: Any,
    ) -> Response[bytes]:
        """Export query results as CSV by ``client_id``.

        Streams the CSV body out of either the cached results-backend
        blob (preferred) or a re-run of the original SQL through
        ``database.get_df`` (fallback). All data goes through
        :func:`df_to_escaped_csv` to defend against CSV injection.
        """
        cmd = SqlResultExportCommand(
            dao=dao,  # type: ignore[arg-type]
            client_id=client_id,
            security_manager=security_manager,
            current_user=current_user,
        )

        from superset.exceptions import SupersetErrorException

        try:
            result = await cmd.execute()
        except SupersetErrorException as ex:
            await event_logger.alog_with_context(
                "sqllab.export", extra={"client_id": client_id}
            )
            return Response(
                content=str(ex.error.message).encode("utf-8"),
                status_code=getattr(ex, "status", 404),
                media_type="text/plain",
            )

        query = result["query"]
        csv_data = result["data"]
        row_count = result["count"]

        # Build filename: ``sqllab_{tab}_{ts}.csv``.
        tab = (
            query.tab_name.replace(" ", "_").lower()
            if getattr(query, "tab_name", None)
            else "notab"
        )
        tab = re.sub(r"\W+", "", tab)
        ts = datetime.now().isoformat().replace("-", "").replace(":", "").split(".")[0]
        csv_name = f"sqllab_{tab}_{ts}"
        quoted_csv_name = urllib_parse.quote(csv_name)

        await event_logger.alog_with_context(
            "sqllab.export",
            extra={
                "client_id": client_id,
                "row_count": row_count,
                "database": getattr(getattr(query, "database", None), "name", None),
                "catalog": query.catalog,
                "schema": query.schema,
                "exported_format": "csv",
            },
        )
        return Response(
            content=csv_data,
            status_code=200,
            media_type="text/csv",
            headers={
                "Content-Disposition": f"attachment; filename={quoted_csv_name}.csv"
            },
        )

    @get(
        "/results/",
        guards=[require_permission("can_get_results", "SQLLab")],
    )
    async def results(
        self,
        rison_params: dict[str, Any] | list[Any] | None,
        dao: QueryDAOProtocol,
    ) -> Response[dict[str, Any]]:
        # ``provide_rison_query`` accepts both dict and list rison roots
        # (e.g. favorite_status uses ``!(1,2,3)``); ``/results/`` is
        # dict-only, so reject the list shape with a clean 422 instead of
        # letting msgspec raise 500 against the signature model.
        params: dict[str, Any] = rison_params if isinstance(rison_params, dict) else {}
        key = params.get("key", "")
        rows = params.get("rows")
        cmd = GetSQLResultsCommand(key=key, rows=rows, dao=dao)

        from superset.exceptions import SupersetErrorException

        try:
            result = await cmd.execute()
        except SupersetErrorException as ex:
            import dataclasses

            await event_logger.alog_with_context("sqllab.results")
            # Return the full SIP-40 error dict; the frontend destructures
            # { error_type, message, extra } from each item — plain strings
            # would yield undefined for all fields.
            error_dict = dataclasses.asdict(ex.error)
            # ``message`` may be a LazyString (i18n) — Litestar's msgspec
            # encoder cannot serialize it; str() it before returning.
            error_dict["message"] = str(error_dict.get("message") or "")
            return Response(
                content={"errors": [error_dict]},
                status_code=getattr(ex, "status", 500),
                media_type="application/json",
            )

        await event_logger.alog_with_context("sqllab.results")
        # Use pessimistic serialization to handle NaN values (converting to null)
        # and edge-case datetime types that the default serializer does not handle.
        from superset.utils import json as superset_json

        payload_str = superset_json.dumps(
            result,
            default=superset_json.pessimistic_json_iso_dttm_ser,
            ignore_nan=True,
        )
        return Response(
            content=payload_str,  # type: ignore[arg-type]
            status_code=200,
            media_type="application/json",
        )

    @post(
        "/execute/",
        guards=[require_permission("can_execute_sql_query", "SQLLab")],
        status_code=200,
    )
    async def execute(
        self,
        data: ExecutePayloadSchema,
        dao: QueryDAOProtocol,
        current_user: UserProtocol,
        security_manager: Any,
        state: State,
        request: Request[Any, Any, Any],
    ) -> Response[dict[str, Any]]:
        settings = state.settings

        # Extract user-agent for audit logs.
        log_params: dict[str, Any] = {
            "user_agent": request.headers.get("user-agent"),
        }

        template_params: dict[str, Any] = {}
        if data.templateParams:
            try:
                from superset.utils import json as superset_json

                parsed = superset_json.loads(data.templateParams)
                if isinstance(parsed, dict):
                    template_params = parsed
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Could not decode templateParams JSON for SQL Lab execute",
                    exc_info=True,
                )

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
            template_params=template_params,
            security_manager=security_manager,
            current_user=current_user,
            log_params=log_params,
        )
        result = await cmd.execute()
        await event_logger.alog_with_context("sqllab.execute", user_id=current_user.id)

        # 202 is ONLY for a freshly-dispatched Celery job (``_run_async`` path,
        # status="running"). Re-submitted queries that already existed must return
        # 200 even when their DB status is "running" or "pending".
        result_dict = result or {}
        # Pop the sentinel set by the idempotency path — must not appear in
        # the response body.
        query_already_created = bool(result_dict.pop("query_already_created", False))
        query_obj = result_dict.get("query", {})
        query_state = query_obj.get("state")
        is_async_dispatch = not query_already_created and query_state in {
            "running",
            "pending",
            QueryStatus.RUNNING,
            QueryStatus.PENDING,
        }
        status = 202 if is_async_dispatch else 200
        # HAS_RESULTS (sync results) → pessimistic ISO serialization for
        # numpy.float64 / pandas.Timestamp / Decimal / NaN (→ null);
        # QUERY_IS_RUNNING / QUERY_ALREADY_CREATED → ``{"query": ...}`` body
        # with ``json_int_dttm_ser`` (epoch-ms datetimes).
        from superset.utils import json as superset_json

        has_results = not is_async_dispatch and not query_already_created
        if has_results:
            payload_str = superset_json.dumps(
                result_dict,
                default=superset_json.pessimistic_json_iso_dttm_ser,
                ignore_nan=True,
            )
        else:
            payload_str = superset_json.dumps(
                {"query": query_obj},
                default=superset_json.json_int_dttm_ser,
                ignore_nan=True,
            )
        return Response(
            content=payload_str,  # type: ignore[arg-type]
            status_code=status,
            media_type="application/json",
        )
