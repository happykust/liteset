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
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

from sqlalchemy.dialects.postgresql import DOUBLE_PRECISION, ENUM, JSON
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql import text
from sqlalchemy.types import Date, DateTime, String

from superset.db.engine_specs.base import (
    AsyncResultSet,
    BaseAsyncEngineSpec,
    ColumnTypeMapping,
)
from superset.typing import GenericDataType

logger = logging.getLogger(__name__)


class AsyncPostgresEngineSpec(BaseAsyncEngineSpec):
    """Async engine spec for PostgreSQL using asyncpg driver."""

    engine = "postgresql"
    engine_name = "PostgreSQL"
    default_driver = "asyncpg"

    supports_dynamic_schema: bool = True
    supports_catalog: bool = True

    get_allow_cost_estimate: bool = True

    column_type_mappings: tuple[ColumnTypeMapping, ...] = (
        (
            re.compile(r"^double precision", re.IGNORECASE),
            DOUBLE_PRECISION(),
            GenericDataType.NUMERIC,
        ),
        (
            re.compile(r"^array.*", re.IGNORECASE),
            String(),
            GenericDataType.STRING,
        ),
        (
            re.compile(r"^json.*", re.IGNORECASE),
            JSON(),
            GenericDataType.STRING,
        ),
        (
            re.compile(r"^enum.*", re.IGNORECASE),
            ENUM(),
            GenericDataType.STRING,
        ),
    )

    _time_grain_expressions: dict[str | None, str] = {
        None: "{col}",
        "PT1S": "DATE_TRUNC('second', {col})",
        "PT5S": (
            "DATE_TRUNC('minute', {col}) + INTERVAL '5 seconds' * "
            "FLOOR(EXTRACT(SECOND FROM {col}) / 5)"
        ),
        "PT30S": (
            "DATE_TRUNC('minute', {col}) + INTERVAL '30 seconds' * "
            "FLOOR(EXTRACT(SECOND FROM {col}) / 30)"
        ),
        "PT1M": "DATE_TRUNC('minute', {col})",
        "PT5M": (
            "DATE_TRUNC('hour', {col}) + INTERVAL '5 minutes' * "
            "FLOOR(EXTRACT(MINUTE FROM {col}) / 5)"
        ),
        "PT10M": (
            "DATE_TRUNC('hour', {col}) + INTERVAL '10 minutes' * "
            "FLOOR(EXTRACT(MINUTE FROM {col}) / 10)"
        ),
        "PT15M": (
            "DATE_TRUNC('hour', {col}) + INTERVAL '15 minutes' * "
            "FLOOR(EXTRACT(MINUTE FROM {col}) / 15)"
        ),
        "PT30M": (
            "DATE_TRUNC('hour', {col}) + INTERVAL '30 minutes' * "
            "FLOOR(EXTRACT(MINUTE FROM {col}) / 30)"
        ),
        "PT1H": "DATE_TRUNC('hour', {col})",
        "P1D": "DATE_TRUNC('day', {col})",
        "P1W": "DATE_TRUNC('week', {col})",
        "P1M": "DATE_TRUNC('month', {col})",
        "P3M": "DATE_TRUNC('quarter', {col})",
        "P1Y": "DATE_TRUNC('year', {col})",
    }

    _custom_errors: list[tuple[re.Pattern[str], str]] = [
        (
            re.compile(r'role "(?P<username>.*?)" does not exist'),
            "Invalid username: {username}",
        ),
        (
            re.compile(r"password authentication failed for user"),
            "Invalid password",
        ),
        (
            re.compile(r'could not translate host name "(?P<host>.*?)"'),
            "Invalid hostname: {host}",
        ),
        (
            re.compile(r"could not connect to server.*Connection refused"),
            "Port is closed or host is unreachable",
        ),
        (
            re.compile(r'database "(?P<database>.*?)" does not exist'),
            "Unknown database: {database}",
        ),
        (
            re.compile(r'column "(?P<column>.*?)" does not exist'),
            "Column does not exist: {column}",
        ),
    ]

    @classmethod
    async def execute(
        cls,
        conn: AsyncConnection,
        query: str,
        parameters: dict[str, Any] | None = None,
    ) -> AsyncResultSet:
        return await cls._default_execute(conn, query, parameters)

    @classmethod
    async def fetch_data(
        cls,
        conn: AsyncConnection,
        query: str,
        limit: int | None = None,
    ) -> list[tuple[Any, ...]]:
        return await cls._default_fetch_data(conn, query, limit)

    @classmethod
    def adjust_engine_params(
        cls,
        uri: str,
        connect_args: dict[str, Any] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        args = connect_args.copy() if connect_args else {}
        args.setdefault("statement_cache_size", 0)
        args.setdefault("prepared_statement_cache_size", 0)
        return uri, args

    # ------------------------------------------------------------------
    # epoch / datetime helpers
    # ------------------------------------------------------------------

    @classmethod
    def epoch_to_dttm(cls) -> str:
        return "(timestamp 'epoch' + {col} * interval '1 second')"

    @classmethod
    def convert_dttm(
        cls,
        target_type: str,
        dttm: datetime,
        db_extra: dict[str, Any] | None = None,
    ) -> str | None:
        sqla_type = cls._get_sqla_column_type(target_type)

        if isinstance(sqla_type, Date):
            return f"TO_DATE('{dttm.date().isoformat()}', 'YYYY-MM-DD')"
        if isinstance(sqla_type, DateTime):
            dttm_formatted = dttm.isoformat(sep=" ", timespec="microseconds")
            return f"TO_TIMESTAMP('{dttm_formatted}', 'YYYY-MM-DD HH24:MI:SS.US')"
        return None

    @classmethod
    def _get_sqla_column_type(cls, native_type: str) -> Any:
        """Resolve *native_type* string to a SQLAlchemy type instance.

        Checks ``column_type_mappings`` first, then falls back to basic
        heuristics for Date / DateTime.
        """
        for regex, sqla_type, _generic in cls.column_type_mappings:
            if regex.match(native_type):
                return sqla_type

        upper = native_type.upper()
        if upper in ("DATE",):
            return Date()
        if upper in (
            "DATETIME",
            "TIMESTAMP",
            "TIMESTAMP WITHOUT TIME ZONE",
            "TIMESTAMP WITH TIME ZONE",
            "TIMESTAMPTZ",
        ):
            return DateTime()
        return None

    # ------------------------------------------------------------------
    # table / schema helpers
    # ------------------------------------------------------------------

    @classmethod
    async def get_catalog_names(
        cls,
        conn: AsyncConnection,
    ) -> set[str]:
        """Return all catalogs (databases) the user can connect to.

        1:1 with upstream ``PostgresEngineSpec.get_catalog_names``
        (``superset_old/db_engine_specs/postgres.py``): queries
        ``pg_database WHERE datistemplate = false`` rather than the base
        class's ``information_schema.schemata`` query, which only returns
        schemas in the *current* database and therefore yields wrong / empty
        results for postgres-wire engines (Postgres, Redshift, StarRocks-over-pg).
        """
        result = await conn.execute(
            text(
                "SELECT datname FROM pg_database WHERE datistemplate = false"
            )
        )
        return {row[0] for row in result.fetchall()}

    @classmethod
    async def get_table_names(
        cls,
        conn: AsyncConnection,
        schema: str | None = None,
    ) -> set[str]:
        """Return table names including foreign tables (specific to Postgres)."""
        from sqlalchemy import inspect as sa_inspect

        def _get(sync_conn: Any) -> set[str]:
            inspector = sa_inspect(sync_conn)
            tables = set(inspector.get_table_names(schema))
            # PGInspector exposes foreign tables
            if hasattr(inspector, "get_foreign_table_names"):
                tables |= set(inspector.get_foreign_table_names(schema))
            return tables

        return await conn.run_sync(_get)

    @classmethod
    def get_prequeries(
        cls,
        schema: str | None = None,
    ) -> list[str]:
        """Set the search_path to *schema* so unqualified names resolve correctly."""
        return [f'set search_path = "{schema}"'] if schema else []

    # ------------------------------------------------------------------
    # cost estimation
    # ------------------------------------------------------------------

    @classmethod
    async def estimate_statement_cost(
        cls,
        conn: AsyncConnection,
        statement: str,
    ) -> dict[str, Any]:
        """Run ``EXPLAIN`` and extract start-up / total cost."""
        sql = f"EXPLAIN {statement}"
        result = await conn.execute(text(sql))
        row = result.fetchone()
        if row:
            match = re.search(r"cost=([\d.]+)\.\.([\d.]+)", row[0])
            if match:
                return {
                    "Start-up cost": float(match.group(1)),
                    "Total cost": float(match.group(2)),
                }
        return {}

    @classmethod
    def query_cost_formatter(
        cls,
        raw_cost: list[dict[str, Any]],
    ) -> list[dict[str, str]]:
        return [{k: str(v) for k, v in row.items()} for row in raw_cost]

    # ------------------------------------------------------------------
    # query cancellation
    # ------------------------------------------------------------------

    @classmethod
    async def get_cancel_query_id(
        cls,
        conn: AsyncConnection,
    ) -> str | None:
        """Return the PostgreSQL backend PID for the current session."""
        result = await conn.execute(text("SELECT pg_backend_pid()"))
        row = result.fetchone()
        return str(row[0]) if row else None

    @classmethod
    async def cancel_query(
        cls,
        conn: AsyncConnection,
        cancel_query_id: str,
    ) -> bool:
        """Terminate the backend identified by *cancel_query_id* (PID)."""
        try:
            await conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) "
                    "FROM pg_stat_activity "
                    "WHERE pid = :pid"
                ),
                {"pid": int(cancel_query_id)},
            )
        except Exception:  # noqa: BLE001
            return False
        return True

    # ------------------------------------------------------------------
    # SSL / extra params
    # ------------------------------------------------------------------

    @classmethod
    def get_extra_params(
        cls,
        extra: str | None = None,
        server_cert: str | None = None,
    ) -> dict[str, Any]:
        """Return extra engine params, including SSL cert handling.

        Parameters mirror the relevant fields on ``Database``.
        """
        import json as _json

        try:
            parsed: dict[str, Any] = _json.loads(extra or "{}")
        except _json.JSONDecodeError as ex:
            raise ValueError("Unable to parse database extras") from ex

        if server_cert:
            import tempfile

            engine_params: dict[str, Any] = parsed.get("engine_params", {})
            connect_args: dict[str, Any] = engine_params.get("connect_args", {})
            connect_args["sslmode"] = connect_args.get("sslmode", "verify-full")

            # Write the certificate to a temp file so asyncpg / libpq can read it
            cert_file = tempfile.NamedTemporaryFile(  # noqa: SIM115
                delete=False, suffix=".crt"
            )
            cert_file.write(server_cert.encode())
            cert_file.flush()
            connect_args["sslrootcert"] = cert_file.name

            engine_params["connect_args"] = connect_args
            parsed["engine_params"] = engine_params

        return parsed

    # ------------------------------------------------------------------
    # datatype resolution
    # ------------------------------------------------------------------

    @classmethod
    def get_datatype(cls, type_code: Any) -> str | None:
        """Resolve a psycopg2-style type code to a type name string.

        When running with asyncpg the type_code is typically already a
        string, so we return it directly.  For psycopg2 backends we look
        up the code in the binary/string type maps.
        """
        if isinstance(type_code, str):
            return type_code

        try:
            from psycopg2.extensions import (
                binary_types,
                string_types,
            )

            types = binary_types.copy()
            types.update(string_types)
            if type_code in types:
                return types[type_code].name
        except ImportError:
            pass

        return None
