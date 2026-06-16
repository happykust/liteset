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
"""Trino engine spec -- synchronous.

Ported 1:1 from ``superset_old/db_engine_specs/trino.py`` with the legacy
WSGI-stack imports removed.  Only overridden methods and attributes are
included.

The Presto base class is inlined here because the liteset codebase does
not need a standalone ``presto.py`` -- Trino is the only Presto-family
engine we support.
"""

from __future__ import annotations

import contextlib
import logging
import re
from abc import ABCMeta
from datetime import datetime
from re import Pattern
from typing import Any, TYPE_CHECKING
from urllib import parse

from sqlalchemy import Column, types
from sqlalchemy.engine import Engine
from sqlalchemy.engine.reflection import Inspector
from sqlalchemy.engine.url import URL
from sqlalchemy.exc import NoSuchTableError

from superset.constants import TimeGrain
from superset.db_engine_specs.base import (
    BaseEngineSpec,
    ColumnTypeMapping,
    convert_inspector_columns,
    ResultSetColumnType,
)
from superset.typing import GenericDataType
from superset.utils import json as json_utils

if TYPE_CHECKING:
    from superset.models.core import Database
    from superset.models.sql_lab import Query
    from superset.sql.parse import Table

    with contextlib.suppress(ImportError):  # trino may not be installed
        from trino.dbapi import Cursor

logger = logging.getLogger(__name__)

# OAuth2: ``trino`` is an optional dependency.  Mirror the original
# ``CustomTrinoAuthErrorMeta`` pattern from
# ``superset_old/db_engine_specs/trino.py`` — match Trino HttpError 401
# responses and treat them as OAuth2 redirect triggers.
try:  # pragma: no cover -- optional dep
    from trino.exceptions import (
        HttpError as _TrinoHttpError,  # type: ignore[import-not-found]
    )
except ImportError:  # pragma: no cover
    _TrinoHttpError = Exception  # type: ignore[assignment,misc]


class _TrinoAuthErrorMeta(type):
    """Metaclass that flags HTTP-401 errors from the Trino driver."""

    def __instancecheck__(cls, instance: object) -> bool:
        return isinstance(instance, _TrinoHttpError) and "error 401" in str(instance)


class TrinoAuthError(_TrinoHttpError, metaclass=_TrinoAuthErrorMeta):  # type: ignore[misc]
    """Sentinel exception class for Trino OAuth2 401 detection."""


# ---------------------------------------------------------------------------
# Custom Presto/Trino SQL types — 1:1 with upstream
# ``superset_old/models/sql_types/presto_sql_types.py`` (the module itself is
# not ported; the types live here, next to the Presto base spec that uses
# them).
# ---------------------------------------------------------------------------


class TinyInteger(types.Integer):
    """Presto ``tinyint`` type."""

    @property
    def python_type(self) -> type[int]:
        return int

    @classmethod
    def _compiler_dispatch(cls, _visitor: Any, **_kw: Any) -> str:
        return "TINYINT"


class Interval(types.TypeEngine):
    """Presto ``interval`` type."""

    @property
    def python_type(self) -> type[Any] | None:
        return None

    @classmethod
    def _compiler_dispatch(cls, _visitor: Any, **_kw: Any) -> str:
        return "INTERVAL"


class Array(types.TypeEngine):
    """Presto ``array`` type."""

    @property
    def python_type(self) -> type[list[Any]] | None:
        return list

    @classmethod
    def _compiler_dispatch(cls, _visitor: Any, **_kw: Any) -> str:
        return "ARRAY"


class Map(types.TypeEngine):
    """Presto ``map`` type."""

    @property
    def python_type(self) -> type[dict[Any, Any]] | None:
        return dict

    @classmethod
    def _compiler_dispatch(cls, _visitor: Any, **_kw: Any) -> str:
        return "MAP"


class Row(types.TypeEngine):
    """Presto ``row`` type."""

    @property
    def python_type(self) -> type[Any] | None:
        return None

    @classmethod
    def _compiler_dispatch(cls, _visitor: Any, **_kw: Any) -> str:
        return "ROW"


class TimeStamp(types.TypeDecorator):
    """Inline-renders TIMESTAMP literals — Presto/Trino can't auto-cast.

    Overrides ``literal_processor`` directly so the value renders verbatim as
    ``TIMESTAMP '...'``. ``process_literal_param`` would only transform the
    value and then hand it to the ``TIMESTAMP`` impl's own literal processor,
    which rejects the formatted string. Partition predicates are compiled with
    ``literal_binds=True`` (see ``where_latest_partition``).
    """

    impl = types.TIMESTAMP
    cache_ok = True

    def literal_processor(self, dialect: Any) -> Any:
        def process(value: str) -> str:
            return f"TIMESTAMP '{value}'"

        return process


class Date(types.TypeDecorator):
    """Inline-renders DATE literals — Presto/Trino can't auto-cast.

    Overrides ``literal_processor`` directly; see ``TimeStamp`` above.
    """

    impl = types.DATE
    cache_ok = True

    def literal_processor(self, dialect: Any) -> Any:
        def process(value: str) -> str:
            return f"DATE '{value}'"

        return process


# ---------------------------------------------------------------------------
# Error regexes
# ---------------------------------------------------------------------------

