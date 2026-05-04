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
"""Shared helpers used across the sqllab command package.

Most of these are direct ports of the synchronous helpers in
``superset_old/sql_lab.py`` and ``superset_old/sqllab/utils.py`` adapted
to the Liteset async/sync split.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa

logger = logging.getLogger(__name__)

# Default maximum rows if no limit is provided and no config override.
DEFAULT_SQL_MAX_ROW = 100000

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


def map_sqlglot_dialect(engine: str | None) -> str | None:
    """Map a SQLAlchemy backend name (e.g. "postgresql") to a sqlglot
    dialect name (e.g. "postgres"). Returns None if no engine is given,
    letting sqlglot auto-detect. Unknown engines are returned as-is.
    """
    if not engine:
        return None
    name = engine.split("+", 1)[0].lower()
    return _SQLGLOT_DIALECT_ALIASES.get(name, name)


def make_json_safe(value: Any) -> Any:
    """Convert Python values to JSON-serializable types.

    JS-MAX-INTEGER cast for big integers ports
    ``superset_old/dataframe.py::_convert_big_integers`` so frontends
    that decode the JSON via ``Number(...)`` don't lose precision on
    e.g. Snowflake ``NUMBER(38,0)`` IDs.
    """
    from superset.utils.core import JS_MAX_INTEGER

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
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and abs(value) > JS_MAX_INTEGER:
        return str(value)
    return value


def to_sync_uri(uri: str) -> str:
    """Replace async driver prefixes with sync equivalents.

    E.g. ``postgresql+asyncpg://...`` becomes ``postgresql+psycopg2://...``.
    """
    for async_prefix, sync_prefix in _ASYNC_DRIVER_REPLACEMENTS.items():
        if uri.startswith(async_prefix):
            return sync_prefix + uri[len(async_prefix) :]
    return uri


def build_connection_uri(database: Any) -> str:
    """Build a usable sync connection string from a Database model.

    The ``Database.sqlalchemy_uri`` column stores the URI with the password
    masked (``PASSWORD_MASK``). The real password is in ``Database.password``.
    We reconstruct the full URI by injecting the password back in.
    """
    raw_uri = database.sqlalchemy_uri or ""
    try:
        url = sa.engine.make_url(raw_uri)
    except Exception:  # noqa: BLE001
        # If the stored URI is totally invalid, return as-is and let
        # SQLAlchemy raise a proper error later.
        return to_sync_uri(raw_uri)

    # Inject password from the separate column if it was masked.
    password = getattr(database, "password", None)
    if password:
        url = url.set(password=password)

    return to_sync_uri(url.render_as_string(hide_password=False))


def get_engine_name(database: Any) -> str:
    """Return the sqlglot-friendly engine name for ``database``.

    Mirrors ``database.db_engine_spec.engine`` access from the original
    while gracefully falling back to the raw SQLAlchemy backend name when
    the engine spec is unavailable (e.g. unit tests using a mock DAO).
    """
    spec = getattr(database, "db_engine_spec", None)
    if spec is not None:
        engine = getattr(spec, "engine", None)
        if engine:
            return str(engine)
    uri = str(getattr(database, "sqlalchemy_uri", "") or "")
    if "://" in uri:
        return uri.split("://", 1)[0].split("+", 1)[0].lower()
    return "base"


def apply_display_max_row_configuration_if_require(
    sql_results: dict[str, Any],
    max_rows_in_result: int,
) -> dict[str, Any]:
    """Cap the rows in ``sql_results`` to ``max_rows_in_result``.

    1:1 with
    ``superset_old/sqllab/utils.py::apply_display_max_row_configuration_if_require``
    so ``GetSQLResultsCommand`` returns the exact same shape (the
    frontend uses the ``displayLimitReached`` boolean).
    """
    from superset.common.query_status import QueryStatus

    def is_require_to_apply() -> bool:
        return (
            sql_results.get("status") == QueryStatus.SUCCESS
            and sql_results.get("query", {}).get("rows", 0) > max_rows_in_result
        )

    if is_require_to_apply():
        sql_results["data"] = sql_results["data"][:max_rows_in_result]
        sql_results["displayLimitReached"] = True
    return sql_results
