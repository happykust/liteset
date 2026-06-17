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
"""Chart data retrieval command."""

from __future__ import annotations

import logging
from typing import Any

from superset.commands.base import AsyncBaseCommand
from superset.commands.chart.exceptions import (
    ChartDataCacheLoadError,
    ChartDataQueryFailedError,
)
from superset.common.query_context import AsyncQueryContext
from superset.common.query_context_processor import AsyncQueryContextProcessor
from superset.exceptions import CacheLoadError

logger = logging.getLogger(__name__)


class ChartDataCommand(AsyncBaseCommand[dict[str, Any]]):
    """Process a ChartDataQueryContext via AsyncQueryContextProcessor."""

    _query_context: AsyncQueryContext

    def __init__(
        self,
        query_context: AsyncQueryContext,
        processor: AsyncQueryContextProcessor,
    ) -> None:
        self._query_context = query_context
        self._processor = processor
        self._cache_query_context: bool = False
        self._force_cached: bool = False

    async def validate(self) -> None:
        await self._processor.raise_for_access()

    async def run(self, **kwargs: Any) -> dict[str, Any]:
        cache_query_context: bool = kwargs.get("cache", self._cache_query_context)
        force_cached: bool = kwargs.get("force_cached", self._force_cached)

        try:
            payload = await self._processor.get_payload(
                self._query_context.queries,
                force=self._query_context.force,
                cache_query_context=cache_query_context,
                force_cached=force_cached,
            )
        except CacheLoadError as ex:
            raise ChartDataCacheLoadError(ex.message) from ex

        # Skip per-query error check for query-only requests — errors
        # are returned in payload so the View Query modal can display
        # validation errors.
        result_type = getattr(self._query_context, "result_type", None)
        if result_type != "query":
            for query in payload.get("queries", []):
                if isinstance(query, dict) and query.get("error"):
                    raise ChartDataQueryFailedError(f"Error: {query['error']}")

        return_value: dict[str, Any] = {
            "query_context": self._query_context,
            "queries": payload.get("queries", []),
        }
        if cache_query_context and "cache_key" in payload:
            return_value["cache_key"] = payload["cache_key"]
        return return_value
