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
"""SqlLab command classes — SQL execution, formatting, estimation, permalinks."""

from __future__ import annotations

import asyncio
import inspect
import json  # noqa: TID251
import logging
import secrets
import time
from contextlib import closing
from datetime import date, datetime
from decimal import Decimal
from typing import Any, TYPE_CHECKING

import sqlalchemy as sa

from superset.commands.base import AsyncBaseCommand
from superset.common.query_status import QueryStatus
from superset.exceptions import CommandInvalidError, ObjectNotFoundError

if TYPE_CHECKING:
    from superset.db.daos.key_value import AsyncKeyValueDAO
    from superset.db.daos.query import AsyncQueryDAO

logger = logging.getLogger(__name__)

# Default maximum rows if no limit is provided and no config override.
_DEFAULT_SQL_MAX_ROW = 100000

# Async-capable driver prefixes that must be replaced with sync
# equivalents when executing user queries via DBAPI cursors.
_ASYNC_DRIVER_REPLACEMENTS: dict[str, str] = {
    "postgresql+asyncpg": "postgresql+psycopg2",
    "postgresql+aiopg": "postgresql+psycopg2",
    "mysql+aiomysql": "mysql+pymysql",
    "mysql+asyncmy": "mysql+pymysql",
    "sqlite+aiosqlite": "sqlite",
}

# SQLAlchemy backend name -> sqlglot dialect name. sqlglot uses "postgres"
# while SQLAlchemy uses "postgresql"; a few others also differ.
_SQLGLOT_DIALECT_ALIASES: dict[str, str] = {
    "postgresql": "postgres",
    "mssql": "tsql",
    "mysql": "mysql",
    "sqlite": "sqlite",
    "trino": "trino",
    "presto": "presto",
    "snowflake": "snowflake",
    "bigquery": "bigquery",
    "redshift": "redshift",
    "clickhouse": "clickhouse",
    "oracle": "oracle",
    "hive": "hive",
    "spark": "spark",
    "databricks": "databricks",
    "duckdb": "duckdb",
}


def _map_sqlglot_dialect(engine: str | None) -> str | None:
    """Map a SQLAlchemy backend name (e.g. "postgresql") to a sqlglot
    dialect name (e.g. "postgres"). Returns None if no engine is given,
    letting sqlglot auto-detect. Unknown engines are returned as-is.
    """
    if not engine:
        return None
    name = engine.split("+", 1)[0].lower()
    return _SQLGLOT_DIALECT_ALIASES.get(name, name)


def _make_json_safe(value: Any) -> Any:
    """Convert Python values to JSON-serializable types."""
    if value is None:
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, memoryview):
        return bytes(value).decode("utf-8", errors="replace")
    return value


def _to_sync_uri(uri: str) -> str:
    """Replace async driver prefixes with sync equivalents.

    E.g. ``postgresql+asyncpg://...`` becomes ``postgresql+psycopg2://...``.
    """
    for async_prefix, sync_prefix in _ASYNC_DRIVER_REPLACEMENTS.items():
        if uri.startswith(async_prefix):
            return sync_prefix + uri[len(async_prefix) :]
    return uri


def _build_connection_uri(database: Any) -> str:
    """Build a usable sync connection string from a Database model.

    The ``Database.sqlalchemy_uri`` column stores the URI with the password
    masked (``PASSWORD_MASK``). The real password is in ``Database.password``.
    We reconstruct the full URI by injecting the password back in.
    """
    raw_uri = database.sqlalchemy_uri or ""
    try:
        url = sa.engine.make_url(raw_uri)
    except Exception:
        # If the stored URI is totally invalid, return as-is and let
        # SQLAlchemy raise a proper error later.
        return _to_sync_uri(raw_uri)

    # Inject password from the separate column if it was masked.
    password = getattr(database, "password", None)
    if password:
        url = url.set(password=password)

    return _to_sync_uri(url.render_as_string(hide_password=False))


