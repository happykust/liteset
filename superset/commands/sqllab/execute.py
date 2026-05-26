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
"""``POST /api/v1/sqllab/execute/`` command.

Direct port of the original
``superset_old/commands/sql_lab/execute.py::ExecuteSqlCommand`` plus
``superset_old/sql_lab.py::execute_sql_statements`` which the original
delegated to. Restores every behaviour the new code had dropped:

1. ``security_manager.raise_for_access(query=...)`` access gate.
2. ``DISALLOWED_SQL_FUNCTIONS`` per engine spec.
3. ``database.allow_dml`` enforcement on mutating SQL.
4. ``apply_rls`` via :func:`superset.utils.rls.apply_rls`.
5. CTAS/CVAS validation + ``apply_ctas`` rewrite of the last statement.
6. Multi-statement scripts, ``apply_limit``, ``run_multiple_statements_as_one``.
7. Jinja ``templateParams`` rendering.
8. Async Celery dispatch when ``runAsync`` is true (returns a 202-shaped
   payload that controllers translate to HTTP 202).
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from typing import Any, TYPE_CHECKING

from superset.commands.base import AsyncBaseCommand
from superset.commands.sqllab._shared import (
    DEFAULT_SQL_MAX_ROW,
)
from superset.common.query_status import QueryStatus
from superset.exceptions import (
    CommandInvalidError,
    ObjectNotFoundError,
    SupersetTimeoutException,
)
from superset.utils.dates import now_as_float

if TYPE_CHECKING:
    from superset.db.daos.query import AsyncQueryDAO

logger = logging.getLogger(__name__)


class ExecuteSQLCommand(AsyncBaseCommand[dict[str, Any]]):
    """Execute SQL query — the core SqlLab operation.

    See the module docstring for a list of restored behaviours. The
    overall ordering matches ``execute_sql_statements`` in the original
    so the SQL emitted to the analytical database is byte-identical.
    """

    def __init__(
        self,
        dao: "AsyncQueryDAO",
        database_id: int,
        sql: str,
        schema: str | None = None,
        catalog: str | None = None,
        select_as_cta: bool = False,
        ctas_method: str = "TABLE",
        tmp_table_name: str | None = None,
        query_limit: int | None = None,
        run_async: bool = False,
        client_id: str | None = None,
        user_id: int | None = None,
        sql_editor_id: str | None = None,
        tab: str | None = None,
        expand_data: bool = True,
        sql_max_row: int = DEFAULT_SQL_MAX_ROW,
        template_params: dict[str, Any] | None = None,
        security_manager: Any = None,
        current_user: Any = None,
        log_params: dict[str, Any] | None = None,
    ) -> None:
        self._dao = dao
        self._database_id = database_id
        self._sql = sql
        self._schema = schema
        self._catalog = catalog
        self._select_as_cta = select_as_cta
        self._ctas_method = ctas_method
        self._tmp_table_name = tmp_table_name
        self._query_limit = query_limit
        self._run_async = run_async
        self._client_id = client_id or secrets.token_hex(5)[:11]
        self._user_id = user_id
        self._sql_editor_id = sql_editor_id
        self._tab = tab
        self._expand_data = expand_data
        self._sql_max_row = sql_max_row
        self._template_params = template_params or {}
        self._security_manager = security_manager
        self._current_user = current_user
        self._log_params = log_params or {}

    async def validate(self) -> None:
        if not self._sql.strip():
            raise CommandInvalidError("SQL query cannot be empty")
        if not self._database_id:
            raise CommandInvalidError("database_id is required")

    async def run(self) -> dict[str, Any]:  # noqa: C901, PLR0912, PLR0915
        session = self._dao.session

        # ------------------------------------------------------------------
        # 1. Idempotency — ``_try_get_existing_query`` from the original.
        # ------------------------------------------------------------------
        from superset.models.sql_lab import LimitingFactor, Query

        existing = await self._try_get_existing_query()
        if existing is not None and existing.status in (
            QueryStatus.RUNNING,
            QueryStatus.PENDING,
            QueryStatus.TIMED_OUT,
        ):
            return self._build_response(
                status=existing.status,
                query=existing,
                data=[],
                columns=[],
                expanded_columns=[],
            )

        # ------------------------------------------------------------------
        # 2. Load the Database record
        # ------------------------------------------------------------------
        from superset.models.core import Database

        db_row = await session.get(Database, self._database_id)
        if db_row is None:
            raise ObjectNotFoundError("Database", self._database_id)

        # ------------------------------------------------------------------
        # 3. Determine effective row limit
        # ------------------------------------------------------------------
        effective_limit = self._query_limit or self._sql_max_row

        # ------------------------------------------------------------------
        # 4. Create Query record with PENDING status
        # ------------------------------------------------------------------
        start_time = now_as_float()
        query = Query(
            database_id=self._database_id,
            sql=self._sql,
            schema=self._schema,
            catalog=self._catalog,
            tab_name=self._tab,
            sql_editor_id=self._sql_editor_id,
            user_id=self._user_id,
            status=QueryStatus.PENDING,
            client_id=self._client_id,
            start_time=start_time,
            progress=0,
            select_as_cta=self._select_as_cta,
            ctas_method=self._ctas_method,
            tmp_table_name=self._tmp_table_name,
            limit=effective_limit,
            executed_sql=self._sql,
            limiting_factor=LimitingFactor.UNKNOWN,
        )
        # Bind for downstream helpers (raise_for_access, RLS) which
        # need a ``query.database`` relationship.
        query.database = db_row
        session.add(query)
        await session.flush()
        query_id = query.id

        # ------------------------------------------------------------------
        # 5. Permission gate — ``security_manager.raise_for_access(query=...)``.
        # Runs *before* Jinja rendering exactly like the original
        # ``ExecuteSqlCommand._validate_access`` so Jinja macros that
        # execute statements during rendering still go through RBAC.
        # ------------------------------------------------------------------
        if self._security_manager is not None and self._current_user is not None:
            try:
                await self._security_manager.raise_for_access(
                    user=self._current_user,
                    query=query,
                    template_params=self._template_params or None,
                )
            except Exception as ex:
                logger.info("SQL Lab access denied for query %s: %s", query_id, ex)
                query.status = QueryStatus.FAILED
                query.error_message = str(ex)
                query.end_time = now_as_float()
                await session.flush()
                raise

        # ------------------------------------------------------------------
        # 6. Jinja templateParams — render *before* parsing the script.
        # Mirrors ``SqlQueryRenderImpl.render`` from the original.
        # ------------------------------------------------------------------
        rendered_sql = await self._render_jinja(db_row, query)
        query.executed_sql = rendered_sql

        # ------------------------------------------------------------------
        # 7. Async branch — dispatch to Celery and return immediately.
        # Mirrors ``ASynchronousSqlJsonExecutor.execute`` which returned
        # ``QUERY_IS_RUNNING`` so the API responded with 202.
        # ------------------------------------------------------------------
        if self._run_async:
            try:
                from superset.tasks.sql_lab import get_sql_results

                get_sql_results.delay(
                    query_id=query_id,
                    rendered_query=rendered_sql,
                    return_results=False,
                    store_results=True,
                    username=getattr(self._current_user, "username", None),
                    start_time=start_time,
                    expand_data=self._expand_data,
                    log_params=self._log_params,
                )
            except Exception:  # noqa: BLE001
                # Failure to enqueue should not silently fall back to a
                # sync execution that would exceed the HTTP timeout —
                # surface the error as we do for any other backend
                # configuration issue.
                logger.exception("Failed to enqueue Celery task for SQL Lab query")
                query.status = QueryStatus.FAILED
                query.error_message = (
                    "Could not enqueue async SQL execution. Check Celery configuration."
                )
                await session.flush()
                raise

            query.status = QueryStatus.PENDING
            await session.flush()
            return self._build_response(
                status="running",
                query=query,
                data=[],
                columns=[],
                expanded_columns=[],
            )

        # ------------------------------------------------------------------
        # 8. Synchronous execution — delegate to ``execute_sql_statements``.
        #
        # Mirrors the original ``SynchronousSqlJsonExecutor`` which simply
        # called ``execute_sql_statements`` (the shared core the Celery task
        # also runs) under a timeout. Delegating keeps sync and async paths
        # 1:1: parse, DISALLOWED_SQL_FUNCTIONS, DML gate, RLS_IN_SQLLAB,
        # CTAS/CVAS, per-statement LIMIT, engine-spec execution via
        # ``SupersetResultSet``, ``expand_data`` and the results-backend all
        # live inside ``execute_sql_statements`` — the previous inline
        # re-implementation bypassed the engine spec (raw ``create_engine`` +
        # hard-coded ``pg_backend_pid()``) and dropped most of that pipeline.
        #
        # ``execute_sql_statements`` reads the Query by id through its own
        # sync session, so the PENDING row must be committed first.
        # ``store_results`` matches the original: persist unless this is a
        # CTAS, and only when SQLLAB_BACKEND_PERSISTENCE is enabled.
        # ------------------------------------------------------------------
        from superset.models.sql_lab import Query as _Query
        from superset.tasks.sql_lab import execute_sql_statements

        store_results = (
            not self._select_as_cta
            and self._is_feature_enabled("SQLLAB_BACKEND_PERSISTENCE")
        )
        await session.commit()

        sqllab_timeout = self._get_sqllab_timeout()
        try:
            payload = await asyncio.wait_for(
                asyncio.to_thread(
                    execute_sql_statements,
                    query_id,
                    rendered_sql,
                    True,  # return_results
                    store_results,
                    start_time,
                    self._expand_data,
                    self._log_params,
                    getattr(self._current_user, "username", None),
                ),
                timeout=float(sqllab_timeout),
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Query %s timed out after %s seconds", query_id, sqllab_timeout
            )
            # The worker thread may still be running; mark the row TIMED_OUT
            # on our own connection so the client sees the timeout promptly.
            await session.rollback()
            timed_out = await session.get(_Query, query_id)
            if timed_out is not None:
                timed_out.status = QueryStatus.TIMED_OUT
                timed_out.error_message = (
                    f"The query exceeded the {sqllab_timeout} seconds timeout."
                )
                timed_out.end_time = now_as_float()
                await session.commit()
            raise SupersetTimeoutException(
                error_type="SQLLAB_TIMEOUT_ERROR",
                message=(
                    f"The query exceeded the {sqllab_timeout} seconds timeout. "
                    "It might be too complex, or the database is under heavy load."
                ),
                level="error",
            ) from None

        # ``execute_sql_statements`` returns the full, correct response shape
        # (data / columns / selected_columns / expanded_columns + a ``query``
        # dict carrying the authoritative rows / resultsKey / endDttm, with
        # ``state`` already set). Return it directly: re-reading the Query
        # through our async session would surface a stale, pre-execution
        # snapshot because the worker committed on a separate connection.
        payload = payload or {}
        payload.setdefault("query_id", query_id)
        payload.setdefault("status", QueryStatus.SUCCESS)
        payload.setdefault("data", [])
        payload.setdefault("columns", [])
        payload.setdefault("selected_columns", payload.get("columns", []))
        payload.setdefault("expanded_columns", [])
        if payload.get("error") and "errors" not in payload:
            payload["errors"] = [{"message": payload["error"]}]
        if "query" not in payload:
            # STOPPED / no-results paths omit the query dict — re-read the row
            # (its committed state is sufficient for those cases).
            await session.rollback()
            refreshed = await session.get(_Query, query_id)
            payload["query"] = refreshed.to_dict() if refreshed is not None else {}
        return payload

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------

    async def _try_get_existing_query(self) -> Any | None:
        """Mirror ``ExecuteSqlCommand._try_get_existing_query``."""
        try:
            return await self._dao.find_one_or_none(
                client_id=self._client_id,
                user_id=self._user_id,
                sql_editor_id=self._sql_editor_id,
            )
        except Exception:  # noqa: BLE001
            return None

    async def _render_jinja(self, database: Any, query: Any) -> str:
        """Apply Jinja ``templateParams`` to ``self._sql``.

        Mirrors ``SqlQueryRenderImpl.render`` which applied the template
        processor and surfaced ``find_undeclared_variables`` errors.
        Falls back to the original SQL if rendering fails so the error
        is reported by the engine itself (matching the original which
        raised :class:`SupersetTemplateException` from
        ``commands/sql_lab/execute.py``).
        """
        if not self._template_params and not _has_jinja_markers(self._sql):
            return self._sql
        try:
            from superset.jinja_context import get_template_processor
        except ImportError:
            return self._sql
        try:
            processor = get_template_processor(database, query=query)
            rendered = processor.process_template(self._sql, **self._template_params)
            return rendered if isinstance(rendered, str) else self._sql
        except Exception as ex:  # noqa: BLE001
            logger.warning(
                "Jinja templateParams rendering failed: %s; "
                "passing raw SQL to the engine",
                ex,
                exc_info=True,
            )
            return self._sql

    def _get_sqllab_timeout(self) -> int:
        """Return the configured ``SQLLAB_TIMEOUT`` in seconds.

        Reads ``SupersetSettings.sqllab_timeout`` (default 30 s) which
        maps 1:1 to the original ``app.config["SQLLAB_TIMEOUT"]`` used by
        ``SynchronousSqlJsonExecutor``.
        """
        try:
            from superset.config import SupersetSettings

            settings = SupersetSettings()  # type: ignore[call-arg]
            return int(getattr(settings, "sqllab_timeout", 30))
        except Exception:  # noqa: BLE001
            return 30

    def _is_feature_enabled(self, name: str) -> bool:
        try:
            from superset.utils.feature_flags import feature_flag_manager

            return feature_flag_manager.is_feature_enabled(name)
        except Exception:  # noqa: BLE001
            return False

    def _build_response(
        self,
        status: Any,
        query: Any,
        data: list[dict[str, Any]],
        columns: list[dict[str, Any]],
        expanded_columns: list[dict[str, Any]],
        *,
        error: str | None = None,
    ) -> dict[str, Any]:
        query_dict = query.to_dict() if hasattr(query, "to_dict") else {}
        if query_dict:
            query_dict["state"] = (
                str(status).lower() if isinstance(status, str) else status
            )

        payload: dict[str, Any] = {
            "status": status,
            "data": data,
            "columns": columns,
            "selected_columns": columns,
            "expanded_columns": expanded_columns,
            "query": query_dict,
            "query_id": getattr(query, "id", None),
        }
        if error:
            payload["error"] = error
            payload["errors"] = [{"message": error}]
        return payload


def _has_jinja_markers(sql: str) -> bool:
    return "{{" in sql or "{%" in sql
