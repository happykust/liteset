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
"""``GET /api/v1/sqllab/results/?q=(key:...)`` command.

Direct port of
``superset_old/commands/sql_lab/results.py::SqlExecutionResultsCommand``.
Restores msgpack/zlib decoding of the results-backend payload, the
404/410 distinction (``query missing`` vs ``results missing``), and the
``apply_display_max_row_configuration_if_require`` row cap.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, TYPE_CHECKING

from superset.commands.base import AsyncBaseCommand
from superset.commands.sqllab._shared import apply_display_max_row_configuration_if_require
from superset.errors import ErrorLevel, SupersetError, SupersetErrorType
from superset.exceptions import (
    CommandInvalidError,
    SupersetErrorException,
    SupersetResultsBackendNotConfigureException,
)

if TYPE_CHECKING:
    from superset.db.daos.query import AsyncQueryDAO

logger = logging.getLogger(__name__)


class GetSQLResultsCommand(AsyncBaseCommand[dict[str, Any]]):
    """Fetch a previously-stored SQL Lab result set by ``key``.

    The original 404/410 distinction is preserved:
    - ``404`` when no Query row carries the requested ``results_key``,
    - ``410`` when the Query row exists but the results-backend blob is
      gone (e.g. cache evicted).
    """

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
        # ------------------------------------------------------------------
        # 1. Resolve the Query row by ``results_key`` — fast path,
        # avoids a needless cache hit when the query was never stored.
        # ------------------------------------------------------------------
        query = None
        if self._dao is not None:
            try:
                query = await self._dao.find_one_or_none(results_key=self._key)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "DAO lookup failed for results key %s", self._key, exc_info=True
                )

        results_backend, use_msgpack = self._resolve_results_backend()

        # ------------------------------------------------------------------
        # 2. Optional in-memory ``cache_manager`` short-circuit (used by
        # tests that wire a custom cache; never set in production).
        # ------------------------------------------------------------------
        if self._cache_manager is not None:
            try:
                getter = self._cache_manager.get(self._key)
                cached = await getter if inspect.isawaitable(getter) else getter
                if cached is not None:
                    if self._rows is not None and "data" in cached:
                        cached["data"] = cached["data"][: self._rows]
                    return cached
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Cache get failed for key %s", self._key, exc_info=True
                )

        # ------------------------------------------------------------------
        # 3. No results backend configured — match original 5xx surface.
        # ------------------------------------------------------------------
        if results_backend is None:
            raise SupersetResultsBackendNotConfigureException()

        # ------------------------------------------------------------------
        # 4. Query row missing -> 404 (matches original).
        # ------------------------------------------------------------------
        if query is None:
            logger.warning(
                "404 - Query not found in metadata DB for key: %s", self._key
            )
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

        # ------------------------------------------------------------------
        # 5. Fetch the blob; honour ``results_backend.get`` synchronously
        # via :func:`asyncio.to_thread` because ``flask-caching`` clients
        # are sync.
        # ------------------------------------------------------------------
        blob = await self._results_backend_get(results_backend, self._key)
        if not blob:
            logger.warning(
                "410 - Query exists but results blob missing in backend; key=%s, "
                "query_id=%s",
                self._key,
                getattr(query, "id", None),
            )
            raise SupersetErrorException(
                SupersetError(
                    message=(
                        "Data could not be retrieved from the results backend. "
                        "You need to re-run the original query."
                    ),
                    error_type=SupersetErrorType.RESULTS_BACKEND_ERROR,
                    level=ErrorLevel.ERROR,
                ),
                status=410,
            )

        # ------------------------------------------------------------------
        # 6. Decompress + deserialize using the original helper.
        # ------------------------------------------------------------------
        from superset.utils.core import zlib_decompress

        payload = zlib_decompress(blob, decode=not use_msgpack)
        try:
            obj = _deserialize_results_payload(payload, query, use_msgpack)
        except Exception as ex:  # noqa: BLE001
            raise SupersetErrorException(
                SupersetError(
                    message=(
                        "Data could not be deserialized from the results backend. "
                        "The storage format might have changed, rendering the old "
                        "data stale. You need to re-run the original query."
                    ),
                    error_type=SupersetErrorType.RESULTS_BACKEND_ERROR,
                    level=ErrorLevel.ERROR,
                ),
                status=404,
            ) from ex

        # ------------------------------------------------------------------
        # 7. ``displayLimitReached`` truncation when ``rows`` is requested.
        # ------------------------------------------------------------------
        if self._rows:
            obj = apply_display_max_row_configuration_if_require(obj, self._rows)
        return obj

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------

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

    @staticmethod
    async def _results_backend_get(results_backend: Any, key: str) -> Any:
        import asyncio

        return await asyncio.to_thread(results_backend.get, key)


def _deserialize_results_payload(
    payload: bytes | str, query: Any, use_msgpack: bool
) -> dict[str, Any]:
    """Decode the results-backend payload.

    1:1 with ``superset_old/views/utils.py::_deserialize_results_payload``
    minus the Flask ``stats_timing`` wrappers.
    """
    if use_msgpack:
        import msgpack
        import pyarrow as pa

        ds_payload = msgpack.loads(payload, raw=False)
        try:
            reader = pa.BufferReader(ds_payload["data"])
            pa_table = pa.ipc.open_stream(reader).read_all()
        except pa.ArrowSerializationError as ex:
            raise RuntimeError("Unable to deserialize Arrow IPC table") from ex

        df = pa_table.to_pandas(integer_object_nulls=True)
        try:
            from superset.commands.sqllab._shared import make_json_safe

            ds_payload["data"] = [
                {col: make_json_safe(val) for col, val in row.items()}
                for row in df.to_dict(orient="records")
            ]
        except Exception:  # noqa: BLE001
            ds_payload["data"] = df.to_dict(orient="records")

        for column in ds_payload.get("selected_columns", []):
            if "name" in column and "column_name" not in column:
                column["column_name"] = column.get("name")

        # Honour the engine's ``expand_data`` hook when available — falls
        # back to leaving the columns alone.
        try:
            db_engine_spec = query.database.db_engine_spec
            all_columns, data, expanded_columns = db_engine_spec.expand_data(
                ds_payload["selected_columns"], ds_payload["data"]
            )
            ds_payload.update(
                {
                    "data": data,
                    "columns": all_columns,
                    "expanded_columns": expanded_columns,
                }
            )
        except Exception:  # noqa: BLE001
            ds_payload.setdefault("expanded_columns", [])

        return ds_payload

    # JSON path -- decode from str/bytes
    from superset.utils import json as superset_json

    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")
    return superset_json.loads(payload)
