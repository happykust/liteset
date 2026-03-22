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
"""Chart data command classes — processes QueryContext through the async pipeline."""

from __future__ import annotations

import inspect
import logging
from typing import Any

from liteset.commands.base import AsyncBaseCommand
from liteset.common.query_context import AsyncQueryContext
from liteset.common.query_context_processor import AsyncQueryContextProcessor
from liteset.exceptions import CommandInvalidError, ForbiddenError

logger = logging.getLogger(__name__)


class ChartDataCommand(AsyncBaseCommand[dict[str, Any]]):
    """Process a ChartDataQueryContext via AsyncQueryContextProcessor."""

    def __init__(
        self,
        query_context: AsyncQueryContext,
        processor: AsyncQueryContextProcessor,
    ) -> None:
        self._query_context = query_context
        self._processor = processor

    async def validate(self) -> None:
        await self._processor.raise_for_access()

    async def run(self) -> dict[str, Any]:
        payload = await self._processor.get_payload(
            self._query_context.queries,
            force=self._query_context.force,
        )
        # Check each query result for errors (unless result_type is "query",
        # which intentionally returns the generated SQL without executing it).
        result_type = getattr(self._query_context, "result_type", None)
        if result_type != "query":
            for result in payload.get("queries", []):
                if isinstance(result, dict) and result.get("error"):
                    raise CommandInvalidError(result["error"])
        return payload


class GetCachedChartDataCommand(AsyncBaseCommand[dict[str, Any] | None]):
    """Retrieve chart data from cache by key, with access-control re-validation.

    Mirrors Superset's ``data_from_cache`` flow:
    1. Load the cached entry (which should contain ``form_data`` / query context
       metadata alongside the result data).
    2. Reconstruct enough context to call ``raise_for_access()`` — verifying
       the *current* user still has permission to view the underlying datasource.
    3. Return the cached payload only after access is confirmed.

    If the cache entry lacks the metadata needed for permission validation the
    command denies access (fail-closed) rather than serving potentially
    unauthorized data.
    """

    def __init__(
        self,
        cache_key: str,
        cache_manager: Any | None = None,
        security_manager: Any | None = None,
        datasource_dao: Any | None = None,
        settings: Any | None = None,
        user: Any | None = None,
    ) -> None:
        self._cache_key = cache_key
        self._cache_manager = cache_manager
        self._security_manager = security_manager
        self._datasource_dao = datasource_dao
        self._settings = settings
        self._user = user

    async def validate(self) -> None:
        if not self._cache_key:
            raise CommandInvalidError("cache_key is required")

    async def _raise_for_access(self, raw: dict[str, Any]) -> None:
        """Re-validate datasource access using metadata stored alongside cached data.

        The cache entry is expected to contain either:
        - ``form_data`` with a ``datasource`` reference (Superset convention), or
        - ``datasource_id`` / ``datasource_type`` fields written by liteset.

        When a security manager and datasource DAO are available the method
        resolves the datasource and delegates to
        ``AsyncQueryContextProcessor.raise_for_access()``.  When insufficient
        context is available to perform the check the request is denied.
        """
        if self._security_manager is None:
            raise ForbiddenError(
                "Security context unavailable — cannot serve cached chart data"
            )

        # --- Extract datasource reference from the cached entry ---------------
        datasource_id: int | None = None
        datasource_type: str | None = None

        # Strategy 1: ``form_data.datasource`` (Superset QueryContext cache format)
        form_data = raw.get("form_data")
        if isinstance(form_data, dict):
            ds_ref = form_data.get("datasource")
            if isinstance(ds_ref, str) and "__" in ds_ref:
                parts = ds_ref.split("__")
                try:
                    datasource_id = int(parts[0])
                    datasource_type = parts[1]
                except (ValueError, IndexError):
                    pass
            elif isinstance(ds_ref, dict):
                datasource_id = ds_ref.get("id")
                datasource_type = ds_ref.get("type")

        # Strategy 2: top-level ``datasource_id`` / ``datasource_type``
        if datasource_id is None:
            datasource_id = raw.get("datasource_id")
            datasource_type = raw.get("datasource_type")

        # Strategy 3: ``datasource`` dict at top level
        if datasource_id is None:
            ds_obj = raw.get("datasource")
            if isinstance(ds_obj, dict):
                datasource_id = ds_obj.get("id")
                datasource_type = ds_obj.get("type")

        if datasource_id is None:
            raise ForbiddenError(
                "Cached entry does not contain datasource metadata required "
                "for access validation — denying access"
            )

        # --- Resolve datasource and check permissions -------------------------
        datasource: Any = None
        if self._datasource_dao is not None:
            try:
                finder = self._datasource_dao.find_by_id_and_type(
                    datasource_id, datasource_type or "table"
                )
                datasource = (
                    await finder if inspect.isawaitable(finder) else finder
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Failed to resolve datasource %s/%s for cache access check",
                    datasource_id,
                    datasource_type,
                    exc_info=True,
                )

        if datasource is None:
            raise ForbiddenError(
                f"Datasource {datasource_id} ({datasource_type}) not found "
                f"— cannot validate access for cached chart data"
            )

        # Build a minimal processor solely for the access check
        processor = AsyncQueryContextProcessor(
            datasource=datasource,
            settings=self._settings,
            security_manager=self._security_manager,
            user=self._user,
        )
        await processor.raise_for_access()

    async def run(self) -> dict[str, Any] | None:
        if self._cache_manager is None:
            return None
        try:
            getter = self._cache_manager.get(self._cache_key)
            raw = await getter if inspect.isawaitable(getter) else getter
            if raw is None:
                return None
        except Exception:  # noqa: BLE001
            logger.warning(
                "Cache get failed for key %s", self._cache_key, exc_info=True
            )
            return None

        # --- Access control re-validation -------------------------------------
        # If the cached entry is a dict, attempt to re-validate permissions
        # using any datasource metadata stored alongside the data.
        if isinstance(raw, dict):
            await self._raise_for_access(raw)

            # Return the cached payload in a normalised shape
            if "result" in raw:
                return raw
            if "data" in raw:
                return {"result": [raw]}
            return {"result": [raw]}

        # Non-dict cached values cannot carry datasource metadata.
        # Deny access rather than serving potentially unauthorised data.
        raise ForbiddenError(
            "Cached entry lacks metadata required for access validation"
        )
