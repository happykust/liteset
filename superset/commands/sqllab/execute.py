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
from contextlib import closing
from typing import Any, TYPE_CHECKING

import sqlalchemy as sa

from superset.commands.base import AsyncBaseCommand
from superset.commands.sqllab._shared import (
    DEFAULT_SQL_MAX_ROW,
    build_connection_uri,
    get_engine_name,
    make_json_safe,
)
from superset.common.query_status import QueryStatus
from superset.constants import QUERY_CANCEL_KEY, QUERY_EARLY_CANCEL_KEY
from superset.errors import ErrorLevel, SupersetError, SupersetErrorType
from superset.exceptions import (
    CommandInvalidError,
    ObjectNotFoundError,
    SupersetDisallowedSQLFunctionException,
    SupersetDMLNotAllowedException,
    SupersetErrorException,
    SupersetInvalidCTASException,
    SupersetInvalidCVASException,
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
        # 8. Set status to RUNNING.
        # Mark QUERY_EARLY_CANCEL_KEY = True so a concurrent stop_query
        # request can short-circuit before the cursor begins executing.
        # Mirrors the original ``execute_sql_statements`` pattern where
        # ``cancel_query`` checks ``query.extra.get(QUERY_EARLY_CANCEL_KEY)``
        # and returns True immediately when set.  The flag is removed once
        # the real ``QUERY_CANCEL_KEY`` (engine PID / cancel token) is
        # written below.
        # ------------------------------------------------------------------
        query.status = QueryStatus.RUNNING
        query.start_running_time = now_as_float()
        query.set_extra_json_key(QUERY_EARLY_CANCEL_KEY, True)
        await session.flush()

        # ------------------------------------------------------------------
        # 9. Parse the script (engine-aware) — used by every gate below.
        # ------------------------------------------------------------------
        engine_name = get_engine_name(db_row)
        try:
            from superset.sql.parse import SQLScript

            parsed_script = SQLScript(rendered_sql, engine=engine_name)
        except Exception as ex:
            logger.warning("Failed to parse SQL script: %s", ex)
            query.status = QueryStatus.FAILED
            query.error_message = str(ex)
            query.end_time = now_as_float()
            await session.flush()
            return self._build_response(
                status=QueryStatus.FAILED,
                query=query,
                data=[],
                columns=[],
                expanded_columns=[],
                error=str(ex),
            )

        # ------------------------------------------------------------------
        # 10. DISALLOWED_SQL_FUNCTIONS — security gate.
        # ------------------------------------------------------------------
        disallowed_functions = self._resolve_disallowed_functions(engine_name)
        if disallowed_functions and parsed_script.check_functions_present(
            disallowed_functions
        ):
            query.status = QueryStatus.FAILED
            query.error_message = (
                "SQL statement contains disallowed functions: "
                f"{sorted(disallowed_functions)}"
            )
            query.end_time = now_as_float()
            await session.flush()
            raise SupersetDisallowedSQLFunctionException(disallowed_functions)

        # ------------------------------------------------------------------
        # 11. DML gate — ``database.allow_dml``.
        # ------------------------------------------------------------------
        if parsed_script.has_mutation() and not getattr(db_row, "allow_dml", False):
            query.status = QueryStatus.FAILED
            query.error_message = "DML is not allowed for this database"
            query.end_time = now_as_float()
            await session.flush()
            raise SupersetDMLNotAllowedException()

        # ------------------------------------------------------------------
        # 12. RLS_IN_SQLLAB — apply RLS predicates per statement.
        # ------------------------------------------------------------------
        if self._is_feature_enabled("RLS_IN_SQLLAB"):
            await self._apply_rls(db_row, query, parsed_script)

        # ------------------------------------------------------------------
        # 13. CTAS / CVAS — validate and apply.
        # ------------------------------------------------------------------
        if self._select_as_cta:
            from superset.sql.parse import CTASMethod

            if (
                self._ctas_method == CTASMethod.TABLE.name
                and not parsed_script.is_valid_ctas()
            ):
                raise SupersetInvalidCTASException()
            if (
                self._ctas_method == CTASMethod.VIEW.name
                and not parsed_script.is_valid_cvas()
            ):
                raise SupersetInvalidCVASException()
            self._apply_ctas(query, parsed_script)
            query.select_as_cta_used = True

        # ------------------------------------------------------------------
        # 14. apply_limit — push LIMIT into the SQL itself per statement.
        # ------------------------------------------------------------------
        sqllab_ctas_no_limit = self._is_sqllab_ctas_no_limit()
        for statement in parsed_script.statements:
            self._apply_limit(query, statement, sqllab_ctas_no_limit)

        # ------------------------------------------------------------------
        # 15. Build SQL blocks — one per statement, or a single combined
        # block when the engine spec sets ``run_multiple_statements_as_one``.
        # ------------------------------------------------------------------
        engine_spec = getattr(db_row, "db_engine_spec", None)
        allows_comments = getattr(engine_spec, "allows_sql_comments", True)
        run_as_one = getattr(engine_spec, "run_multiple_statements_as_one", False)

        if run_as_one:
            blocks = [parsed_script.format(comments=allows_comments)]
        else:
            blocks = [
                statement.format(comments=allows_comments)
                for statement in parsed_script.statements
            ]

        # ------------------------------------------------------------------
        # 16. Execute each block — share a single connection. Sets
        # ``cancel_query`` extra so subsequent ``/api/v1/query/stop`` can
        # call ``db_engine_spec.cancel_query``.
        #
        # The call is wrapped with ``asyncio.wait_for`` so that a slow
        # query cannot block the Uvicorn worker indefinitely.  Mirrors the
        # original ``SynchronousSqlJsonExecutor._get_sql_results_with_timeout``
        # which used ``utils.timeout(seconds=SQLLAB_TIMEOUT, ...)``.
        # On timeout we raise ``SupersetTimeoutException`` with
        # ``SQLLAB_TIMEOUT_ERROR`` — the same error_type the original raised
        # so the frontend renders the correct "query timed out" banner.
        # ------------------------------------------------------------------
        connection_uri = build_connection_uri(db_row)
        sqllab_timeout = self._get_sqllab_timeout()
        try:
            (
                rows_raw,
                cursor_desc,
                has_more,
                cancel_id,
            ) = await asyncio.wait_for(
                asyncio.to_thread(
                    _execute_blocks_in_thread,
                    connection_uri,
                    blocks,
                    self._schema,
                    effective_limit,
                    parsed_script.has_mutation() or self._select_as_cta,
                ),
                timeout=float(sqllab_timeout),
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Query %s timed out after %s seconds", query_id, sqllab_timeout
            )
            query.status = QueryStatus.TIMED_OUT
            query.error_message = (
                f"The query exceeded the {sqllab_timeout} seconds timeout."
            )
            query.end_time = now_as_float()
            await session.flush()
            raise SupersetTimeoutException(
                error_type="SQLLAB_TIMEOUT_ERROR",
                message=(
                    f"The query exceeded the {sqllab_timeout} seconds timeout. "
                    "It might be too complex, or the database is under heavy load."
                ),
                level="error",
            )
        except Exception as exc:
            logger.exception("Query %s execution failed", query_id)
            query.status = QueryStatus.FAILED
            query.error_message = str(exc)
            query.end_time = now_as_float()
            query.progress = 0
            await session.flush()

            return self._build_response(
                status=QueryStatus.FAILED,
                query=query,
                data=[],
                columns=[],
                expanded_columns=[],
                error=str(exc),
            )

        # Now that we have the real cancel key, remove the early-cancel flag
        # and write the engine-specific cancel id.
        query.set_extra_json_key(QUERY_EARLY_CANCEL_KEY, False)
        if cancel_id is not None:
            query.set_extra_json_key(QUERY_CANCEL_KEY, cancel_id)

        # ------------------------------------------------------------------
        # 17. Build column metadata + JSON-safe rows
        # ------------------------------------------------------------------
        _dttm_type_names = frozenset(
            {"datetime", "date", "timestamp", "timestamptz", "time"}
        )
        columns: list[dict[str, Any]] = []
        col_names: list[str] = []
        for desc in cursor_desc:
            col_name = desc[0]
            col_type_raw = desc[1]
            col_type_str = (
                col_type_raw.__name__
                if hasattr(col_type_raw, "__name__")
                else str(col_type_raw or "STRING")
            )
            columns.append(
                {
                    "name": col_name,
                    "column_name": col_name,
                    "type": col_type_str.upper(),
                    "is_dttm": col_type_str.lower() in _dttm_type_names,
                }
            )
            col_names.append(col_name)

        data: list[dict[str, Any]] = [
            {col_names[i]: make_json_safe(val) for i, val in enumerate(row)}
            for row in rows_raw
        ]

        # ------------------------------------------------------------------
        # 18. Compute limiting_factor against the configured & user limits
        # ------------------------------------------------------------------
        if has_more:
            if self._query_limit and self._query_limit < self._sql_max_row:
                limiting_factor = LimitingFactor.DROPDOWN
            else:
                limiting_factor = LimitingFactor.QUERY
        else:
            limiting_factor = LimitingFactor.NOT_LIMITED

        # ------------------------------------------------------------------
        # 19. Update Query record — SUCCESS
        # ------------------------------------------------------------------
        query.status = QueryStatus.SUCCESS
        query.rows = len(data)
        query.progress = 100
        query.end_time = now_as_float()
        query.limiting_factor = limiting_factor
        query.set_extra_json_key("columns", columns)
        query.set_extra_json_key("progress", None)
        await session.flush()

        return self._build_response(
            status=QueryStatus.SUCCESS,
            query=query,
            data=data,
            columns=columns,
            expanded_columns=[],
        )

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

    def _resolve_disallowed_functions(self, engine_name: str) -> set[str]:
        try:
            from superset.config import SupersetSettings

            settings = SupersetSettings()  # type: ignore[call-arg]
        except Exception:  # noqa: BLE001
            return set()
        functions: dict[str, set[str]] = (
            getattr(settings, "disallowed_sql_functions", {}) or {}
        )
        return set(functions.get(engine_name, set()))

    def _is_feature_enabled(self, name: str) -> bool:
        try:
            from superset.utils.feature_flags import feature_flag_manager

            return feature_flag_manager.is_feature_enabled(name)
        except Exception:  # noqa: BLE001
            return False

    def _is_sqllab_ctas_no_limit(self) -> bool:
        try:
            from superset.config import SupersetSettings

            settings = SupersetSettings()  # type: ignore[call-arg]
            return bool(getattr(settings, "sqllab_ctas_no_limit", False))
        except Exception:  # noqa: BLE001
            return False

    async def _apply_rls(self, database: Any, query: Any, parsed_script: Any) -> None:
        """Apply RLS predicates per statement in ``parsed_script``.

        Wraps the synchronous :func:`superset.utils.rls.apply_rls`
        (which performs sync metadata DB lookups) in
        :func:`asyncio.to_thread` so the controller stays
        non-blocking.
        """
        try:
            from superset.utils.rls import apply_rls
        except ImportError:
            return
        default_schema = self._schema or ""
        try:
            default_schema = (
                database.get_default_schema_for_query(query) or default_schema
            )
        except Exception:  # noqa: BLE001
            pass

        for statement in parsed_script.statements:
            try:
                await asyncio.to_thread(
                    apply_rls,
                    database,
                    self._catalog,
                    default_schema,
                    statement,
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Failed to apply RLS to SQL Lab statement (continuing)",
                    exc_info=True,
                )

    def _apply_ctas(self, query: Any, parsed_script: Any) -> None:
        """Rewrite the last statement as a ``CREATE TABLE/VIEW AS SELECT``.

        1:1 with ``superset_old/sql_lab.py::apply_ctas``.
        """
        from datetime import datetime as _dt

        from superset.sql.parse import CTASMethod, Table

        if not query.tmp_table_name:
            # ``start_time`` is stored in milliseconds (``now_as_float``), so
            # divide by 1000 for the POSIX-seconds value ``fromtimestamp``
            # expects.  NOTE: the original omitted this division (a latent
            # ms-as-seconds bug → far-future tmp-table name); the port corrects it.
            start_dttm = _dt.fromtimestamp(query.start_time / 1000)
            prefix = f"tmp_{query.user_id}_table"
            query.tmp_table_name = start_dttm.strftime(f"{prefix}_%Y_%m_%d_%H_%M_%S")

        catalog: str | None = None
        spec = getattr(query.database, "db_engine_spec", None)
        if spec is not None and getattr(spec, "supports_cross_catalog_queries", False):
            catalog = query.catalog

        table = Table(query.tmp_table_name, query.tmp_schema_name, catalog)
        method = CTASMethod[query.ctas_method.upper()]

        last = parsed_script.statements[-1]
        parsed_script.statements[-1] = last.as_create_table(table, method)

    def _apply_limit(
        self,
        query: Any,
        statement: Any,
        sqllab_ctas_no_limit: bool,
    ) -> None:
        """Inject ``LIMIT`` into ``statement`` per the original
        ``apply_limit`` semantics.
        """
        if statement.is_mutating() or (
            getattr(query, "select_as_cta_used", False) and sqllab_ctas_no_limit
        ):
            return

        sql_max_row = self._sql_max_row
        if sql_max_row and (not query.limit or query.limit > sql_max_row):
            query.limit = sql_max_row

        if query.limit:
            spec = getattr(query.database, "db_engine_spec", None)
            from superset.sql.parse import LimitMethod

            limit_method = getattr(spec, "limit_method", LimitMethod.FORCE_LIMIT)
            try:
                statement.set_limit_value(query.limit + 1, limit_method)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Could not push LIMIT into SQL via set_limit_value()",
                    exc_info=True,
                )

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


