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
"""Async port of ``superset_old/commands/database/validate_sql.py``.

Honours ``SQL_VALIDATORS_BY_ENGINE`` to dispatch to the appropriate
:class:`BaseSQLValidator` (``PostgreSQLValidator`` -> ``pgsanity``,
``PrestoDBSQLValidator`` -> ``EXPLAIN (TYPE VALIDATE)``). Falls back to
sqlglot syntax checking when no engine validator is configured.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, TYPE_CHECKING

from superset.commands.base import AsyncBaseCommand
from superset.exceptions import CommandInvalidError, ObjectNotFoundError

if TYPE_CHECKING:
    from superset.db.daos.database import AsyncDatabaseDAO

logger = logging.getLogger(__name__)


class ValidateSQLCommand(AsyncBaseCommand[dict[str, Any]]):
    """Validate SQL syntax — engine-aware, with sqlglot fallback.

    1:1 port of ``superset_old/commands/database/validate_sql.py``:
    1. Look up the engine name from the database row.
    2. Resolve the validator name via ``SQL_VALIDATORS_BY_ENGINE``.
    3. If a validator is configured, run it (sync) inside
       :func:`asyncio.to_thread`.
    4. Otherwise fall back to sqlglot transpile-based syntax check.
    """

    def __init__(
        self,
        dao: "AsyncDatabaseDAO",
        database_id: int,
        sql: str,
        schema: str | None = None,
        catalog: str | None = None,
        template_params: dict[str, Any] | None = None,
    ) -> None:
        self._dao = dao
        self._database_id = database_id
        self._sql = sql
        self._schema = schema
        self._catalog = catalog
        self._template_params = template_params or {}
        self._database: Any | None = None

    async def validate(self) -> None:
        if not self._sql or not self._sql.strip():
            raise CommandInvalidError("SQL query is required")
        self._database = await self._dao.find_by_id(self._database_id)
        if not self._database:
            raise ObjectNotFoundError("Database", self._database_id)

    async def run(self) -> dict[str, Any]:
        if self._database is None:
            await self.validate()

        validator_name = self._resolve_validator_name()
        if validator_name:
            try:
                from superset.sql.validators import get_validator_by_name

                validator = get_validator_by_name(validator_name)
                if validator is not None:
                    annotations = await asyncio.to_thread(
                        self._validate_with_validator, validator
                    )
                    return {
                        "result": [a.to_dict() for a in annotations],
                    }
            except ImportError as ex:
                # ``pgsanity`` is an optional dependency; surface a
                # helpful error rather than crashing the request when it
                # is not installed.
                logger.warning(
                    "SQL validator %s could not be loaded (%s); "
                    "falling back to sqlglot",
                    validator_name,
                    ex,
                )

        errors = await asyncio.to_thread(self._validate_with_sqlglot)
        return {"result": errors}

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------

    def _resolve_validator_name(self) -> str | None:
        """Map the database engine to a validator name via config.

        Mirrors ``app.config["SQL_VALIDATORS_BY_ENGINE"]`` from the
        original. The new config map is exposed on
        :class:`SupersetSettings.sql_validators_by_engine`.
        """
        try:
            from superset.config import SupersetSettings

            settings = SupersetSettings()  # type: ignore[call-arg]
            mapping: dict[str, str] = (
                getattr(settings, "sql_validators_by_engine", {}) or {}
            )
        except Exception:  # noqa: BLE001
            return None

        engine = self._engine_name()
        if not engine:
            return None
        return mapping.get(engine)

    def _engine_name(self) -> str:
        spec = getattr(self._database, "db_engine_spec", None)
        if spec is not None and getattr(spec, "engine", None):
            return str(spec.engine)
        uri = str(getattr(self._database, "sqlalchemy_uri", "") or "")
        if "://" in uri:
            return uri.split("://", 1)[0].split("+", 1)[0].lower()
        return ""

    def _validate_with_validator(self, validator: Any) -> list[Any]:
        """Run a per-engine validator synchronously."""
        return validator.validate(
            self._sql,
            self._catalog,
            self._schema,
            self._database,
        )

    def _validate_with_sqlglot(self) -> list[dict[str, Any]]:
        """Parse SQL with sqlglot and return any syntax errors.

        Runs in a thread pool because sqlglot is CPU-bound and fully
        synchronous.
        """
        import sqlglot
        from sqlglot.errors import ParseError

        dialect = self._resolve_dialect()

        errors: list[dict[str, Any]] = []
        try:
            sqlglot.transpile(
                self._sql, read=dialect, error_level=sqlglot.ErrorLevel.RAISE
            )
        except ParseError as ex:
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
        backend = uri.split("://", 1)[0].split("+", 1)[0].lower()
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
