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
"""``GET /api/v1/sqllab/export/<client_id>/`` command.

Direct port of
``superset_old/commands/sql_lab/export.py::SqlResultExportCommand``.
Streams the result-set as CSV — re-decompresses the results-backend blob
when present, otherwise re-runs the query via ``database.get_df`` (sync,
wrapped in :func:`asyncio.to_thread`). All data goes through
``df_to_escaped_csv`` for CSV-injection protection.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, TYPE_CHECKING, TypedDict

from superset.commands.base import AsyncBaseCommand
from superset.errors import ErrorLevel, SupersetError, SupersetErrorType
from superset.exceptions import (
    CommandInvalidError,
    SupersetErrorException,
    SupersetSecurityException,
)

if TYPE_CHECKING:
    from superset.db.daos.query import AsyncQueryDAO

logger = logging.getLogger(__name__)


class SqlExportResult(TypedDict):
    query: Any
    count: int
    data: bytes


class SqlResultExportCommand(AsyncBaseCommand[SqlExportResult]):
    """Export SQL Lab query results as CSV bytes."""

    def __init__(
        self,
        dao: "AsyncQueryDAO",
        client_id: str,
        security_manager: Any = None,
        current_user: Any = None,
    ) -> None:
        self._dao = dao
        self._client_id = client_id
        self._security_manager = security_manager
        self._current_user = current_user
        self._query: Any | None = None

    async def validate(self) -> None:
        if not self._client_id:
            raise CommandInvalidError("client_id is required")

        try:
            self._query = await self._dao.find_one_or_none(client_id=self._client_id)
        except Exception:  # noqa: BLE001
            self._query = None

        if self._query is None:
            raise SupersetErrorException(
                SupersetError(
                    message=(
                        "The query associated with these results could not be found. "
                        "You need to re-run the original query."
                    ),
                    error_type=SupersetErrorType.RESULTS_BACKEND_ERROR,
                    level=ErrorLevel.ERROR,
                ),
                status=404,
            )

        # Eager-load the ``database`` relationship NOW (async context) so the
        # sync ``_fetch_dataframe_via_get_df`` worker thread — which calls
        # ``self._query.database.get_df(...)`` — doesn't trip a lazy-load
        # against asyncpg and crash with ``MissingGreenlet``. The
        # results-backend fast path doesn't need it, but the get_df fallback
        # does, and we can't tell which path will run until run().
        try:
            await self._dao.session.refresh(self._query, ["database"])
        except Exception:  # noqa: BLE001
            # Some Query rows may have no FK / a detached state; the get_df
            # path will surface a clearer error than a refresh failure.
            logger.debug("Could not eager-load query.database", exc_info=True)

        # Permission gate — security manager based, mirroring the original
        # ``query.raise_for_access()`` which delegates to
        # ``security_manager.raise_for_access(query=...)``.
        if self._security_manager is not None and self._current_user is not None:
            try:
                await self._security_manager.raise_for_access(
                    user=self._current_user,
                    query=self._query,
                )
            except SupersetSecurityException as ex:
                raise SupersetErrorException(
                    SupersetError(
                        message="Cannot access the query",
                        error_type=SupersetErrorType.QUERY_SECURITY_ACCESS_ERROR,
                        level=ErrorLevel.ERROR,
                    ),
                    status=403,
                ) from ex

    async def run(self) -> SqlExportResult:
        if self._query is None:
            await self.validate()
        assert self._query is not None  # noqa: S101 -- validate guarantees this

        df = await self._build_dataframe()
        csv_bytes = await asyncio.to_thread(self._render_csv, df)

        return {
            "query": self._query,
            "count": int(len(df.index)),
            "data": csv_bytes,
        }

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------

    async def _build_dataframe(self) -> Any:
        """Return a pandas DataFrame for the export.

        Tries the results-backend cached blob first (matches original
        order). Falls back to re-executing the query via
        ``database.get_df`` (sync) wrapped in ``asyncio.to_thread``.
        """
        import pandas as pd

        results_backend, use_msgpack = self._resolve_results_backend()
        results_key = getattr(self._query, "results_key", None)

        if results_backend is not None and results_key:
            blob = await asyncio.to_thread(results_backend.get, results_key)
            if blob:
                from superset.commands.sqllab.results import (
                    _deserialize_results_payload,
                )
                from superset.utils.core import zlib_decompress

                payload = zlib_decompress(blob, decode=not use_msgpack)
                obj = _deserialize_results_payload(
                    payload, self._query, bool(use_msgpack)
                )
                return pd.DataFrame(
                    data=obj.get("data", []),
                    dtype=object,
                    columns=[c["name"] for c in obj.get("columns", [])],
                )

        # ------------------------------------------------------------------
        # Re-run the query — same fallback as the original.
        # ------------------------------------------------------------------
        return await asyncio.to_thread(self._fetch_dataframe_via_get_df)

    def _fetch_dataframe_via_get_df(self) -> Any:
        """Synchronously execute the original SQL via ``database.get_df``.

        Mirrors the exact branching from the original ``run`` method:
        if ``select_sql`` was populated by a CTAS execution we read from
        the materialised ``select_sql``; otherwise we re-execute the
        ``executed_sql`` while honouring the original ``LimitingFactor``
        adjustments.
        """
        from superset.commands.sqllab._shared import get_engine_name
        from superset.models.sql_lab import LimitingFactor
        from superset.sql.parse import SQLScript

        if getattr(self._query, "select_sql", None):
            sql = self._query.select_sql
            limit: int | None = None
        else:
            sql = self._query.executed_sql
            try:
                script = SQLScript(sql, get_engine_name(self._query.database))
                limit = script.statements[-1].get_limit_value()
            except Exception:  # noqa: BLE001
                limit = None

        if limit is not None and self._query.limiting_factor in {
            LimitingFactor.QUERY,
            LimitingFactor.DROPDOWN,
            LimitingFactor.QUERY_AND_DROPDOWN,
        }:
            limit -= 1

        df = self._query.database.get_df(
            sql,
            self._query.catalog,
            self._query.schema,
        )
        if limit is not None:
            df = df[:limit]
        return df

    def _render_csv(self, df: Any) -> bytes:
        from superset.utils.csv import df_to_escaped_csv

        try:
            from superset.config import SupersetSettings

            settings = SupersetSettings()  # type: ignore[call-arg]
            csv_export = getattr(settings, "csv_export", {}) or {}
        except Exception:  # noqa: BLE001
            csv_export = {}

        encoding = csv_export.get("encoding", "utf-8")
        kwargs = {k: v for k, v in csv_export.items()}
        kwargs.setdefault("index", False)

        csv_string = df_to_escaped_csv(df, **kwargs)
        return csv_string.encode(encoding)

    def _resolve_results_backend(self) -> tuple[Any | None, bool]:
        try:
            from superset.config import SupersetSettings

            settings = SupersetSettings()  # type: ignore[call-arg]
            return (
                getattr(settings, "results_backend", None),
                bool(getattr(settings, "results_backend_use_msgpack", True)),
            )
        except Exception:  # noqa: BLE001
            return None, True