def _execute_sql_in_thread(
    connection_uri: str,
    sql: str,
    schema: str | None,
    effective_limit: int,
) -> tuple[
    list[tuple[Any, ...]],
    list[tuple[Any, ...]],
    bool,
]:
    """Run SQL synchronously (called via ``asyncio.to_thread``).

    Returns ``(rows, cursor_description, has_more_rows)``.
    """
    engine = sa.create_engine(
        connection_uri,
        poolclass=sa.pool.NullPool,  # one-shot; no pool needed
    )
    try:
        with closing(engine.connect()) as conn:
            # Set search_path / schema when provided
            if schema:
                # Try the standard SET search_path for Postgres;
                # for other dialects this is typically a no-op or
                # handled by the driver. We intentionally swallow
                # errors from non-Postgres engines.
                try:
                    conn.execute(sa.text(f"SET search_path TO {schema}"))
                except Exception:  # noqa: BLE001, S110
                    pass  # Non-Postgres engines may not support SET search_path

            result = conn.execute(sa.text(sql))

            # Fetch limit+1 to detect overflow
            fetch_size = effective_limit + 1
            rows = result.fetchmany(fetch_size)
            cursor_description = result.cursor.description if result.cursor else ()

            has_more = len(rows) > effective_limit
            if has_more:
                rows = rows[:effective_limit]

            return rows, cursor_description or (), has_more
    finally:
        engine.dispose()


