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
# mypy: ignore-errors
"""Celery task for SQL Lab async query execution.

Direct port of ``superset_old/sql_lab.py::get_sql_results`` —
``execute_sql_statements`` rolled inline. Celery stays synchronous
per the Liteset design doc, so this module imports the synchronous
SQLAlchemy session via :func:`superset.db.session.get_sync_session`,
talks to the analytical database through ``database.get_sqla_engine``
(sync), and writes results to ``results_backend`` exactly as the
original did (msgpack + zlib + Arrow IPC).
"""

from __future__ import annotations

import dataclasses
import logging
import sys
import time
import uuid
from contextlib import closing
from datetime import datetime
from typing import Any, cast, Optional

import sqlalchemy as sa
from celery.exceptions import SoftTimeLimitExceeded

from superset.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)

BYTES_IN_MB = 1024 * 1024


# ---------------------------------------------------------------------------
# soft_time_limit resolution — matches superset_old/sql_lab.py defaults
# (SQLLAB_ASYNC_TIME_LIMIT_SEC = 21600, SQLLAB_HARD_TIMEOUT = 21660).
# ---------------------------------------------------------------------------


def _resolve_timeouts() -> tuple[int, int]:
    try:
        from superset.config import SupersetSettings

        settings = SupersetSettings()  # type: ignore[call-arg]
        soft = int(getattr(settings, "sqllab_async_time_limit_sec", 21600))
        hard = soft + 60
        return soft, hard
    except Exception:  # noqa: BLE001
        return 21600, 21660


_SOFT_TIME_LIMIT, _HARD_TIME_LIMIT = _resolve_timeouts()


# ---------------------------------------------------------------------------
# Custom exceptions matching the original
# ---------------------------------------------------------------------------


class SqlLabException(Exception):  # noqa: N818
    """Base exception used by the original sql_lab module."""


class SqlLabQueryStoppedException(SqlLabException):
    """Raised when the query was set to STOPPED while executing."""


# ---------------------------------------------------------------------------
# Public Celery task
# ---------------------------------------------------------------------------