COLUMN_DOES_NOT_EXIST_REGEX = re.compile(
    "line (?P<location>.+?): .*Column '(?P<column_name>.+?)' cannot be resolved"
)
TABLE_DOES_NOT_EXIST_REGEX = re.compile(".*Table (?P<table_name>.+?) does not exist")
SCHEMA_DOES_NOT_EXIST_REGEX = re.compile(
    "line (?P<location>.+?): .*Schema '(?P<schema_name>.+?)' does not exist"
)
CONNECTION_ACCESS_DENIED_REGEX = re.compile("Access Denied: Invalid credentials")
CONNECTION_INVALID_HOSTNAME_REGEX = re.compile(
    r"Failed to establish a new connection: \[Errno 8\] nodename nor servname "
    "provided, or not known"
)
CONNECTION_HOST_DOWN_REGEX = re.compile(
    r"Failed to establish a new connection: \[Errno 60\] Operation timed out"
)
CONNECTION_PORT_CLOSED_REGEX = re.compile(
    r"Failed to establish a new connection: \[Errno 61\] Connection refused"
)
CONNECTION_UNKNOWN_DATABASE_ERROR = re.compile(
    r"line (?P<location>.+?): Catalog '(?P<catalog_name>.+?)' does not exist"
)


# ---------------------------------------------------------------------------
# PrestoBaseEngineSpec -- inlined Presto base (shared with Trino)
# ---------------------------------------------------------------------------