class ExecuteSQLCommand(AsyncBaseCommand[dict[str, Any]]):
    """Execute SQL query — the core SqlLab operation.

    Replaces the original Superset chain of ``SqlJsonExecutionContext`` ->
    ``SynchronousSqlJsonExecutor`` -> ``get_sql_results()`` ->
    ``execute_sql_statements()``.

    The user database query runs synchronously inside
    ``asyncio.to_thread()`` (via a one-shot ``NullPool`` engine) while
    the metadata-database Query record is managed through the async
    session provided by the DAO.
    """

    def __init__(
        self,
        dao: AsyncQueryDAO,
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
        sql_max_row: int = _DEFAULT_SQL_MAX_ROW,
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

    async def validate(self) -> None:
        if not self._sql.strip():
            raise CommandInvalidError("SQL query cannot be empty")
        if not self._database_id:
            raise CommandInvalidError("database_id is required")

    async def run(self) -> dict[str, Any]:
        session = self._dao.session

        # ------------------------------------------------------------------
        # 1. Load the Database record
        # ------------------------------------------------------------------
        from superset.models.core import Database
        from superset.models.sql_lab import LimitingFactor, Query

        db_row = await session.get(Database, self._database_id)
        if db_row is None:
            raise ObjectNotFoundError("Database", self._database_id)

        # ------------------------------------------------------------------
        # 2. Determine effective row limit
        # ------------------------------------------------------------------
        effective_limit = self._query_limit or self._sql_max_row

        # ------------------------------------------------------------------
        # 3. Create Query record with PENDING status
        # ------------------------------------------------------------------
        start_time = time.time()
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
        session.add(query)
        await session.flush()
        query_id = query.id  # populated after flush

        # ------------------------------------------------------------------
        # 4. Set status to RUNNING
        # ------------------------------------------------------------------
        query.status = QueryStatus.RUNNING  # type: ignore[assignment]
        query.start_running_time = time.time()  # type: ignore[assignment]
        await session.flush()

        # ------------------------------------------------------------------
        # 5. Build sync connection string & execute in thread
        # ------------------------------------------------------------------
        connection_uri = _build_connection_uri(db_row)

        try:
            rows_raw, cursor_desc, has_more = await asyncio.to_thread(
                _execute_sql_in_thread,
                connection_uri,
                self._sql,
                self._schema,
                effective_limit,
            )
        except Exception as exc:
            # ----------------------------------------------------------
            # Execution failed — update Query record and return error
            # ----------------------------------------------------------
            logger.exception(
                "Query %d execution failed: %s",
                query_id,
                exc,
            )
            query.status = QueryStatus.FAILED  # type: ignore[assignment]
            query.error_message = str(exc)
            query.end_time = time.time()  # type: ignore[assignment]
            query.progress = 0  # type: ignore[assignment]
            await session.flush()

            return {
                "status": QueryStatus.FAILED,
                "error": str(exc),
                "query": query.to_dict(),
                "query_id": query_id,
                "data": [],
                "columns": [],
                "selected_columns": [],
                "expanded_columns": [],
            }

        # ------------------------------------------------------------------
        # 6. Build column metadata from cursor.description
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

        # ------------------------------------------------------------------
        # 7. Convert rows to list-of-dicts with JSON-safe values
        # ------------------------------------------------------------------
        data: list[dict[str, Any]] = []
        for row in rows_raw:
            data.append(
                {col_names[i]: _make_json_safe(val) for i, val in enumerate(row)}
            )

        # ------------------------------------------------------------------
        # 8. Determine limiting factor
        # ------------------------------------------------------------------
        if has_more:
            if self._query_limit and self._query_limit < self._sql_max_row:
                limiting_factor = LimitingFactor.DROPDOWN
            else:
                limiting_factor = LimitingFactor.QUERY
        else:
            limiting_factor = LimitingFactor.NOT_LIMITED

        # ------------------------------------------------------------------
        # 9. Update Query record — SUCCESS
        # ------------------------------------------------------------------
        query.status = QueryStatus.SUCCESS  # type: ignore[assignment]
        query.rows = len(data)  # type: ignore[assignment]
        query.progress = 100  # type: ignore[assignment]
        query.end_time = time.time()  # type: ignore[assignment]
        query.limiting_factor = limiting_factor  # type: ignore[assignment]

        # Store column metadata in the extra JSON
        query.set_extra_json_key("columns", columns)  # type: ignore[attr-defined]
        query.set_extra_json_key("progress", None)  # type: ignore[attr-defined]

        await session.flush()

        # ------------------------------------------------------------------
        # 10. Build response matching original format
        # ------------------------------------------------------------------
        query_dict = query.to_dict()
        query_dict["state"] = QueryStatus.SUCCESS

        return {
            "status": QueryStatus.SUCCESS,
            "data": data,
            "columns": columns,
            "selected_columns": columns,
            "expanded_columns": [],
            "query": query_dict,
            "query_id": query_id,
        }


class EstimateQueryCostCommand(AsyncBaseCommand[list[dict[str, Any]]]):
    def __init__(self, database_id: int, sql: str, schema: str | None = None) -> None:
        self._database_id = database_id
        self._sql = sql
        self._schema = schema

    async def validate(self) -> None:
        if not self._sql.strip():
            raise CommandInvalidError("SQL query cannot be empty")

    async def run(self) -> list[dict[str, Any]]:
        # Delegates to engine spec's estimate_query_cost()
        return [{"cost": "Not available"}]


class FormatSQLCommand(AsyncBaseCommand[str]):
    def __init__(self, sql: str, engine: str | None = None) -> None:
        self._sql = sql
        self._engine = engine

    async def validate(self) -> None:
        if not self._sql.strip():
            raise CommandInvalidError("SQL cannot be empty")

    async def run(self) -> str:
        import asyncio

        try:
            import sqlglot
            from sqlglot.errors import SqlglotError
        except ImportError:
            logger.debug("sqlglot not available, returning unformatted SQL")
            return self._sql

        dialect = _map_sqlglot_dialect(self._engine)
        try:
            # sqlglot.transpile is CPU-bound; offload to a thread to avoid
            # blocking the async event loop.
            result = await asyncio.to_thread(
                sqlglot.transpile,
                self._sql,
                read=dialect,
                pretty=True,
            )
            return result[0]
        except (SqlglotError, ValueError):
            logger.warning("SQL formatting failed, returning original", exc_info=True)
            return self._sql


class GetSQLResultsCommand(AsyncBaseCommand[dict[str, Any]]):
    def __init__(
        self,
        key: str,
        rows: int | None = None,
        cache_manager: Any = None,
        dao: "AsyncQueryDAO | Any | None" = None,
    ) -> None:
        self._key = key
        self._rows = rows
        self._cache_manager = cache_manager
        self._dao = dao

    async def validate(self) -> None:
        if not self._key:
            raise CommandInvalidError("key is required")

    async def run(self) -> dict[str, Any]:
        # 1. Try the cache first
        if self._cache_manager is not None:
            try:
                getter = self._cache_manager.get(self._key)
                result = await getter if inspect.isawaitable(getter) else getter
                if result is not None:
                    if self._rows is not None and "data" in result:
                        result["data"] = result["data"][: self._rows]
                    return result
            except Exception:  # noqa: BLE001
                logger.warning("Cache get failed for key %s", self._key, exc_info=True)

        # 2. Fallback: look up the Query by results_key and return its metadata
        if self._dao is not None:
            try:
                query = await self._dao.find_one_or_none(results_key=self._key)
                if query is not None:
                    # Return the query metadata; actual data re-execution
                    # requires a database connection which may not be available.
                    extra = (
                        query.get_extra_dict()
                        if hasattr(query, "get_extra_dict")
                        else {}
                    )
                    columns = extra.get("columns", [])
                    return {
                        "status": query.status or "success",
                        "query": (
                            query.to_dict()
                            if hasattr(query, "to_dict")
                            else {"id": query.id}
                        ),
                        "columns": columns,
                        "selected_columns": columns,
                        "expanded_columns": [],
                        "data": [],
                    }
            except Exception:  # noqa: BLE001
                logger.warning(
                    "DAO fallback failed for key %s",
                    self._key,
                    exc_info=True,
                )

        return {"status": "not_found", "data": [], "columns": []}


class CreateSqlLabPermalinkCommand(AsyncBaseCommand[str]):
    """Create a SQL Lab permalink — deterministic UUID + hashids id.

    Mirrors original CreateSqlLabPermalinkCommand at
    superset_old/commands/sql_lab/permalink/create.py — stores the
    state under a deterministic UUID derived from (salt, state) and
    returns a hashids-encoded autoincrement id as the URL key.
    """

    def __init__(
        self,
        dao: AsyncKeyValueDAO,
        state: dict[str, Any],
        user_id: int | None = None,
    ) -> None:
        self._dao = dao
        self._state = state
        self._user_id = user_id

    async def validate(self) -> None:
        pass

    async def run(self) -> str:
        from superset.key_value.shared_entries import get_permalink_salt
        from superset.key_value.types import KeyValueResource, SharedKey
        from superset.key_value.utils import (
            encode_permalink_key,
            get_deterministic_uuid,
        )

        session = self._dao.session
        salt = await get_permalink_salt(session, SharedKey.SQLLAB_PERMALINK_SALT)
        deterministic_uuid = get_deterministic_uuid(salt, (self._user_id, self._state))

        encoded = json.dumps(self._state, sort_keys=True).encode("utf-8")
        existing = await self._dao.get_entry_by_key(
            resource=KeyValueResource.SQLLAB_PERMALINK.value,
            key=deterministic_uuid,
        )
        if existing is None:
            entry = await self._dao.create_entry(
                resource=KeyValueResource.SQLLAB_PERMALINK.value,
                value=encoded,
                key=deterministic_uuid,
            )
            await session.flush()
        else:
            existing.value = encoded  # type: ignore[assignment]
            entry = existing

        if entry.id is None:
            raise RuntimeError("SQL Lab permalink entry missing autogenerated id")
        return encode_permalink_key(key=entry.id, salt=salt)


class GetSqlLabPermalinkCommand(AsyncBaseCommand[dict[str, Any]]):
    def __init__(self, dao: AsyncKeyValueDAO, key: str) -> None:
        self._dao = dao
        self._key = key

    async def validate(self) -> None:
        if not self._key:
            raise CommandInvalidError("key is required")

    async def run(self) -> dict[str, Any]:
        # Decode hashids string back to the autoincrement int id, then
        # look up the JSON state. Mirrors original
        # GetSqlLabPermalinkCommand at
        # superset_old/commands/sql_lab/permalink/get.py.
        from superset.key_value.exceptions import KeyValueParseKeyError
        from superset.key_value.shared_entries import get_permalink_salt
        from superset.key_value.types import KeyValueResource, SharedKey
        from superset.key_value.utils import decode_permalink_id

        session = self._dao.session
        salt = await get_permalink_salt(session, SharedKey.SQLLAB_PERMALINK_SALT)
        try:
            entry_id = decode_permalink_id(self._key, salt=salt)
        except (KeyValueParseKeyError, Exception) as ex:  # noqa: BLE001
            raise ObjectNotFoundError("SqlLabPermalink", self._key) from ex

        value = await self._dao.get_value_by_key(
            resource=KeyValueResource.SQLLAB_PERMALINK.value,
            key=entry_id,
        )
        if value is None:
            raise ObjectNotFoundError("SqlLabPermalink", self._key)
        if isinstance(value, dict):
            return value
        return {"value": value}