@celery_app.task(
    name="sql_lab.get_sql_results",
    time_limit=_HARD_TIME_LIMIT,
    soft_time_limit=_SOFT_TIME_LIMIT,
)
def get_sql_results(  # pylint: disable=too-many-arguments
    query_id: int,
    rendered_query: str,
    return_results: bool = True,
    store_results: bool = False,
    username: Optional[str] = None,
    start_time: Optional[float] = None,
    expand_data: bool = False,
    log_params: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """Celery entrypoint for asynchronous SQL Lab execution.

    1:1 with ``superset_old/sql_lab.py::get_sql_results``.
    """
    try:
        return execute_sql_statements(
            query_id=query_id,
            rendered_query=rendered_query,
            return_results=return_results,
            store_results=store_results,
            start_time=start_time,
            expand_data=expand_data,
            log_params=log_params,
            username=username,
        )
    except Exception as ex:  # noqa: BLE001
        logger.debug("Query %d: %s", query_id, ex)
        try:
            session = _get_session()
            try:
                query = _get_query(session, query_id)
                return _handle_query_error(session, ex, query)
            finally:
                session.close()
        except Exception:  # noqa: BLE001
            logger.exception("Failed to record SQL Lab error for query %s", query_id)
            return None


# ---------------------------------------------------------------------------
# Core execution — execute_sql_statements 1:1 from the original
# ---------------------------------------------------------------------------


def execute_sql_statements(  # noqa: C901, PLR0912, PLR0915
    query_id: int,
    rendered_query: str,
    return_results: bool,
    store_results: bool,
    start_time: Optional[float],
    expand_data: bool,
    log_params: Optional[dict[str, Any]],
    username: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Execute the SQL query — multi-statement aware, results-backend aware.

    Restored 1:1 from ``superset_old/sql_lab.py::execute_sql_statements``.
    The only mechanical adjustments are:

    - Sessions: ``superset.db.session.get_sync_session()`` instead of
      Flask's ``db.session``.
    - Results backend / msgpack flag: read from
      :class:`SupersetSettings` rather than module-level globals.
    - User context: looked up via the sync session by ``username``
      (the parameter is passed by ``ExecuteSQLCommand``).
    - The ``override_user`` Flask-thread-local helper is replaced by
      :func:`superset.utils.core.set_current_user`.
    """
    session = _get_session()
    try:
        query = _get_query(session, query_id)
        payload: dict[str, Any] = {"query_id": query_id}
        database = query.database
        db_engine_spec = database.db_engine_spec

        if hasattr(db_engine_spec, "patch"):
            try:
                db_engine_spec.patch()
            except Exception:  # noqa: BLE001
                logger.debug("db_engine_spec.patch() failed", exc_info=True)

        results_backend, results_backend_use_msgpack = _resolve_results_backend()

        if database.allow_run_async and not results_backend:
            from superset.exceptions import (
                SupersetResultsBackendNotConfigureException,
            )

            raise SupersetResultsBackendNotConfigureException()

        # Bind the user context for RLS / auditing.
        if username:
            try:
                from superset.utils.core import set_current_user

                user = _get_user_by_username(session, username)
                if user is not None:
                    set_current_user(user)
            except Exception:  # noqa: BLE001
                logger.debug("Could not bind current user for async task", exc_info=True)

        logger.info("Query %s: Set query to 'running'", str(query_id))
        from superset.common.query_status import QueryStatus

        query.status = QueryStatus.RUNNING
        query.start_running_time = time.time()
        session.commit()

        from superset.commands.sqllab._shared import get_engine_name
        from superset.sql.parse import CTASMethod, SQLScript, Table

        parsed_script = SQLScript(rendered_query, engine=get_engine_name(database))

        # ------------------------------------------------------------------
        # DISALLOWED_SQL_FUNCTIONS (security)
        # ------------------------------------------------------------------
        disallowed_functions = _resolve_disallowed_functions(get_engine_name(database))
        if disallowed_functions and parsed_script.check_functions_present(
            disallowed_functions
        ):
            from superset.exceptions import SupersetDisallowedSQLFunctionException

            raise SupersetDisallowedSQLFunctionException(disallowed_functions)

        # ------------------------------------------------------------------
        # DML allowance
        # ------------------------------------------------------------------
        if parsed_script.has_mutation() and not getattr(database, "allow_dml", False):
            from superset.exceptions import SupersetDMLNotAllowedException

            raise SupersetDMLNotAllowedException()

        # ------------------------------------------------------------------
        # RLS_IN_SQLLAB
        # ------------------------------------------------------------------
        if _is_feature_enabled("RLS_IN_SQLLAB"):
            try:
                from superset.utils.rls import apply_rls

                default_schema = database.get_default_schema_for_query(query) or ""
                for statement in parsed_script.statements:
                    apply_rls(database, query.catalog, default_schema, statement)
            except Exception:  # noqa: BLE001
                logger.warning("apply_rls failed in async task", exc_info=True)

        # ------------------------------------------------------------------
        # CTAS / CVAS
        # ------------------------------------------------------------------
        if query.select_as_cta:
            from superset.exceptions import (
                SupersetInvalidCTASException,
                SupersetInvalidCVASException,
            )

            if (
                query.ctas_method == CTASMethod.TABLE.name
                and not parsed_script.is_valid_ctas()
            ):
                raise SupersetInvalidCTASException()
            if (
                query.ctas_method == CTASMethod.VIEW.name
                and not parsed_script.is_valid_cvas()
            ):
                raise SupersetInvalidCVASException()

            parsed_script.statements[-1] = _apply_ctas(
                query,
                parsed_script.statements[-1],
            )
            query.select_as_cta_used = True

        # ------------------------------------------------------------------
        # apply_limit per statement
        # ------------------------------------------------------------------
        sqllab_ctas_no_limit = _is_sqllab_ctas_no_limit()
        sql_max_row = _resolve_sql_max_row()
        for statement in parsed_script.statements:
            _apply_limit(query, statement, sqllab_ctas_no_limit, sql_max_row)

        # ------------------------------------------------------------------
        # Build blocks
        # ------------------------------------------------------------------
        allows_comments = getattr(db_engine_spec, "allows_sql_comments", True)
        run_as_one = getattr(db_engine_spec, "run_multiple_statements_as_one", False)
        if run_as_one:
            blocks = [parsed_script.format(comments=allows_comments)]
        else:
            blocks = [
                statement.format(comments=allows_comments)
                for statement in parsed_script.statements
            ]

        # ------------------------------------------------------------------
        # Execute
        # ------------------------------------------------------------------
        result_set = None
        from superset.utils.core import QuerySource
        from superset.constants import QUERY_CANCEL_KEY

        with database.get_sqla_engine(
            catalog=query.catalog,
            schema=query.schema,
            source=QuerySource.SQL_LAB,
        ) as engine:
            with closing(engine.raw_connection()) as conn:
                cursor = conn.cursor()
                try:
                    cancel_query_id = _get_cancel_query_id(db_engine_spec, cursor, query)
                    if cancel_query_id is not None:
                        query.set_extra_json_key(QUERY_CANCEL_KEY, cancel_query_id)
                        session.commit()
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "Could not extract cancel_query id from cursor", exc_info=True
                    )

                block_count = len(blocks)
                for i, block in enumerate(blocks):
                    session.refresh(query)
                    if query.status == QueryStatus.STOPPED:
                        payload.update({"status": query.status})
                        return payload

                    msg = (
                        f"Running block {i + 1} out of {block_count}"
                        if block_count > 1
                        else "Running query"
                    )
                    logger.info("Query %s: %s", str(query_id), msg)
                    query.set_extra_json_key("progress", msg)
                    session.commit()

                    if hasattr(database, "mutate_sql_based_on_config"):
                        try:
                            query.executed_sql = database.mutate_sql_based_on_config(
                                block
                            )
                        except TypeError:
                            query.executed_sql = database.mutate_sql_based_on_config(
                                block, is_split=True
                            )
                    else:
                        query.executed_sql = block

                    try:
                        result_set = _execute_query(query, cursor, db_engine_spec)
                    except SqlLabQueryStoppedException:
                        payload.update({"status": QueryStatus.STOPPED})
                        return payload
                    except SoftTimeLimitExceeded:
                        query.status = QueryStatus.TIMED_OUT
                        session.commit()
                        raise
                    except Exception as ex:  # noqa: BLE001
                        return _handle_query_error(session, ex, query, payload)

                if parsed_script.has_mutation() or query.select_as_cta:
                    try:
                        conn.commit()
                    except Exception:  # noqa: BLE001
                        logger.warning(
                            "Could not commit mutating SQL block", exc_info=True
                        )

        # ------------------------------------------------------------------
        # Success — write results-backend payload
        # ------------------------------------------------------------------
        if result_set is None:
            # No result set (DDL only or all blocks were mutations) —
            # mirror the original by treating this as success with empty data.
            data: list[Any] = []
            selected_columns: list[Any] = []
            all_columns: list[Any] = []
            expanded_columns: list[Any] = []
        else:
            query.rows = result_set.size
            if query.select_as_cta:
                try:
                    query.select_sql = database.select_star(
                        Table(query.tmp_table_name, query.tmp_schema_name),
                        limit=query.limit,
                        show_cols=False,
                        latest_partition=False,
                    )
                except Exception:  # noqa: BLE001
                    logger.debug("select_star failed", exc_info=True)

            use_arrow_data = store_results and bool(results_backend_use_msgpack)
            (
                data,
                selected_columns,
                all_columns,
                expanded_columns,
            ) = _serialize_and_expand_data(
                result_set, db_engine_spec, use_arrow_data, expand_data
            )

        query.progress = 100
        query.set_extra_json_key("progress", None)
        if result_set is not None:
            query.set_extra_json_key("columns", selected_columns)
        query.end_time = time.time()

        payload.update(
            {
                "status": QueryStatus.SUCCESS,
                "data": data,
                "columns": all_columns,
                "selected_columns": selected_columns,
                "expanded_columns": expanded_columns,
                "query": query.to_dict() if hasattr(query, "to_dict") else {},
            }
        )
        if "query" in payload and payload["query"]:
            payload["query"]["state"] = QueryStatus.SUCCESS

        # ------------------------------------------------------------------
        # Persist to results backend (msgpack + zlib + payload size guard)
        # ------------------------------------------------------------------
        if store_results and results_backend:
            key = str(uuid.uuid4())
            payload["query"]["resultsKey"] = key
            logger.info(
                "Query %s: Storing results in results backend, key: %s",
                str(query_id),
                key,
            )

            serialized_payload = _serialize_payload(
                payload, bool(results_backend_use_msgpack)
            )

            sql_lab_payload_max_mb = _resolve_sqllab_payload_max_mb()
            if sql_lab_payload_max_mb:
                serialized_payload_size = sys.getsizeof(serialized_payload)
                max_bytes = sql_lab_payload_max_mb * BYTES_IN_MB
                if serialized_payload_size > max_bytes:
                    from superset.errors import (
                        ErrorLevel,
                        SupersetError,
                        SupersetErrorType,
                    )
                    from superset.exceptions import SupersetErrorException

                    raise SupersetErrorException(
                        SupersetError(
                            message=(
                                f"Result size ("
                                f"{serialized_payload_size / BYTES_IN_MB:.2f} MB"
                                f") exceeds the allowed limit of "
                                f"{sql_lab_payload_max_mb} MB."
                            ),
                            error_type=SupersetErrorType.RESULT_TOO_LARGE_ERROR,
                            level=ErrorLevel.ERROR,
                        )
                    )

            cache_timeout = getattr(database, "cache_timeout", None)
            if cache_timeout is None:
                cache_timeout = _resolve_cache_default_timeout()

            from superset.utils.core import zlib_compress

            compressed = zlib_compress(serialized_payload)

            write_success = results_backend.set(key, compressed, cache_timeout)
            if not write_success:
                logger.error(
                    "Query %s: Failed to store results in backend, key: %s",
                    str(query_id),
                    key,
                )
                query.results_key = None
                if not return_results:
                    query.status = QueryStatus.FAILED
                    query.error_message = (
                        "Failed to store query results in the results backend. "
                        "Please try again or contact your administrator."
                    )
                    session.commit()
                    from superset.errors import (
                        ErrorLevel,
                        SupersetError,
                        SupersetErrorType,
                    )
                    from superset.exceptions import SupersetErrorException

                    raise SupersetErrorException(
                        SupersetError(
                            message="Failed to store query results. Please try again.",
                            error_type=SupersetErrorType.RESULTS_BACKEND_ERROR,
                            level=ErrorLevel.ERROR,
                        )
                    )
            else:
                query.results_key = key
                logger.info(
                    "Query %s: Successfully stored results in backend, key: %s",
                    str(query_id),
                    key,
                )

        if query.status != QueryStatus.FAILED:
            query.status = QueryStatus.SUCCESS
        session.commit()

        if return_results:
            # Re-build non-Arrow data for the inline response.
            if (
                store_results
                and bool(results_backend_use_msgpack)
                and result_set is not None
            ):
                (
                    data,
                    selected_columns,
                    all_columns,
                    expanded_columns,
                ) = _serialize_and_expand_data(
                    result_set, db_engine_spec, False, expand_data
                )
                payload.update(
                    {
                        "data": data,
                        "columns": all_columns,
                        "selected_columns": selected_columns,
                        "expanded_columns": expanded_columns,
                    }
                )
            return payload

        return None
    finally:
        session.close()


# ---------------------------------------------------------------------------
# helpers — direct ports of the corresponding functions in superset_old
# ---------------------------------------------------------------------------


def _get_session() -> Any:
    from superset.db.session import get_sync_session

    return get_sync_session()


def _get_query(session: Any, query_id: int) -> Any:
    """Look up a Query row, with the original's exponential-backoff retry."""
    from superset.models.sql_lab import Query

    last_err: Exception | None = None
    for attempt in range(5):
        try:
            return session.execute(
                sa.select(Query).where(Query.id == query_id)
            ).scalar_one()
        except Exception as ex:  # noqa: BLE001
            last_err = ex
            time.sleep(min(2**attempt, 4))
    raise SqlLabException("Failed at getting query") from last_err


def _get_user_by_username(session: Any, username: str) -> Any | None:
    try:
        from superset.models.security import User
    except Exception:  # noqa: BLE001
        return None
    try:
        return session.execute(
            sa.select(User).where(User.username == username)
        ).scalar_one_or_none()
    except Exception:  # noqa: BLE001
        return None


def _resolve_results_backend() -> tuple[Any | None, bool]:
    try:
        from superset.config import SupersetSettings

        settings = SupersetSettings()  # type: ignore[call-arg]
        return (
            getattr(settings, "results_backend", None),
            bool(getattr(settings, "results_backend_use_msgpack", True)),
        )
    except Exception:  # noqa: BLE001
        return None, True


def _resolve_disallowed_functions(engine_name: str) -> set[str]:
    try:
        from superset.config import SupersetSettings

        settings = SupersetSettings()  # type: ignore[call-arg]
        functions: dict[str, set[str]] = (
            getattr(settings, "disallowed_sql_functions", {}) or {}
        )
        return set(functions.get(engine_name, set()))
    except Exception:  # noqa: BLE001
        return set()


def _is_feature_enabled(name: str) -> bool:
    try:
        from superset.utils.feature_flags import feature_flag_manager

        return feature_flag_manager.is_feature_enabled(name)
    except Exception:  # noqa: BLE001
        return False


def _is_sqllab_ctas_no_limit() -> bool:
    try:
        from superset.config import SupersetSettings

        settings = SupersetSettings()  # type: ignore[call-arg]
        return bool(getattr(settings, "sqllab_ctas_no_limit", False))
    except Exception:  # noqa: BLE001
        return False


def _resolve_sql_max_row() -> int:
    try:
        from superset.config import SupersetSettings

        settings = SupersetSettings()  # type: ignore[call-arg]
        return int(getattr(settings, "sql_max_row", 100000))
    except Exception:  # noqa: BLE001
        return 100000


def _resolve_sqllab_payload_max_mb() -> int | None:
    try:
        from superset.config import SupersetSettings

        settings = SupersetSettings()  # type: ignore[call-arg]
        value = getattr(settings, "sqllab_payload_max_mb", None)
        return int(value) if value else None
    except Exception:  # noqa: BLE001
        return None


def _resolve_cache_default_timeout() -> int:
    try:
        from superset.config import SupersetSettings

        settings = SupersetSettings()  # type: ignore[call-arg]
        return int(getattr(settings, "cache_default_timeout", 86400))
    except Exception:  # noqa: BLE001
        return 86400


def _apply_ctas(query: Any, statement: Any) -> Any:
    """Port of ``superset_old/sql_lab.py::apply_ctas``."""
    from superset.sql.parse import CTASMethod, Table

    if not query.tmp_table_name:
        start_dttm = datetime.fromtimestamp(query.start_time)
        prefix = f"tmp_{query.user_id}_table"
        query.tmp_table_name = start_dttm.strftime(f"{prefix}_%Y_%m_%d_%H_%M_%S")

    catalog = (
        query.catalog
        if getattr(
            query.database.db_engine_spec, "supports_cross_catalog_queries", False
        )
        else None
    )
    table = Table(query.tmp_table_name, query.tmp_schema_name, catalog)
    method = CTASMethod[query.ctas_method.upper()]
    return statement.as_create_table(table, method)


def _apply_limit(
    query: Any,
    statement: Any,
    sqllab_ctas_no_limit: bool,
    sql_max_row: int,
) -> None:
    """Port of ``superset_old/sql_lab.py::apply_limit``."""
    from superset.sql.parse import LimitMethod

    if statement.is_mutating() or (
        getattr(query, "select_as_cta_used", False) and sqllab_ctas_no_limit
    ):
        return

    if sql_max_row and (not query.limit or query.limit > sql_max_row):
        query.limit = sql_max_row

    if query.limit:
        spec = getattr(query.database, "db_engine_spec", None)
        limit_method = getattr(spec, "limit_method", LimitMethod.FORCE_LIMIT)
        try:
            statement.set_limit_value(query.limit + 1, limit_method)
        except Exception:  # noqa: BLE001
            logger.warning("Could not push LIMIT into SQL", exc_info=True)


def _get_cancel_query_id(db_engine_spec: Any, cursor: Any, query: Any) -> str | None:
    """Best-effort wrapper around ``BaseEngineSpec.get_cancel_query_id``."""
    fn = getattr(db_engine_spec, "get_cancel_query_id", None)
    if fn is None:
        return None
    try:
        result = fn(cursor, query)
        return None if result is None else str(result)
    except Exception:  # noqa: BLE001
        return None


def _execute_query(query: Any, cursor: Any, db_engine_spec: Any) -> Any:
    """Run ``query.executed_sql`` against ``cursor`` and wrap the result.

    1:1 with ``superset_old/sql_lab.py::execute_query`` minus the
    Flask-specific stats logging. Returns a :class:`SupersetResultSet`
    when the original is available; otherwise a lightweight shim that
    exposes ``.size``, ``.columns``, ``.pa_table``, and ``.to_pandas_df``
    so the downstream serializer keeps working.
    """
    from superset.common.query_status import QueryStatus

    try:
        if hasattr(db_engine_spec, "execute_with_cursor"):
            db_engine_spec.execute_with_cursor(cursor, query.executed_sql, query)
        elif hasattr(db_engine_spec, "execute"):
            db_engine_spec.execute(cursor, query.executed_sql, query.database)
        else:
            cursor.execute(query.executed_sql)

        # Apply LIMIT+1 fetch trick to detect overflow.
        increased_limit = None if query.limit is None else query.limit + 1
        fetch_fn = getattr(db_engine_spec, "fetch_data", None)
        if callable(fetch_fn):
            data = fetch_fn(cursor, increased_limit)
        elif increased_limit is None:
            data = cursor.fetchall()
        else:
            data = cursor.fetchmany(increased_limit)

        from superset.models.sql_lab import LimitingFactor

        if query.limit is None or len(data) <= query.limit:
            query.limiting_factor = LimitingFactor.NOT_LIMITED
        else:
            data = data[:-1]

        cursor_description = cursor.description

        # Wrap into the original SupersetResultSet when available.
        try:
            from superset.result_set import SupersetResultSet

            return SupersetResultSet(data, cursor_description, db_engine_spec)
        except Exception:  # noqa: BLE001
            return _LiteSetResultSet(data, cursor_description)
    except SoftTimeLimitExceeded:
        raise
    except Exception as ex:
        # ``query.status`` may have been changed externally — pick that up
        try:
            from superset.db.session import get_sync_session

            with closing(get_sync_session()) as fresh_session:
                fresh = fresh_session.merge(query)
                if fresh.status == QueryStatus.STOPPED:
                    raise SqlLabQueryStoppedException() from ex
        except SqlLabQueryStoppedException:
            raise
        except Exception:  # noqa: BLE001
            pass
        raise SqlLabException(str(ex)) from ex


# ---------------------------------------------------------------------------
# Result-set serialization (msgpack + Arrow IPC)
# ---------------------------------------------------------------------------


class _LiteSetResultSet:
    """Fallback :class:`SupersetResultSet` used when the original is missing.

    Exposes the minimal surface (``size``, ``columns``, ``to_pandas_df``,
    ``pa_table``) used by :func:`_serialize_and_expand_data`.
    """

    def __init__(self, data: list[Any], cursor_description: Any) -> None:
        import pandas as pd

        column_names: list[str] = []
        if cursor_description:
            column_names = [
                str(col[0]) if not isinstance(col[0], str) else col[0]
                for col in cursor_description
            ]
        self._df = pd.DataFrame(
            data=[tuple(row) for row in data], columns=column_names
        )
        self._columns = [
            {"name": c, "column_name": c, "type": None, "is_dttm": False}
            for c in column_names
        ]

    @property
    def size(self) -> int:
        return int(len(self._df.index))

    @property
    def columns(self) -> list[Any]:
        return list(self._columns)

    def to_pandas_df(self) -> Any:
        return self._df

    @property
    def pa_table(self) -> Any:
        import pyarrow as pa

        return pa.Table.from_pandas(self._df)


def _serialize_payload(
    payload: dict[Any, Any], use_msgpack: bool = False
) -> bytes | str:
    """1:1 with ``superset_old/sql_lab.py::_serialize_payload``."""
    if use_msgpack:
        import msgpack

        from superset.utils import json as superset_json

        return msgpack.dumps(
            payload, default=superset_json.json_iso_dttm_ser, use_bin_type=True
        )

    from superset.utils import json as superset_json

    return superset_json.dumps(
        payload, default=superset_json.json_iso_dttm_ser, ignore_nan=True
    )


def _serialize_and_expand_data(
    result_set: Any,
    db_engine_spec: Any,
    use_msgpack: bool = False,
    expand_data: bool = False,
) -> tuple[bytes | str | list[Any], list[Any], list[Any], list[Any]]:
    """1:1 with ``superset_old/sql_lab.py::_serialize_and_expand_data``."""
    selected_columns = result_set.columns

    if use_msgpack:
        try:
            from superset.sqllab.utils import write_ipc_buffer  # type: ignore
        except Exception:  # noqa: BLE001
            write_ipc_buffer = None  # type: ignore[assignment]

        if write_ipc_buffer is not None:
            data = write_ipc_buffer(result_set.pa_table).to_pybytes()
        else:
            import pyarrow as pa

            sink = pa.BufferOutputStream()
            with pa.ipc.new_stream(sink, result_set.pa_table.schema) as writer:
                writer.write_table(result_set.pa_table)
            data = sink.getvalue().to_pybytes()

        all_columns = selected_columns
        expanded_columns: list[Any] = []
    else:
        df = result_set.to_pandas_df()
        try:
            from superset.dataframe import df_to_records  # type: ignore

            data = df_to_records(df) or []
        except Exception:  # noqa: BLE001
            from superset.commands.sqllab._shared import make_json_safe

            data = [
                {col: make_json_safe(val) for col, val in row.items()}
                for row in df.to_dict(orient="records")
            ]

        if expand_data and hasattr(db_engine_spec, "expand_data"):
            try:
                all_columns, data, expanded_columns = db_engine_spec.expand_data(
                    selected_columns, data
                )
            except Exception:  # noqa: BLE001
                all_columns = selected_columns
                expanded_columns = []
        else:
            all_columns = selected_columns
            expanded_columns = []

    return data, selected_columns, all_columns, expanded_columns


def _handle_query_error(
    session: Any,
    ex: Exception,
    query: Any,
    payload: dict[str, Any] | None = None,
    prefix_message: str = "",
) -> dict[str, Any]:
    """1:1 port of ``superset_old/sql_lab.py::handle_query_error``."""
    from superset.common.query_status import QueryStatus
    from superset.exceptions import SupersetErrorException, SupersetErrorsException

    payload = payload or {}
    msg = f"{prefix_message} {str(ex)}".strip()
    query.error_message = msg
    query.tmp_table_name = None
    query.status = QueryStatus.FAILED
    if not query.end_time:
        query.end_time = time.time()

    errors: list[Any] = []
    if isinstance(ex, SupersetErrorException):
        errors = [ex.error]
    elif isinstance(ex, SupersetErrorsException):
        errors = ex.errors
    else:
        try:
            errors = query.database.db_engine_spec.extract_errors(str(ex))
        except Exception:  # noqa: BLE001
            errors = []

    errors_payload = []
    for err in errors:
        try:
            errors_payload.append(dataclasses.asdict(err))
        except Exception:  # noqa: BLE001
            errors_payload.append({"message": str(err)})

    if errors_payload:
        query.set_extra_json_key("errors", errors_payload)

    session.commit()
    payload.update({"status": query.status, "error": msg, "errors": errors_payload})

    troubleshooting_link = _resolve_troubleshooting_link()
    if troubleshooting_link:
        payload["link"] = troubleshooting_link
    return payload


def _resolve_troubleshooting_link() -> str:
    try:
        from superset.config import SupersetSettings

        settings = SupersetSettings()  # type: ignore[call-arg]
        return str(getattr(settings, "troubleshooting_link", "") or "")
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------------------
# Cancel-query helper used by the controllers/query.py stop endpoint
# (kept here so the original module layout is preserved).
# ---------------------------------------------------------------------------


def cancel_query(query: Any) -> bool:
    """1:1 with ``superset_old/sql_lab.py::cancel_query``."""
    from superset.constants import QUERY_CANCEL_KEY, QUERY_EARLY_CANCEL_KEY
    from superset.utils.core import QuerySource

    db_engine_spec = query.database.db_engine_spec

    if hasattr(db_engine_spec, "has_implicit_cancel") and db_engine_spec.has_implicit_cancel():
        return True

    if hasattr(db_engine_spec, "prepare_cancel_query"):
        try:
            db_engine_spec.prepare_cancel_query(query)
        except Exception:  # noqa: BLE001
            logger.debug("prepare_cancel_query failed", exc_info=True)

    extra = cast(dict[str, Any], getattr(query, "extra", {}) or {})
    if extra.get(QUERY_EARLY_CANCEL_KEY):
        return True

    cancel_query_id = extra.get(QUERY_CANCEL_KEY)
    if cancel_query_id is None:
        return False

    with query.database.get_sqla_engine(
        catalog=query.catalog,
        schema=query.schema,
        source=QuerySource.SQL_LAB,
    ) as engine:
        with closing(engine.raw_connection()) as conn:
            with closing(conn.cursor()) as cursor:
                return db_engine_spec.cancel_query(cursor, query, cancel_query_id)
