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

Full SQL Lab execution including:

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
    SupersetException,
    SupersetTimeoutException,
)
from superset.utils import core as utils
from superset.utils.dates import now_as_float

if TYPE_CHECKING:
    from superset.db.daos.query import AsyncQueryDAO

logger = logging.getLogger(__name__)


def _map_execute_statements_error(ex: Exception, db_engine_spec: Any) -> Exception:
    from superset.exceptions import (
        SupersetErrorException,
        SupersetErrorsException,
    )

    if isinstance(ex, SupersetErrorException):
        return SupersetErrorsException([ex.error])
    if isinstance(ex, SupersetErrorsException):
        return SupersetErrorsException(ex.errors)
    return SupersetErrorsException(db_engine_spec.extract_errors(str(ex)))


class ExecuteSQLCommand(AsyncBaseCommand[dict[str, Any]]):
    """Execute SQL query — the core SqlLab operation."""

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
        # SQLLAB_FORCE_RUN_ASYNC overrides the per-request runAsync flag.
        self._run_async = self._is_feature_enabled("SQLLAB_FORCE_RUN_ASYNC") or bool(
            run_async
        )
        self._client_id = client_id or secrets.token_hex(5)[:11]
        self._user_id = user_id
        self._sql_editor_id = sql_editor_id
        self._tab = tab
        # expand_data requires both PRESTO_EXPAND_DATA feature flag and
        # the request param.
        self._expand_data = bool(
            self._is_feature_enabled("PRESTO_EXPAND_DATA") and expand_data
        )
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

        from superset.models.sql_lab import LimitingFactor, Query

        existing = await self._try_get_existing_query()
        if existing is not None and existing.status in (
            QueryStatus.RUNNING,
            QueryStatus.PENDING,
            QueryStatus.TIMED_OUT,
        ):
            # HTTP 200 (not 202 which is reserved for fresh Celery dispatches).
            query_dict = existing.to_dict() if hasattr(existing, "to_dict") else {}
            if query_dict:
                query_dict["state"] = existing.status.lower()
            await self._dao.save_metadata(existing, {"query": query_dict})
            return {
                "query": query_dict,
                "query_already_created": True,
            }

        from superset.models.core import Database

        db_row = await session.get(Database, self._database_id)
        if db_row is None:
            raise ObjectNotFoundError("Database", self._database_id)

        if self._query_limit and self._query_limit > 0:
            effective_limit = min(self._sql_max_row, self._query_limit)
        else:
            effective_limit = self._sql_max_row

        # For CTAS/CVAS: resolve target schema and persist as ``tmp_schema_name``
        # so ``apply_ctas`` can qualify the new table name.
        tmp_schema_name: str | None = None
        if self._select_as_cta:
            tmp_schema_name = self._get_ctas_target_schema_name(db_row)

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
            tmp_schema_name=tmp_schema_name,
            limit=effective_limit,
            executed_sql=self._sql,
            limiting_factor=LimitingFactor.UNKNOWN,
        )
        query.database = db_row
        session.add(query)
        await session.flush()
        query_id = query.id

        # Permission gate runs before Jinja rendering so macros that
        # execute statements still go through RBAC.
        if self._security_manager is not None and self._current_user is not None:
            try:
                await self._security_manager.raise_for_access(
                    user=self._current_user,
                    query=query,
                    template_params=self._template_params or None,
                )
            except Exception as ex:
                logger.info("SQL Lab access denied for query %s: %s", query_id, ex)
                query.status = QueryStatus.FAILED  # type: ignore[assignment]
                query.error_message = str(ex)  # type: ignore[assignment]
                query.end_time = now_as_float()  # type: ignore[assignment]
                await session.flush()
                raise

        # Any exception after Query creation must mark the row FAILED
        # so it never stays PENDING.
        try:
            rendered_sql = await self._render_jinja(db_row, query)
        except Exception:
            query.status = QueryStatus.FAILED  # type: ignore[assignment]
            query.end_time = now_as_float()  # type: ignore[assignment]
            await session.flush()
            raise

        query.executed_sql = rendered_sql  # type: ignore[assignment]

        # Without this reduction, a SQL ``LIMIT 3`` would return the full
        # result set (capped only by the dropdown max). Skipped for CTAS
        # when SQLLAB_CTAS_NO_LIMIT is set.
        ctas_no_limit = False
        if self._select_as_cta:
            try:
                from superset.config import SupersetSettings

                ctas_no_limit = bool(
                    getattr(SupersetSettings(), "sqllab_ctas_no_limit", False)  # type: ignore[call-arg]
                )
            except Exception:  # noqa: BLE001
                ctas_no_limit = False
        try:
            if not (ctas_no_limit and self._select_as_cta) and effective_limit:
                sql_limit = db_row.db_engine_spec.get_limit_from_sql(rendered_sql)
                limits = [sql_limit, effective_limit]
                if limits[0] is None or limits[0] > limits[1]:  # type: ignore[operator]
                    query.limiting_factor = LimitingFactor.DROPDOWN  # type: ignore[assignment]
                elif limits[1] > limits[0]:  # type: ignore[operator]
                    query.limiting_factor = LimitingFactor.QUERY  # type: ignore[assignment]
                else:  # limits[0] == limits[1]
                    query.limiting_factor = LimitingFactor.QUERY_AND_DROPDOWN  # type: ignore[assignment]
                query.limit = min(lim for lim in limits if lim is not None)  # type: ignore[misc]
        except Exception:
            query.status = QueryStatus.FAILED  # type: ignore[assignment]
            query.end_time = now_as_float()  # type: ignore[assignment]
            await session.flush()
            raise

        if self._run_async:
            # Must commit BEFORE dispatching the Celery task: the worker (separate
            # process, READ COMMITTED isolation) won't see the row until it's committed,
            # and it may run before the request transaction closes.
            await session.commit()
            try:
                from superset.tasks.sql_lab import get_sql_results

                task = get_sql_results.delay(
                    query_id=query_id,
                    rendered_query=rendered_sql,
                    return_results=False,
                    # The async path stores results unless this is a CTAS — it
                    # does NOT gate on SQLLAB_BACKEND_PERSISTENCE (only the
                    # sync path does).
                    store_results=not self._select_as_cta,
                    username=getattr(self._current_user, "username", None),
                    start_time=now_as_float(),
                    expand_data=self._expand_data,
                    log_params=self._log_params,
                )
                # Discard the Celery result so the result backend does not
                # accumulate stale entries.
                try:
                    task.forget()
                except NotImplementedError:
                    logger.warning(
                        "Unable to forget Celery task as backend"
                        "does not support this operation"
                    )
            except Exception as ex:  # noqa: BLE001
                # Set structured error in extra_json and raise
                # SupersetErrorException with ASYNC_WORKERS_ERROR type.
                logger.exception("Query %i: %s", query_id, str(ex))
                from superset.errors import ErrorLevel, SupersetError, SupersetErrorType
                from superset.exceptions import SupersetErrorException

                message = "Failed to start remote query on a worker."
                error = SupersetError(
                    message=message,
                    error_type=SupersetErrorType.ASYNC_WORKERS_ERROR,
                    level=ErrorLevel.ERROR,
                )
                import dataclasses

                error_payload = dataclasses.asdict(error)
                query.set_extra_json_key("errors", [error_payload])
                query.status = QueryStatus.FAILED  # type: ignore[assignment]
                query.error_message = message  # type: ignore[assignment]
                await session.flush()
                raise SupersetErrorException(error) from ex

            query.status = QueryStatus.PENDING  # type: ignore[assignment]
            await session.flush()

            query_dict = query.to_dict() if hasattr(query, "to_dict") else {}
            if query_dict:
                query_dict["state"] = QueryStatus.PENDING.lower()
            await self._dao.save_metadata(query, {"query": query_dict})
            return {"query": query_dict}

        # execute_sql_statements reads the Query via its own sync session,
        # so commit first.
        from superset.models.sql_lab import Query as _Query
        from superset.tasks.sql_lab import execute_sql_statements

        store_results = not self._select_as_cta and self._is_feature_enabled(
            "SQLLAB_BACKEND_PERSISTENCE"
        )
        await session.commit()

        sqllab_timeout = self._get_sqllab_timeout()
        try:
            payload = await asyncio.wait_for(
                asyncio.to_thread(
                    execute_sql_statements,
                    query_id,  # type: ignore[arg-type]
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
            await session.rollback()
            timed_out = await session.get(_Query, query_id)
            if timed_out is not None:
                timed_out.status = QueryStatus.TIMED_OUT  # type: ignore[assignment]
                timed_out.error_message = (
                    f"The query exceeded the {sqllab_timeout} seconds timeout."  # type: ignore[assignment]
                )
                timed_out.end_time = now_as_float()  # type: ignore[assignment]
                await session.commit()
            raise SupersetTimeoutException(
                error_type="SQLLAB_TIMEOUT_ERROR",
                message=(
                    f"The query exceeded the {sqllab_timeout} seconds timeout. "
                    "It might be too complex, or the database is under heavy load."
                ),
                level="error",
            ) from None
        except Exception as ex:  # noqa: BLE001
            logger.warning("Query %s: execute_sql_statements raised: %s", query_id, ex)
            try:
                await session.rollback()
                failed_q = await session.get(_Query, query_id)
                if failed_q is not None and failed_q.status not in (
                    QueryStatus.FAILED,
                ):
                    failed_q.status = QueryStatus.FAILED  # type: ignore[assignment]
                    failed_q.error_message = str(ex)  # type: ignore[assignment]
                    failed_q.end_time = now_as_float()  # type: ignore[assignment]
                    await session.commit()
            except Exception:  # noqa: BLE001
                logger.debug(
                    "Failed to mark query FAILED after execute_sql_statements error",
                    exc_info=True,
                )

            raise _map_execute_statements_error(ex, db_row.db_engine_spec) from ex

        # Return the payload directly: re-reading the Query via our async session would
        # surface a stale snapshot because the worker committed on
        # a separate connection.
        payload = payload or {}

        # Raise on FAILED payload so the HTTP layer returns 400/500 instead of 200.
        if payload.get("status") == QueryStatus.FAILED:
            from superset.errors import SupersetError as _SupersetError
            from superset.exceptions import (
                SupersetErrorsException as _SupersetErrorsException,
                SupersetGenericDBErrorException as _SupersetGenericDBErrorException,
            )

            errors = payload.get("errors") or []
            if errors:
                try:
                    error_objects = [_SupersetError(**e) for e in errors]
                except Exception:  # noqa: BLE001
                    error_objects = []
                if error_objects:
                    raise _SupersetErrorsException(error_objects)
            raise _SupersetGenericDBErrorException(
                payload.get("error") or "Query execution failed"
            )

        payload.setdefault("query_id", query_id)
        payload.setdefault("status", QueryStatus.SUCCESS)
        payload.setdefault("data", [])
        payload.setdefault("columns", [])
        payload.setdefault("selected_columns", payload.get("columns", []))
        payload.setdefault("expanded_columns", [])
        if payload.get("error") and "errors" not in payload:
            payload["errors"] = [{"message": payload["error"]}]
        if "query" not in payload:
            # STOPPED/no-results paths omit the query dict; re-read the committed row.
            await session.rollback()
            refreshed = await session.get(_Query, query_id)
            payload["query"] = refreshed.to_dict() if refreshed is not None else {}

        # ``save_metadata`` normalizes column_name from name so downstream consumers
        # can read column_name even when the result only provides name.
        try:
            await session.rollback()
            refreshed_q = await session.get(_Query, query_id)
            if refreshed_q is not None:
                await self._dao.save_metadata(refreshed_q, payload)
                await session.commit()
        except Exception:  # noqa: BLE001
            logger.debug(
                "save_metadata after sync execution failed; "
                "columns already persisted by execute_sql_statements",
                exc_info=True,
            )

        self._apply_display_max_row(payload)
        return payload

    async def _try_get_existing_query(self) -> Any | None:
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

        Catches ``TemplateError`` and raises a structured
        ``CommandInvalidError`` with the appropriate error type
        (``INVALID_TEMPLATE_PARAMS_ERROR`` or
        ``MISSING_TEMPLATE_PARAMS_ERROR``), issue codes, and extra data
        (``undefined_parameters``, ``template_parameters``). Also validates
        undeclared variables via ``jinja2.meta.find_undeclared_variables``
        when ``ENABLE_TEMPLATE_PROCESSING`` is enabled.
        """
        if not self._template_params and not _has_jinja_markers(self._sql):
            return self._sql.strip().strip(";")
        try:
            from superset.jinja_context import get_template_processor
        except ImportError:
            return self._sql.strip().strip(";")
        try:
            from jinja2 import TemplateError

            try:
                processor = get_template_processor(database, query=query)
                rendered = processor.process_template(
                    self._sql.strip().strip(";"), **self._template_params
                )
                if not isinstance(rendered, str):
                    return self._sql.strip().strip(";")
            except TemplateError as ex:
                err = SupersetException(
                    "The query contains one or more malformed template parameters. "
                    "Please check your query and confirm that all template "
                    "parameters are surround by double braces, for example, "
                    '"{{ ds }}". Then, try running your query again.',
                    error_type="INVALID_TEMPLATE_PARAMS_ERROR",
                )
                err.extra = {
                    "issue_codes": [
                        {
                            "code": 1028,
                            "message": (
                                "Issue 1028 - The query contains one or more "
                                "malformed template parameters."
                            ),
                        }
                    ],
                }
                raise err from ex

            if self._is_feature_enabled("ENABLE_TEMPLATE_PROCESSING"):
                self._check_undeclared_template_vars(processor, rendered)

            return rendered
        except CommandInvalidError:
            raise
        except SupersetException:
            raise

    def _check_undeclared_template_vars(self, processor: Any, rendered: str) -> None:
        try:
            from jinja2.meta import find_undeclared_variables

            syntax_tree = processor.env.parse(rendered)
            undefined_parameters = find_undeclared_variables(syntax_tree)
            if undefined_parameters:
                params_str = utils.format_list(list(undefined_parameters))
                count = len(undefined_parameters)
                if count == 1:
                    reason = f"The parameter {params_str} in your query is undefined."
                else:
                    reason = (
                        "The following parameters in your query are "
                        f"undefined: {params_str}."
                    )
                err = SupersetException(
                    f"{reason} Please check your template parameters "
                    "for syntax errors and make sure they match across "
                    "your SQL query and Set Parameters. Then, try "
                    "running your query again.",
                    error_type="MISSING_TEMPLATE_PARAMS_ERROR",
                )
                err.extra = {
                    "undefined_parameters": list(undefined_parameters),
                    "template_parameters": self._template_params,
                    "issue_codes": [
                        {
                            "code": 1006,
                            "message": (
                                "Issue 1006 - One or more parameters "
                                "specified in the query are missing."
                            ),
                        }
                    ],
                }
                raise err
        except SupersetException:
            raise
        except Exception:  # noqa: BLE001
            logger.debug(
                "find_undeclared_variables failed; proceeding with rendered SQL",
                exc_info=True,
            )

    def _get_sqllab_timeout(self) -> int:
        """Return the configured SQLLAB_TIMEOUT in seconds (default 30)."""
        try:
            from superset.config import SupersetSettings

            settings = SupersetSettings()  # type: ignore[call-arg]
            return int(getattr(settings, "sqllab_timeout", 30))
        except Exception:  # noqa: BLE001
            return 30

    def _apply_display_max_row(self, payload: dict[str, Any]) -> None:
        """
        Truncate data and set displayLimitReached=True when rows exceed
        DISPLAY_MAX_ROW.
        """
        try:
            from superset.config import SupersetSettings

            max_rows = int(getattr(SupersetSettings(), "display_max_row", 10000))  # type: ignore[call-arg]
        except Exception:  # noqa: BLE001
            max_rows = 10000

        if payload.get("status") != QueryStatus.SUCCESS:
            return
        rows = (payload.get("query") or {}).get("rows")
        if rows is None or rows <= max_rows:
            return
        data = payload.get("data")
        if isinstance(data, list):
            payload["data"] = data[:max_rows]
        payload["displayLimitReached"] = True

    def _get_ctas_target_schema_name(self, database: Any) -> str | None:
        """
        Resolve the CTAS/CVAS target schema; force_ctas_schema takes
        precedence over the config hook.
        """
        if force_ctas_schema := getattr(database, "force_ctas_schema", None):
            return force_ctas_schema

        func: Any = None
        try:
            from superset.config import SupersetSettings

            func = getattr(SupersetSettings(), "sqllab_ctas_schema_name_func", None)  # type: ignore[call-arg]
        except Exception:  # noqa: BLE001
            func = None
        if not func:
            return None
        try:
            return func(database, self._current_user, self._schema, self._sql)
        except Exception:  # noqa: BLE001
            logger.warning("SQLLAB_CTAS_SCHEMA_NAME_FUNC failed", exc_info=True)
            return None

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
