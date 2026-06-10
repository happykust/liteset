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
"""``POST /api/v1/sqllab/estimate/`` command.

Ports ``superset_old/commands/sql_lab/estimate.py::QueryEstimationCommand``
into the async pipeline. Loads the database via the DAO, optionally
renders Jinja ``template_params`` against the SQL, then dispatches to
``db_engine_spec.estimate_statement_cost`` (async) per parsed statement
and runs the result through the engine spec's
``query_cost_formatter``. Honours ``SQLLAB_QUERY_COST_ESTIMATE_TIMEOUT``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, TYPE_CHECKING

from superset.commands.base import AsyncBaseCommand
from superset.errors import ErrorLevel, SupersetError, SupersetErrorType
from superset.exceptions import (
    CommandInvalidError,
    SupersetErrorException,
    SupersetTimeoutException,
)

if TYPE_CHECKING:
    from superset.db.daos.database import AsyncDatabaseDAO

logger = logging.getLogger(__name__)


class EstimateQueryCostCommand(AsyncBaseCommand[list[dict[str, Any]]]):
    """Estimate the cost of a multi-statement SQL query.

    1:1 with the original ``QueryEstimationCommand`` (sync). Differences
    are only mechanical: ``db.session.query`` -> ``AsyncDatabaseDAO``,
    sync ``estimate_statement_cost`` -> ``await
    spec.estimate_statement_cost(conn, statement)``, and the timeout is
    enforced via :func:`asyncio.wait_for` rather than the unix-signal
    based ``utils.timeout`` (which is not safe inside the asyncio loop).
    """

    def __init__(
        self,
        database_id: int,
        sql: str,
        schema: str | None = None,
        catalog: str | None = None,
        template_params: dict[str, Any] | None = None,
        dao: "AsyncDatabaseDAO | None" = None,
    ) -> None:
        self._database_id = database_id
        self._sql = sql
        self._schema = schema or ""
        self._catalog = catalog
        self._template_params = template_params or {}
        self._dao = dao
        self._database: Any | None = None

    async def validate(self) -> None:
        if not self._sql.strip():
            raise CommandInvalidError("SQL query cannot be empty")

        if self._dao is not None:
            self._database = await self._dao.find_by_id(self._database_id)
            if self._database is None:
                raise SupersetErrorException(
                    SupersetError(
                        message="The database could not be found",
                        error_type=SupersetErrorType.RESULTS_BACKEND_ERROR,
                        level=ErrorLevel.ERROR,
                    ),
                    status=404,
                )

    async def run(self) -> list[dict[str, Any]]:
        # When invoked from a controller, ``validate()`` has already
        # populated ``self._database``.  When invoked directly (tests
        # without a DAO), we still emulate the original by raising the
        # same SupersetErrorException.
        if self._database is None:
            await self.validate()

        if self._database is None:
            return [{"cost": "Not available"}]

        sql = await self._render_template(self._sql)

        timeout = self._resolve_estimate_timeout()
        try:
            cost = await asyncio.wait_for(
                self._estimate_cost(sql),
                timeout=timeout,
            )
        except (asyncio.TimeoutError, SupersetTimeoutException) as ex:
            logger.exception("Cost estimation timed out after %ss", timeout)
            raise SupersetErrorException(
                SupersetError(
                    message=(
                        f"The query estimation was killed after {timeout} seconds. "
                        "It might be too complex, or the database might be under "
                        "heavy load."
                    ),
                    error_type=SupersetErrorType.SQLLAB_TIMEOUT_ERROR,
                    level=ErrorLevel.ERROR,
                ),
                status=500,
            ) from ex

        return self._format_cost(cost)

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------

    async def _render_template(self, sql: str) -> str:
        """Render Jinja ``template_params`` against ``sql`` if any.

        Mirrors the original
        ``superset_old/commands/sql_lab/estimate.py``: we do this *before*
        dispatching to the engine spec so estimation reflects the same
        rendered SQL the user would execute.
        """
        if not self._template_params:
            return sql
        try:
            from superset.jinja_context import get_template_processor
        except ImportError:
            logger.debug("Jinja not available; skipping template render")
            return sql
        try:
            processor = get_template_processor(self._database)
            rendered = processor.process_template(sql, **self._template_params)
            return rendered if isinstance(rendered, str) else sql
        except Exception:  # noqa: BLE001
            logger.warning(
                "Failed to render Jinja templateParams during cost estimation",
                exc_info=True,
            )
            return sql

    def _resolve_estimate_timeout(self) -> int:
        """Return the configured ``SQLLAB_QUERY_COST_ESTIMATE_TIMEOUT``.

        Falls back to the original default (60 seconds) when the
        ``SupersetSettings`` model is unavailable.
        """
        try:
            from superset.config import SupersetSettings

            settings = SupersetSettings()  # type: ignore[call-arg]
            return int(getattr(settings, "sqllab_query_cost_estimate_timeout", 60))
        except Exception:  # noqa: BLE001
            return 60

    async def _estimate_cost(self, sql: str) -> list[dict[str, Any]]:
        """Run ``EXPLAIN``-style estimation per statement.

        Mirrors ``BaseEngineSpec.estimate_query_cost`` from the original:
        - parse ``sql`` into a :class:`SQLScript`,
        - check ``get_allow_cost_estimate`` (raises if disabled),
        - open *one* connection and run ``estimate_statement_cost`` per
          statement, accumulating the dict-typed cost results.
        """
        from superset.commands.sqllab._shared import get_engine_name
        from superset.sql.parse import SQLScript
        from superset.utils.database import (
            get_async_connection,
            get_engine_spec_for_database,
        )

        engine_spec = get_engine_spec_for_database(self._database)
        extra: dict[str, Any] = {}
        if hasattr(self._database, "get_extra"):
            try:
                extra = self._database.get_extra() or {}
            except Exception:  # noqa: BLE001
                extra = {}

        # ``get_allow_cost_estimate`` may be a classmethod, a method,
        # or a plain bool attribute (Postgres declares it as a bool;
        # Trino as a classmethod). Normalize all three.
        allow = self._is_cost_estimate_allowed(engine_spec, extra)
        if not allow:
            raise SupersetErrorException(
                SupersetError(
                    message="Database does not support cost estimation",
                    error_type=SupersetErrorType.GENERIC_DB_ENGINE_ERROR,
                    level=ErrorLevel.ERROR,
                ),
                status=400,
            )

        engine_name = get_engine_name(self._database)
        parsed_script = SQLScript(sql, engine=engine_name)

        results: list[dict[str, Any]] = []
        async with get_async_connection(self._database) as (conn, _):
            for statement in parsed_script.statements:
                statement_sql = statement.format(
                    comments=getattr(engine_spec, "allows_sql_comments", True),
                )
                if hasattr(self._database, "mutate_sql_based_on_config"):
                    try:
                        statement_sql = self._database.mutate_sql_based_on_config(
                            statement_sql,
                            is_split=True,
                        )
                    except TypeError:
                        statement_sql = self._database.mutate_sql_based_on_config(
                            statement_sql
                        )

                cost = await engine_spec.estimate_statement_cost(conn, statement_sql)
                results.append(cost or {})

        return results

    def _is_cost_estimate_allowed(
        self, engine_spec: Any, extra: dict[str, Any]
    ) -> bool:
        attr = getattr(engine_spec, "get_allow_cost_estimate", None)
        if isinstance(attr, bool):
            return attr
        if callable(attr):
            try:
                return bool(attr(extra))
            except TypeError:
                try:
                    return bool(attr())
                except Exception:  # noqa: BLE001
                    return False
        return False

    def _format_cost(self, cost: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Apply the engine-spec or config-level cost formatter.

        Mirrors the original which checked
        ``QUERY_COST_FORMATTERS_BY_ENGINE`` first, then fell back to the
        engine spec's :meth:`query_cost_formatter`. The
        ``QUERY_COST_FORMATTERS_BY_ENGINE`` config map is preserved in
        :class:`SupersetSettings` and may be empty by default.
        """
        try:
            from superset.commands.sqllab._shared import get_engine_name
            from superset.config import SupersetSettings
            from superset.utils.database import get_engine_spec_for_database

            settings = SupersetSettings()  # type: ignore[call-arg]
            engine_spec = get_engine_spec_for_database(self._database)
            engine_name = get_engine_name(self._database)
            formatters: dict[str, Any] = (
                getattr(settings, "query_cost_formatters_by_engine", {}) or {}
            )
            formatter = formatters.get(engine_name) or getattr(
                engine_spec, "query_cost_formatter", None
            )
            if formatter is None:
                return cost
            try:
                formatted = formatter(cost)
                return list(formatted) if formatted is not None else cost
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Cost formatter failed; returning raw cost", exc_info=True
                )
                return cost
        except Exception:  # noqa: BLE001
            return cost
