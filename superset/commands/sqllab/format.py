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
"""``POST /api/v1/sqllab/format_sql/`` command.

1:1 with the original Flask handler
``superset_old/sqllab/api.py::format_sql`` which dispatched to
``SQLScript(sql, engine).format()``. We reuse the new ``SQLScript`` so
engine-specific formatting (e.g. comments policy) stays consistent with
``ExecuteSQLCommand`` and the validators.
"""

from __future__ import annotations

import asyncio
import logging

from superset.commands.base import AsyncBaseCommand
from superset.exceptions import CommandInvalidError

logger = logging.getLogger(__name__)


class FormatSQLCommand(AsyncBaseCommand[str]):
    """Format SQL using engine-aware ``SQLScript.format()``.

    Falls back to the raw input when sqlglot cannot parse the snippet —
    matching the original behaviour of returning the user's text rather
    than raising 5xx.
    """

    def __init__(self, sql: str, engine: str | None = None) -> None:
        self._sql = sql
        self._engine = engine

    async def validate(self) -> None:
        if not self._sql.strip():
            raise CommandInvalidError("SQL cannot be empty")

    async def run(self) -> str:
        # SQLScript dispatches to engine-specific dialect maps and the
        # original ``format()`` semantics (";\n"-joined statements).
        return await asyncio.to_thread(self._format_sync)

    def _format_sync(self) -> str:
        try:
            from superset.exceptions import SupersetParseError
            from superset.sql.parse import SQLScript

            try:
                script = SQLScript(self._sql, engine=self._engine or "base")
                return script.format()
            except SupersetParseError:
                logger.warning(
                    "SQLScript could not parse input for engine %s; "
                    "falling back to raw sqlglot.transpile",
                    self._engine,
                    exc_info=True,
                )
        except ImportError:
            logger.debug("superset.sql.parse not available; trying sqlglot directly")

        try:
            import sqlglot
            from sqlglot.errors import SqlglotError
        except ImportError:
            logger.debug("sqlglot not available, returning unformatted SQL")
            return self._sql

        from superset.commands.sqllab._shared import map_sqlglot_dialect

        dialect = map_sqlglot_dialect(self._engine)
        try:
            result = sqlglot.transpile(self._sql, read=dialect, pretty=True)
            return result[0]
        except (SqlglotError, ValueError):
            logger.warning("SQL formatting failed, returning original", exc_info=True)
            return self._sql