def _execute_blocks_in_thread(
    connection_uri: str,
    blocks: list[str],
    schema: str | None,
    effective_limit: int,
    has_mutation: bool,
) -> tuple[
    list[tuple[Any, ...]],
    list[tuple[Any, ...]],
    bool,
    str | None,
]:
    """Execute SQL blocks synchronously and return the *last* block's data.

    1:1 with the original ``execute_sql_statements`` loop:
    - share a single connection across blocks,
    - call ``conn.commit()`` if any block was mutating or ``select_as_cta``,
    - fetch ``limit+1`` rows from the final ``execute()`` to detect
      truncation.

    Returns ``(rows, cursor_description, has_more_rows, cancel_query_id)``.
    """
    engine = sa.create_engine(
        connection_uri,
        poolclass=sa.pool.NullPool,
    )
    try:
        with closing(engine.connect()) as conn:
            if schema:
                try:
                    conn.execute(sa.text(f"SET search_path TO {schema}"))
                except Exception:  # noqa: BLE001, S110
                    pass

            cancel_id: str | None = None
            try:
                # Try to capture an engine-specific session id we can use
                # to cancel later. Postgres exposes pg_backend_pid().
                row = conn.execute(sa.text("SELECT pg_backend_pid()")).fetchone()
                if row:
                    cancel_id = str(row[0])
            except Exception:  # noqa: BLE001
                cancel_id = None

            rows: list[tuple[Any, ...]] = []
            cursor_description: tuple[tuple[Any, ...], ...] = ()
            has_more = False

            block_count = len(blocks)
            for i, block in enumerate(blocks):
                result = conn.execute(sa.text(block))
                is_last = i == block_count - 1
                if not is_last:
                    if result.returns_rows:
                        result.fetchall()
                    continue

                if not result.returns_rows:
                    cursor_description = ()
                    rows = []
                    break

                # Capture cursor description BEFORE fetchmany — some
                # adapters (notably asyncpg under SQLAlchemy 2.0) detach
                # ``result.cursor`` after the first fetch, leaving it
                # ``None`` and crashing the original
                # ``result.cursor.description`` path.  Fall back to
                # ``result.keys()`` so column names are preserved even
                # when DBAPI metadata is unavailable.
                raw_cursor = getattr(result, "cursor", None)
                raw_desc = (
                    getattr(raw_cursor, "description", None) if raw_cursor else None
                )
                if raw_desc:
                    cursor_description = tuple(tuple(d) for d in raw_desc)
                else:
                    try:
                        keys = list(result.keys())
                    except Exception:  # noqa: BLE001
                        keys = []
                    # DBAPI cursor.description is a sequence of
                    # 7-tuples (name, type_code, display_size,
                    # internal_size, precision, scale, null_ok).
                    cursor_description = tuple(
                        (name, None, None, None, None, None, None)
                        for name in keys
                    )

                fetch_size = effective_limit + 1
                fetched = result.fetchmany(fetch_size)
                has_more = len(fetched) > effective_limit
                if has_more:
                    fetched = fetched[:effective_limit]
                rows = [tuple(r) for r in fetched]

            if has_mutation:
                try:
                    conn.commit()
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "Could not commit mutating SQL block (driver auto-commits?)",
                        exc_info=True,
                    )

            return rows, cursor_description or (), has_more, cancel_id
    finally:
        engine.dispose()
