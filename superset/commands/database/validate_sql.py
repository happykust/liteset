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
"""Async port of ``superset_old/commands/database/validate_sql.py``."""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from superset.commands.base import AsyncBaseCommand
from superset.exceptions import CommandInvalidError, ObjectNotFoundError

if TYPE_CHECKING:
    from superset.db.daos.database import AsyncDatabaseDAO

logger = logging.getLogger(__name__)


class ValidateSQLCommand(AsyncBaseCommand[dict[str, Any]]):
    """Validate SQL syntax using sqlglot.

    Ported from superset_old/commands/database/validate_sql.py.
    The original delegates to engine-specific validators; since engine
    specs are stubs in Liteset, we use sqlglot for dialect-aware
    syntax checking.  Errors are returned in the original format:
    ``[{"line_number": N, "start_column": N, "end_column": N, "message": "..."}]``
    """

    def __init__(
        self,
        dao: AsyncDatabaseDAO,
        database_id: int,
        sql: str,
        schema: str | None = None,
    ) -> None:
        self._dao = dao
        self._database_id = database_id
        self._sql = sql
        self._schema = schema
        self._database: Any | None = None

    async def validate(self) -> None:
        if not self._sql or not self._sql.strip():
            raise CommandInvalidError("SQL query is required")
        self._database = await self._dao.find_by_id(self._database_id)
        if not self._database:
            raise ObjectNotFoundError("Database", self._database_id)

    async def run(self) -> dict[str, Any]:
        import asyncio

        errors = await asyncio.to_thread(self._validate_with_sqlglot)
        return {"result": errors}

    def _validate_with_sqlglot(self) -> list[dict[str, Any]]:
        """Parse SQL with sqlglot and return any syntax errors.

        Runs in a thread pool because sqlglot is CPU-bound and fully
        synchronous.
        """
        import sqlglot
        from sqlglot.errors import ParseError

        # Map database backend to sqlglot dialect
        dialect = self._resolve_dialect()

        errors: list[dict[str, Any]] = []
        try:
            sqlglot.transpile(
                self._sql, read=dialect, error_level=sqlglot.ErrorLevel.RAISE
            )
        except ParseError as ex:
            # sqlglot ParseError includes error details in .errors
            for err in getattr(ex, "errors", []):
                line = err.get("line", 1)
                col = err.get("col", 0)
                description = err.get("description", str(ex))
                errors.append(
                    {
                        "line_number": line,
                        "start_column": col,
                        "end_column": col,
                        "message": description,
                    }
                )
            # If no structured errors were parsed, return the raw message
            if not errors:
                errors.append(
                    {
                        "line_number": 1,
                        "start_column": 0,
                        "end_column": 0,
                        "message": str(ex),
                    }
                )

        return errors

    def _resolve_dialect(self) -> str | None:
        """Map the database's SQLAlchemy URI backend to a sqlglot dialect."""
        if not self._database:
            return None
        uri = str(getattr(self._database, "sqlalchemy_uri", "") or "")
        if "://" not in uri:
            return None
        backend = uri.split("://")[0].split("+")[0].lower()
        backend_to_dialect: dict[str, str] = {
            "postgresql": "postgres",
            "mysql": "mysql",
            "sqlite": "sqlite",
            "mssql": "tsql",
            "clickhouse": "clickhouse",
            "trino": "trino",
            "presto": "presto",
            "hive": "hive",
            "bigquery": "bigquery",
            "snowflake": "snowflake",
            "redshift": "redshift",
            "duckdb": "duckdb",
            "oracle": "oracle",
            "spark": "spark",
            "databricks": "databricks",
        }
        return backend_to_dialect.get(backend)