class PrestoBaseEngineSpec(BaseEngineSpec, metaclass=ABCMeta):
    """Abstract base class that shares common functions between Presto and Trino."""

    supports_dynamic_schema = True
    supports_catalog = True
    supports_dynamic_catalog = True
    supports_cross_catalog_queries = True

    column_type_mappings: tuple[ColumnTypeMapping, ...] = (
        (
            re.compile(r"^boolean.*", re.IGNORECASE),
            types.BOOLEAN(),
            GenericDataType.BOOLEAN,
        ),
        (
            re.compile(r"^tinyint.*", re.IGNORECASE),
            TinyInteger(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^smallint.*", re.IGNORECASE),
            types.SmallInteger(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^integer.*", re.IGNORECASE),
            types.INTEGER(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^bigint.*", re.IGNORECASE),
            types.BigInteger(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^real.*", re.IGNORECASE),
            types.FLOAT(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^double.*", re.IGNORECASE),
            types.FLOAT(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^decimal.*", re.IGNORECASE),
            types.DECIMAL(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^varchar(\((\d+)\))*$", re.IGNORECASE),
            lambda match: types.VARCHAR(int(match[2])) if match[2] else types.String(),
            GenericDataType.STRING,
        ),
        (
            re.compile(r"^char(\((\d+)\))*$", re.IGNORECASE),
            lambda match: types.CHAR(int(match[2])) if match[2] else types.String(),
            GenericDataType.STRING,
        ),
        (
            re.compile(r"^varbinary.*", re.IGNORECASE),
            types.VARBINARY(),
            GenericDataType.STRING,
        ),
        (
            re.compile(r"^json.*", re.IGNORECASE),
            types.JSON(),
            GenericDataType.STRING,
        ),
        (
            re.compile(r"^date.*", re.IGNORECASE),
            types.Date(),
            GenericDataType.TEMPORAL,
        ),
        (
            re.compile(r"^timestamp.*", re.IGNORECASE),
            types.TIMESTAMP(),
            GenericDataType.TEMPORAL,
        ),
        (
            re.compile(r"^interval.*", re.IGNORECASE),
            Interval(),
            GenericDataType.TEMPORAL,
        ),
        (
            re.compile(r"^time.*", re.IGNORECASE),
            types.Time(),
            GenericDataType.TEMPORAL,
        ),
        (re.compile(r"^array.*", re.IGNORECASE), Array(), GenericDataType.STRING),
        (re.compile(r"^map.*", re.IGNORECASE), Map(), GenericDataType.STRING),
        (re.compile(r"^row.*", re.IGNORECASE), Row(), GenericDataType.STRING),
    )

    # pylint: disable=line-too-long
    _time_grain_expressions = {
        None: "{col}",
        TimeGrain.SECOND: "date_trunc('second', CAST({col} AS TIMESTAMP))",
        TimeGrain.FIVE_SECONDS: "date_trunc('second', CAST({col} AS TIMESTAMP)) - interval '1' second * (second(CAST({col} AS TIMESTAMP)) % 5)",  # noqa: E501
        TimeGrain.THIRTY_SECONDS: "date_trunc('second', CAST({col} AS TIMESTAMP)) - interval '1' second * (second(CAST({col} AS TIMESTAMP)) % 30)",  # noqa: E501
        TimeGrain.MINUTE: "date_trunc('minute', CAST({col} AS TIMESTAMP))",
        TimeGrain.FIVE_MINUTES: "date_trunc('minute', CAST({col} AS TIMESTAMP)) - interval '1' minute * (minute(CAST({col} AS TIMESTAMP)) % 5)",  # noqa: E501
        TimeGrain.TEN_MINUTES: "date_trunc('minute', CAST({col} AS TIMESTAMP)) - interval '1' minute * (minute(CAST({col} AS TIMESTAMP)) % 10)",  # noqa: E501
        TimeGrain.FIFTEEN_MINUTES: "date_trunc('minute', CAST({col} AS TIMESTAMP)) - interval '1' minute * (minute(CAST({col} AS TIMESTAMP)) % 15)",  # noqa: E501
        TimeGrain.HALF_HOUR: "date_trunc('minute', CAST({col} AS TIMESTAMP)) - interval '1' minute * (minute(CAST({col} AS TIMESTAMP)) % 30)",  # noqa: E501
        TimeGrain.HOUR: "date_trunc('hour', CAST({col} AS TIMESTAMP))",
        TimeGrain.SIX_HOURS: "date_trunc('hour', CAST({col} AS TIMESTAMP)) - interval '1' hour * (hour(CAST({col} AS TIMESTAMP)) % 6)",  # noqa: E501
        TimeGrain.DAY: "date_trunc('day', CAST({col} AS TIMESTAMP))",
        TimeGrain.WEEK: "date_trunc('week', CAST({col} AS TIMESTAMP))",
        TimeGrain.MONTH: "date_trunc('month', CAST({col} AS TIMESTAMP))",
        TimeGrain.QUARTER: "date_trunc('quarter', CAST({col} AS TIMESTAMP))",
        TimeGrain.YEAR: "date_trunc('year', CAST({col} AS TIMESTAMP))",
        TimeGrain.WEEK_STARTING_SUNDAY: "date_trunc('week', CAST({col} AS TIMESTAMP) + interval '1' day) - interval '1' day",  # noqa: E501
        TimeGrain.WEEK_STARTING_MONDAY: "date_trunc('week', CAST({col} AS TIMESTAMP))",
        TimeGrain.WEEK_ENDING_SATURDAY: "date_trunc('week', CAST({col} AS TIMESTAMP) + interval '1' day) + interval '5' day",  # noqa: E501
        TimeGrain.WEEK_ENDING_SUNDAY: "date_trunc('week', CAST({col} AS TIMESTAMP)) + interval '6' day",  # noqa: E501
    }

    custom_errors: dict[Pattern[str], tuple[str, str, dict[str, Any]]] = {
        COLUMN_DOES_NOT_EXIST_REGEX: (
            'We can\'t seem to resolve column "%(column_name)s" at line %(location)s.',
            "COLUMN_DOES_NOT_EXIST_ERROR",
            {},
        ),
        TABLE_DOES_NOT_EXIST_REGEX: (
            'The table "%(table_name)s" does not exist. A valid table must be '
            "used to run this query.",
            "TABLE_DOES_NOT_EXIST_ERROR",
            {},
        ),
        SCHEMA_DOES_NOT_EXIST_REGEX: (
            'The schema "%(schema_name)s" does not exist. A valid schema must '
            "be used to run this query.",
            "SCHEMA_DOES_NOT_EXIST_ERROR",
            {},
        ),
        CONNECTION_ACCESS_DENIED_REGEX: (
            "Either the username or the password is incorrect.",
            "CONNECTION_ACCESS_DENIED_ERROR",
            {"invalid": ["username", "password"]},
        ),
        CONNECTION_INVALID_HOSTNAME_REGEX: (
            "The hostname provided can't be resolved.",
            "CONNECTION_INVALID_HOSTNAME_ERROR",
            {"invalid": ["host"]},
        ),
        CONNECTION_HOST_DOWN_REGEX: (
            "The host might be down and can't be reached.",
            "CONNECTION_HOST_DOWN_ERROR",
            {"invalid": ["host", "port"]},
        ),
        CONNECTION_PORT_CLOSED_REGEX: (
            "The port is closed. A valid port number is needed to connect.",
            "CONNECTION_PORT_CLOSED_ERROR",
            {"invalid": ["port"]},
        ),
        CONNECTION_UNKNOWN_DATABASE_ERROR: (
            'Unable to connect to catalog "%(catalog_name)s".',
            "CONNECTION_UNKNOWN_DATABASE_ERROR",
            {"invalid": ["database"]},
        ),
    }

    @classmethod
    def convert_dttm(
        cls,
        target_type: str,
        dttm: datetime,
        db_extra: dict[str, Any] | None = None,
    ) -> str | None:
        """Convert a Python ``datetime`` object to a SQL expression.

        Superset only defines time zone naive ``datetime`` objects, though this
        method handles both time zone naive and aware conversions.
        """
        sqla_type = cls.get_sqla_column_type(target_type)

        if isinstance(sqla_type, types.Date):
            return f"DATE '{dttm.date().isoformat()}'"
        if isinstance(sqla_type, types.TIMESTAMP):
            return f"""TIMESTAMP '{dttm.isoformat(timespec="microseconds", sep=" ")}'"""
        return None

    @classmethod
    def epoch_to_dttm(cls) -> str:
        return "from_unixtime({col})"

    @classmethod
    def get_default_catalog(cls, database: Database) -> str | None:
        """Return the default catalog."""
        if database.url_object.database is None:
            return None
        return database.url_object.database.split("/")[0]

    @classmethod
    def get_catalog_names(
        cls,
        database: Database,
        inspector: Inspector,
    ) -> set[str]:
        """Get all catalogs.

        1:1 with upstream ``PrestoBaseEngineSpec.get_catalog_names``
        (``SHOW CATALOGS``), adapted for SQLAlchemy 2.0 — upstream's
        ``inspector.bind.execute("SHOW CATALOGS")`` relied on the removed
        SA 1.x ``Engine.execute(str)`` shim, so open a real connection and
        wrap the raw SQL in ``text()``.
        """
        from sqlalchemy import text

        bind = inspector.bind
        if isinstance(bind, Engine):
            with bind.connect() as conn:
                return {catalog for (catalog,) in conn.execute(text("SHOW CATALOGS"))}
        return {catalog for (catalog,) in bind.execute(text("SHOW CATALOGS"))}

    @classmethod
    def adjust_engine_params(
        cls,
        uri: URL,
        connect_args: dict[str, Any],
        catalog: str | None = None,
        schema: str | None = None,
    ) -> tuple[URL, dict[str, Any]]:
        if uri.database and "/" in uri.database:
            current_catalog, current_schema = uri.database.split("/", 1)
        else:
            current_catalog, current_schema = uri.database, None

        if schema:
            schema = parse.quote(schema, safe="")

        adjusted_database = "/".join(
            [
                catalog or current_catalog or "",
                schema or current_schema or "",
            ]
        ).rstrip("/")

        uri = uri.set(database=adjusted_database)
        return uri, connect_args

    @classmethod
    def get_schema_from_engine_params(
        cls,
        sqlalchemy_uri: URL,
        connect_args: dict[str, Any],
    ) -> str | None:
        """Return the configured schema.

        In Presto/Trino the schema is the second part of ``catalog/schema``.
        """
        database = sqlalchemy_uri.database
        if database and "/" in database:
            return parse.unquote(database.split("/")[1])
        return None

    @classmethod
    def where_latest_partition(
        cls,
        database: Database,
        table: Table,
        query: Any,
        columns: list[ResultSetColumnType] | None = None,
    ) -> Any | None:
        """Add a WHERE clause referencing only the most recent partition.

        1:1 with upstream ``PrestoBaseEngineSpec.where_latest_partition`` —
        defined on the base so both Presto AND Trino inherit it.
        ``cls.latest_partition`` resolves to the concrete spec's
        implementation.
        """
        try:
            col_names, values = cls.latest_partition(database, table, show_first=True)
        except Exception:  # noqa: BLE001
            # table is not partitioned
            return None

        if values is None:
            return None

        column_type_by_name = {
            column.get("column_name"): column.get("type") for column in columns or []
        }

        for col_name, value in zip(col_names, values, strict=False):
            col_type = column_type_by_name.get(col_name)

            if isinstance(col_type, str):
                col_type_class = getattr(types, col_type, None)
                col_type = col_type_class() if col_type_class else None

            if isinstance(col_type, types.DATE):
                col_type = Date()
            elif isinstance(col_type, types.TIMESTAMP):
                col_type = TimeStamp()

            query = query.where(Column(col_name, col_type) == value)

        return query

    @classmethod
    def estimate_statement_cost(
        cls, database: Database, statement: str, cursor: Any
    ) -> dict[str, Any]:
        """Run a SQL query that estimates the cost of a given statement.

        1:1 with upstream ``PrestoBaseEngineSpec.estimate_statement_cost``
        (``EXPLAIN (TYPE IO, FORMAT JSON)``).
        """
        sql = f"EXPLAIN (TYPE IO, FORMAT JSON) {statement}"
        cursor.execute(sql)

        # the output is a single column and a single row containing JSON
        return json_utils.loads(cursor.fetchone()[0])

    @classmethod
    def query_cost_formatter(
        cls, raw_cost: list[dict[str, Any]]
    ) -> list[dict[str, str]]:
        """Format cost estimate (1:1 upstream ``PrestoBaseEngineSpec``)."""

        def humanize(value: Any, suffix: str) -> str:
            try:
                value = int(value)
            except ValueError:
                return str(value)

            prefixes = ["K", "M", "G", "T", "P", "E", "Z", "Y"]
            prefix = ""
            to_next_prefix = 1000
            while value > to_next_prefix and prefixes:
                prefix = prefixes.pop(0)
                value //= to_next_prefix

            return f"{value} {prefix}{suffix}"

        cost = []
        columns = [
            ("outputRowCount", "Output count", " rows"),
            ("outputSizeInBytes", "Output size", "B"),
            ("cpuCost", "CPU cost", ""),
            ("maxMemory", "Max memory", "B"),
            ("networkCost", "Network cost", ""),
        ]
        for row in raw_cost:
            estimate: dict[str, float] = row.get("estimate", {})
            statement_cost = {}
            for key, label, suffix in columns:
                if key in estimate:
                    statement_cost[label] = humanize(estimate[key], suffix).strip()
            cost.append(statement_cost)

        return cost

    @classmethod
    def get_function_names(cls, database: Database) -> list[str]:
        """Get a list of function names callable on the database.

        Used for SQL Lab autocomplete (upstream
        ``PrestoBaseEngineSpec.get_function_names``: ``SHOW FUNCTIONS``).
        Results are cached per-database — the business equivalent of
        upstream's ``@cache_manager.data_cache.memoize()``; the port's cache
        layer exposes no upstream ``memoize``, so this uses the sync
        cache slot directly with a manual get/set.
        """
        from superset.extensions import cache_manager

        cache_key = f"db:{getattr(database, 'id', None)}:function_names"
        try:
            cached = cache_manager.sync_cache.get(cache_key)
            if cached is not None:
                return cached
        except Exception:  # noqa: BLE001
            logger.debug("function-name cache read failed", exc_info=True)

        names = database.get_df("SHOW FUNCTIONS")["Function"].tolist()

        try:
            cache_manager.sync_cache.set(cache_key, names)
        except Exception:  # noqa: BLE001
            logger.debug("function-name cache write failed", exc_info=True)
        return names


# ---------------------------------------------------------------------------
# TrinoEngineSpec
# ---------------------------------------------------------------------------


class TrinoEngineSpec(PrestoBaseEngineSpec):
    engine = "trino"
    engine_name = "Trino"
    allows_alias_to_source_column = False

    # OAuth 2.0 — 1:1 with superset_old/db_engine_specs/trino.py
    supports_oauth2 = True
    oauth2_exception = TrinoAuthError
    oauth2_token_request_type = "data"  # noqa: S105

    @classmethod
    def impersonate_user(
        cls,
        database: Database,
        username: str | None,
        user_token: str | None,
        url: URL,
        engine_kwargs: dict[str, Any],
    ) -> tuple[URL, dict[str, Any]]:
        """Impersonate ``username`` on a Trino connection (1:1 upstream).

        Sets ``connect_args["user"]`` (Trino runs the query as that user) and,
        when an OAuth2 ``user_token`` is supplied, an HTTP session carrying the
        Bearer token. ``requests`` is imported lazily so the no-token path
        (the common case) has no hard dependency.
        """
        if username is None:
            return url, engine_kwargs

        backend_name = url.get_backend_name()
        connect_args = engine_kwargs.setdefault("connect_args", {})
        if backend_name == "trino":
            connect_args["user"] = username
            if user_token is not None:
                import requests  # noqa: PLC0415

                http_session = requests.Session()
                http_session.headers.update({"Authorization": f"Bearer {user_token}"})
                connect_args["http_session"] = http_session

        return url, engine_kwargs

    @classmethod
    def get_allow_cost_estimate(cls, extra: dict[str, Any]) -> bool:
        return True

    @classmethod
    def get_tracking_url(cls, cursor: Cursor) -> str | None:
        try:
            return cursor.info_uri
        except AttributeError:
            with contextlib.suppress(AttributeError):
                conn = cursor.connection
                return (
                    f"{conn.http_scheme}://{conn.host}:{conn.port}"
                    f"/ui/query.html?{cursor._query.query_id}"  # noqa: SLF001
                )
        return None

    @classmethod
    def handle_cursor(cls, cursor: Cursor, query: Query) -> None:
        """Handle a trino client cursor.

        WARNING: if you execute a query, it will block until complete and you
        will not be able to handle the cursor until complete. Use
        ``execute_with_cursor`` instead, to handle this asynchronously.

        1:1 with ``superset_old/db_engine_specs/trino.py`` adapted to the
        port's session model: upstream commits the global ``db.session``;
        here the ``query`` ORM object carries its own (sync) session, resolved
        via ``object_session``.  ``tracking_url`` is a read-only property in the
        port (the column is ``tracking_url_raw``), so write the raw column.
        """
        from sqlalchemy.orm import object_session

        from superset.constants import QUERY_CANCEL_KEY, QUERY_EARLY_CANCEL_KEY

        # Adds the executed query id to the extra payload so the query can be
        # cancelled by a concurrent stop request.
        cancel_query_id = cursor.query_id
        logger.debug("Query %s: queryId %s found in cursor", query.id, cancel_query_id)
        query.set_extra_json_key(key=QUERY_CANCEL_KEY, value=cancel_query_id)

        if tracking_url := cls.get_tracking_url(cursor):
            query.tracking_url_raw = tracking_url

        session = object_session(query)
        if session is not None:
            session.commit()

        # If query cancellation was requested prior to the handle_cursor call but
        # the query was still executed, trigger the actual cancellation now.
        if query.extra.get(QUERY_EARLY_CANCEL_KEY):
            cls.cancel_query(
                cursor=cursor,
                query=query,
                cancel_query_id=cancel_query_id,
            )

        super().handle_cursor(cursor=cursor, query=query)

    @classmethod
    def execute_with_cursor(
        cls,
        cursor: Cursor,
        sql: str,
        query: Query,
    ) -> None:
        """Trigger execution of a query and handle the resulting cursor.

        Trino's client blocks until the query is complete, so we run it in
        another thread and poll ``handle_cursor`` for the query ID to appear on
        the cursor in parallel — that ID is what lets a concurrent stop request
        cancel the running query.

        1:1 with ``superset_old/db_engine_specs/trino.py`` minus the upstream
        ``@copy_current_request_context`` wrapper: the port's Celery worker
        runs tasks without a request/current-user context (no ``ContextTask``),
        and the sync engine spec's ``execute`` needs none, so a plain thread
        suffices.
        """
        import threading
        import time

        query_id = query.id
        query_database = query.database

        execute_result: dict[str, Any] = {}
        execute_event = threading.Event()

        def _execute(
            results: dict[str, Any],
            event: threading.Event,
        ) -> None:
            logger.debug("Query %s: Running query: %s", query_id, sql)
            try:
                cls.execute(cursor, sql, query_database)
            except Exception as ex:  # noqa: BLE001  # pylint: disable=broad-except
                results["error"] = ex
            finally:
                event.set()

        execute_thread = threading.Thread(
            target=_execute,
            args=(execute_result, execute_event),
        )
        execute_thread.start()

        # Wait for the thread to start before continuing.
        time.sleep(0.1)
        # Wait for a query ID to be available before handling the cursor, as
        # it's required by that method; it may never become available on error.
        while not cursor.query_id and not execute_event.is_set():
            time.sleep(0.1)

        logger.debug("Query %s: Handling cursor", query_id)
        cls.handle_cursor(cursor, query)

        # Block until the query completes; same behaviour as the client itself.
        logger.debug("Query %s: Waiting for query to complete", query_id)
        execute_event.wait()

        # Re-raise the original execution error (thread mangles the traceback,
        # but throwing the original allows normal DB-error mapping downstream).
        if err := execute_result.get("error"):
            raise err

    @classmethod
    def prepare_cancel_query(cls, query: Query) -> None:
        """Flag an early cancellation when no query ID has been captured yet.

        1:1 with upstream, committing via the query's own (sync) session
        instead of the global ``db.session``.
        """
        from sqlalchemy.orm import object_session

        from superset.constants import QUERY_CANCEL_KEY, QUERY_EARLY_CANCEL_KEY

        if QUERY_CANCEL_KEY not in query.extra:
            query.set_extra_json_key(QUERY_EARLY_CANCEL_KEY, True)
            session = object_session(query)
            if session is not None:
                session.commit()

    @classmethod
    def cancel_query(cls, cursor: Cursor, query: Query, cancel_query_id: str) -> bool:
        """Cancel query in the underlying database.

        :param cursor: New cursor instance to the db of the query
        :param query: Query instance
        :param cancel_query_id: Trino ``queryId``
        :return: True if query cancelled successfully, False otherwise
        """
        try:
            cursor.execute(
                f"CALL system.runtime.kill_query(query_id => '{cancel_query_id}',"
                "message => 'Query cancelled by Superset')"
            )
            cursor.fetchall()  # needed to trigger the call
        except Exception:  # noqa: BLE001
            return False

        return True

    @staticmethod
    def get_extra_params(database: Database, source: Any = None) -> dict[str, Any]:
        """Add elements to connection parameters (e.g. certificates).

        :param database: database instance from which to extract extras
        :param source: in which context is the connection needed
        :raises CertificateException: If certificate is not valid/unparseable

        1:1 with ``superset_old/db_engine_specs/trino.py`` adapted: the
        upstream ``app.config`` is gone; ``get_user_agent`` reads from
        SupersetSettings.
        """
        from superset.utils.core import get_user_agent

        extra: dict[str, Any] = BaseEngineSpec.get_extra_params(database, source)
        engine_params: dict[str, Any] = extra.setdefault("engine_params", {})
        connect_args: dict[str, Any] = engine_params.setdefault("connect_args", {})
        user_agent = get_user_agent(database, source)

        connect_args.setdefault("source", user_agent)

        if database.server_cert:
            from superset.utils.core import create_ssl_cert_file

            connect_args["http_scheme"] = "https"
            connect_args["verify"] = create_ssl_cert_file(database.server_cert)

        return extra

    @staticmethod
    def update_params_from_encrypted_extra(
        database: Database,
        params: dict[str, Any],
    ) -> None:
        if not database.encrypted_extra:
            return
        try:
            encrypted_extra = json_utils.loads(database.encrypted_extra)
            auth_method = encrypted_extra.pop("auth_method", None)
            auth_params = encrypted_extra.pop("auth_params", {})
            if not auth_method:
                return

            connect_args = params.setdefault("connect_args", {})
            connect_args["http_scheme"] = "https"

            if auth_method == "basic":
                from trino.auth import BasicAuthentication as trino_auth  # noqa: N813
            elif auth_method == "kerberos":
                from trino.auth import (
                    KerberosAuthentication as trino_auth,  # noqa: N813
                )
            elif auth_method == "certificate":
                from trino.auth import (
                    CertificateAuthentication as trino_auth,  # noqa: N813
                )
            elif auth_method == "jwt":
                from trino.auth import JWTAuthentication as trino_auth  # noqa: N813
            else:
                # Custom auth: consult ALLOWED_EXTRA_AUTHENTICATIONS config
                # (1:1 with superset_old: app.config["ALLOWED_EXTRA_AUTHENTICATIONS"]
                # → SupersetSettings().allowed_extra_authentications).
                from superset.config import SupersetSettings

                _settings = SupersetSettings()  # type: ignore[call-arg]
                allowed_extra_auths: dict[str, Any] = (
                    _settings.allowed_extra_authentications.get("trino", {})
                )
                if auth_method in allowed_extra_auths:
                    trino_auth = allowed_extra_auths.get(auth_method)  # noqa: N813
                else:
                    raise ValueError(
                        f"For security reason, custom authentication '{auth_method}' "
                        f"must be listed in 'ALLOWED_EXTRA_AUTHENTICATIONS' config"
                    )

            connect_args["auth"] = trino_auth(**auth_params)
        except json_utils.JSONDecodeError as ex:
            logger.error(ex, exc_info=True)
            raise

    @classmethod
    def get_extra_table_metadata(
        cls,
        database: Database,
        table: Table,
    ) -> dict[str, Any]:
        """Return partition + view metadata for a Trino table.

        1:1 with ``superset_old/db_engine_specs/trino.py`` adapted to the
        port's sync helpers: ``Database.get_indexes`` / ``Database.has_view``
        aren't ported on the async Database model, so the partition indexes
        and the view check are resolved inline through a single synchronous
        inspector instead.
        """
        metadata: dict[str, Any] = {}

        with database.get_inspector(
            catalog=table.catalog,
            schema=table.schema,
        ) as inspector:
            try:
                indexes = cls.get_indexes(database, inspector, table)
            except Exception:  # noqa: BLE001
                indexes = []

            if indexes:
                col_names, latest_parts = cls.latest_partition(
                    database,
                    table,
                    show_first=True,
                    indexes=indexes,
                )
                if not latest_parts:
                    latest_parts = tuple([None] * len(col_names))
                metadata["partitions"] = {
                    "cols": sorted(
                        {
                            column_name
                            for index in indexes
                            if index.get("name") == "partition"
                            for column_name in index.get("column_names", [])
                        }
                    ),
                    "latest": dict(zip(col_names, latest_parts, strict=False)),
                    "partitionQuery": cls._partition_query(
                        table=table,
                        indexes=indexes,
                        database=database,
                    ),
                }

            # ``database.has_view`` isn't ported — detect via the inspector.
            if table.table in set(inspector.get_view_names(schema=table.schema)):
                metadata["view"] = inspector.get_view_definition(
                    table.table,
                    table.schema,
                )

        return metadata

    @classmethod
    def _partition_query(
        cls,
        table: Table,
        indexes: list[dict[str, Any]],
        database: Database,
        limit: int = 0,
        order_by: list[tuple[str, bool]] | None = None,
        filters: dict[Any, Any] | None = None,
    ) -> str:
        """Return a partition query (1:1 upstream ``PrestoBaseEngineSpec``)."""
        from textwrap import dedent

        from packaging.version import Version

        limit_clause = f"LIMIT {limit}" if limit else ""
        order_by_clause = ""
        if order_by:
            l = []  # noqa: E741
            for field, desc in order_by:
                l.append(field + " DESC" if desc else "")
            order_by_clause = "ORDER BY " + ", ".join(l)

        where_clause = ""
        if filters:
            l = []  # noqa: E741
            for field, value in filters.items():
                l.append(f"{field} = '{value}'")
            where_clause = "WHERE " + " AND ".join(l)

        # Partition select syntax changed in v0.199, so check here.
        presto_version = database.get_extra().get("version")
        if presto_version and Version(presto_version) < Version("0.199"):
            full_table_name = (
                f"{table.schema}.{table.table}" if table.schema else table.table
            )
            partition_select_clause = f"SHOW PARTITIONS FROM {full_table_name}"
        else:
            system_table_name = f'"{table.table}$partitions"'
            full_table_name = (
                f"{table.schema}.{system_table_name}"
                if table.schema
                else system_table_name
            )
            partition_select_clause = f"SELECT * FROM {full_table_name}"  # noqa: S608

        sql = dedent(
            f"""\
            {partition_select_clause}
            {where_clause}
            {order_by_clause}
            {limit_clause}
        """
        )
        return sql

    @classmethod
    def _latest_partition_from_df(cls, df: Any) -> list[str] | None:
        if not df.empty:
            return df.to_records(index=False)[0].item()
        return None

    @classmethod
    def latest_partition(
        cls,
        database: Database,
        table: Table,
        show_first: bool = False,
        indexes: list[dict[str, Any]] | None = None,
    ) -> tuple[list[str], list[str] | None]:
        """Return col names + the latest (max) partition value for a table.

        1:1 with upstream ``PrestoBaseEngineSpec.latest_partition``.  The
        upstream ``@cache_manager.data_cache.memoize(timeout=60)`` decorator
        is intentionally dropped (perf-only; the port wires no engine-spec
        data-cache memoization — same call as the ClickHouse ``get_function_names``
        decorator).
        """
        from superset.exceptions import SupersetTemplateException

        if indexes is None:
            with database.get_inspector(
                catalog=table.catalog,
                schema=table.schema,
            ) as inspector:
                indexes = cls.get_indexes(database, inspector, table)

        if not indexes:
            raise SupersetTemplateException(
                f"Error getting partition for {table}. "
                "Verify that this table has a partition."
            )

        if len(indexes[0]["column_names"]) < 1:
            raise SupersetTemplateException(
                "The table should have one partitioned field"
            )

        if not show_first and len(indexes[0]["column_names"]) > 1:
            raise SupersetTemplateException(
                "The table should have a single partitioned field "
                "to use this function. You may want to use "
                "`presto.latest_sub_partition`"
            )

        column_names = indexes[0]["column_names"]

        return column_names, cls._latest_partition_from_df(
            df=database.get_df(
                sql=cls._partition_query(
                    table,
                    indexes,
                    database,
                    limit=1,
                    order_by=[(column_name, True) for column_name in column_names],
                ),
                catalog=table.catalog,
                schema=table.schema,
            )
        )

    @classmethod
    def latest_sub_partition(
        cls,
        database: Database,
        table: Table,
        **kwargs: Any,
    ) -> Any:
        """Return the latest (max) partition value for a table.

        A filtering criteria should be passed for all fields that are
        partitioned except for the field to be returned.  For example,
        if a table is partitioned by (``ds``, ``event_type`` and
        ``event_category``) and you want the latest ``ds``, you'll want
        to provide a filter as keyword arguments for both
        ``event_type`` and ``event_category`` as in
        ``latest_sub_partition('my_table',
            event_category='page', event_type='click')``

        :param database: database query will be run against
        :param table: the table instance
        :param kwargs: keyword arguments define the filtering criteria
            on the partition list. There can be many of these.

        1:1 with upstream ``PrestoBaseEngineSpec.latest_sub_partition``
        (``superset_old/db_engine_specs/presto.py``), adapted to the
        port's inspector model (no ``database.get_indexes`` shortcut).
        """
        from superset.exceptions import SupersetTemplateException

        with database.get_inspector(
            catalog=table.catalog,
            schema=table.schema,
        ) as inspector:
            indexes = cls.get_indexes(database, inspector, table)

        part_fields = indexes[0]["column_names"]
        for k in kwargs.keys():  # pylint: disable=consider-iterating-dictionary
            if k not in k in part_fields:  # pylint: disable=comparison-with-itself
                msg = f"Field [{k}] is not part of the portioning key"
                raise SupersetTemplateException(msg)
        if len(kwargs.keys()) != len(part_fields) - 1:
            msg = (
                "A filter needs to be specified for {} out of the {} fields."
            ).format(len(part_fields) - 1, len(part_fields))
            raise SupersetTemplateException(msg)

        for field in part_fields:
            if field not in kwargs:
                field_to_return = field

        sql = cls._partition_query(
            table,
            indexes,
            database,
            limit=1,
            order_by=[(field_to_return, True)],
            filters=kwargs,
        )
        df = database.get_df(sql, table.catalog, table.schema)
        if df.empty:
            return ""
        return df.to_dict()[field_to_return][0]

    @classmethod
    def get_dbapi_exception_mapping(cls) -> dict[type[Exception], type[Exception]]:
        # pylint: disable=import-outside-toplevel
        from requests import exceptions as requests_exceptions
        from trino import exceptions as trino_exceptions

        from superset.db_engine_specs.exceptions import (
            SupersetDBAPIConnectionError,
            SupersetDBAPIDatabaseError,
            SupersetDBAPIOperationalError,
            SupersetDBAPIProgrammingError,
        )

        static_mapping: dict[type[Exception], type[Exception]] = {
            requests_exceptions.ConnectionError: SupersetDBAPIConnectionError,
        }

        class _CustomMapping(dict[type[Exception], type[Exception]]):
            def get(  # type: ignore[override]
                self, item: type[Exception], default: type[Exception] | None = None
            ) -> type[Exception] | None:
                if static := static_mapping.get(item):
                    return static
                if issubclass(item, trino_exceptions.InternalError):
                    return SupersetDBAPIDatabaseError
                if issubclass(item, trino_exceptions.OperationalError):
                    return SupersetDBAPIOperationalError
                if issubclass(item, trino_exceptions.ProgrammingError):
                    return SupersetDBAPIProgrammingError
                return default

        return _CustomMapping()

    @classmethod
    def _expand_columns(cls, col: ResultSetColumnType) -> list[ResultSetColumnType]:
        """Expand the given column out to one or more columns by analysing their
        types, descending into ROWs and expanding out their inner fields
        recursively.

        We can only navigate named fields in ROWs in this way, so we can't
        expand out MAP or ARRAY types, nor fields in ROWs which have no name.
        Expanded columns are named ``foo.bar.baz`` and we provide a
        ``query_as`` property to instruct the base engine spec how to
        correctly query them.
        """
        from trino.sqlalchemy import datatype  # noqa: I001

        cols: list[ResultSetColumnType] = [col]
        col_type = col.get("type")

        if not isinstance(col_type, datatype.ROW):
            return cols

        for inner_name, inner_type in col_type.attr_types:
            outer_name = col["name"]
            name = ".".join([outer_name, inner_name])
            query_name = ".".join([f'"{piece}"' for piece in name.split(".")])
            column_spec = cls.get_column_spec(str(inner_type))
            is_dttm = column_spec.is_dttm if column_spec else False

            inner_col = ResultSetColumnType(
                name=name,
                column_name=name,
                type=inner_type,
                is_dttm=is_dttm,
                query_as=f'{query_name} AS "{name}"',
            )
            cols.extend(cls._expand_columns(inner_col))

        return cols

    @classmethod
    def get_columns(
        cls,
        inspector: Inspector,
        table: Table,
        options: dict[str, Any] | None = None,
    ) -> list[ResultSetColumnType]:
        """If the ``expand_rows`` feature is enabled on the database via
        ``schema_options``, expand the schema definition out to show all
        subfields of nested ROWs as their appropriate dotted paths.
        """
        try:
            sqla_columns = inspector.get_columns(table.table, table.schema)
            base_cols = convert_inspector_columns(sqla_columns)
        except NoSuchTableError:
            # The SQLAlchemy dialect can't always reflect Trino tables (e.g. ones
            # with ROW columns raise NoSuchTableError); fall back to SHOW COLUMNS
            # and parse the Trino type strings directly.
            from trino.sqlalchemy import datatype

            full_table_name = cls.quote_table(table, inspector.engine.dialect)
            rows = inspector.bind.execute(
                f"SHOW COLUMNS FROM {full_table_name}"  # noqa: S608
            ).fetchall()
            base_cols = [
                {
                    "name": row.Column,
                    "column_name": row.Column,
                    "type": datatype.parse_sqltype(row.Type),
                    "is_dttm": None,
                    "type_generic": None,
                    "default": None,
                    "nullable": True,
                }
                for row in rows
            ]

        if not (options or {}).get("expand_rows"):
            return base_cols

        return [col for base_col in base_cols for col in cls._expand_columns(base_col)]

    @classmethod
    def get_indexes(
        cls,
        database: Database,
        inspector: Inspector,
        table: Table,
    ) -> list[dict[str, Any]]:
        """Get the indexes associated with the specified schema/table.

        Trino dialect raises ``NoSuchTableError`` in ``get_indexes`` if
        the table is empty.
        """
        try:
            return super().get_indexes(database, inspector, table)
        except NoSuchTableError:
            return []


__all__ = [
    "PrestoBaseEngineSpec",
    "TrinoEngineSpec",
]
