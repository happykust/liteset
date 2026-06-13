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

1:1 with the original handler
``superset_old/sqllab/api.py::format_sql`` (lines 231-236) which dispatched
to ``SQLScript(model["sql"], model.get("engine")).format()`` *without*
catching parse/format errors — so an unparseable snippet propagates as a
``SupersetParseError`` (HTTP 422), rather than echoing the raw SQL with 200.
"""

from __future__ import annotations

import asyncio
import logging

from superset.commands.base import AsyncBaseCommand
from superset.exceptions import CommandInvalidError

logger = logging.getLogger(__name__)


class FormatSQLCommand(AsyncBaseCommand[str]):
    """Format SQL using engine-aware ``SQLScript.format()``.

    Mirrors the original handler exactly: parse failures are *not* swallowed.
    ``SQLScript(...).format()`` raises ``SupersetParseError`` (status 422) on
    an unparseable statement and that propagates to the client, instead of
    returning the unformatted SQL with a 200.
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
        from superset.sql.parse import SQLScript

        # 1:1 with ``superset_old/sqllab/api.py::format_sql`` —
        # ``SQLScript(sql, engine).format()`` with no error suppression.
        # ``engine`` is ``None`` when the caller omitted it, exactly as the
        # original passed ``model.get("engine")``; ``SQLGLOT_DIALECTS.get``
        # tolerates ``None`` (falls back to the sqlglot default dialect).
        return SQLScript(self._sql, self._engine).format()  # type: ignore[arg-type]
