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
"""Async port of ``superset_old/commands/dashboard/filter_state/get.py``.

Reads via ``cache_manager.filter_state_cache`` (CACHE_TYPE-honouring slot);
see create.py for the storage-format notes.
"""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from superset.commands.base import AsyncBaseCommand
from superset.commands.dashboard.filter_state.create import _default_cache
from superset.commands.dashboard.filter_state.utils import check_access
from superset.exceptions import ObjectNotFoundError
from superset.temporary_cache.utils import cache_key

if TYPE_CHECKING:
    from superset.db.daos.dashboard import AsyncDashboardDAO

logger = logging.getLogger(__name__)


class GetFilterStateCommand(AsyncBaseCommand[str | None]):
    def __init__(
        self,
        dashboard_id: int,
        key: str,
        security_manager: Any | None = None,
        user_id: int | None = None,
        dashboard_dao: AsyncDashboardDAO | None = None,
        user: Any | None = None,
        cache: Any | None = None,
    ) -> None:
        self._dashboard_id = dashboard_id
        self._key = key
        self._security_manager = security_manager
        self._user_id = user_id
        self._dashboard_dao = dashboard_dao
        self._user = user
        self._cache = cache if cache is not None else _default_cache()

        # 1:1 with original: read REFRESH_TIMEOUT_ON_RETRIEVAL from
        # FILTER_STATE_CACHE_CONFIG (superset_old/commands/dashboard/
        # filter_state/get.py:31-33).
        from superset.config import SupersetSettings

        settings = SupersetSettings()  # type: ignore[call-arg]
        self._refresh_timeout = settings.filter_state_cache_config.get(
            "REFRESH_TIMEOUT_ON_RETRIEVAL"
        )

    async def validate(self) -> None:
        # Pass the Dashboard DAO (which has get_by_id_or_slug) — NOT the KV DAO.
        # The real user is required so the can_access_dashboard gate is enforced.
        await check_access(
            self._dashboard_dao,
            self._dashboard_id,
            security_manager=self._security_manager,
            user=self._user,
        )

    async def run(self) -> str | None:
        ck = cache_key(self._dashboard_id, self._key)
        entry = await self._cache.get(ck)
        if entry is None:
            raise ObjectNotFoundError("FilterState", self._key)

        # 1:1 with original (superset_old/commands/dashboard/filter_state/
        # get.py:40-41): if the entry exists and REFRESH_TIMEOUT_ON_RETRIEVAL
        # is truthy, re-store the entry — the slot's ``set`` stamps a fresh
        # TTL window from *now* (CACHE_DEFAULT_TIMEOUT), refreshing the
        # entry's expiry on both the metastore and Redis backends.
        if entry and self._refresh_timeout:
            await self._cache.set(ck, entry)

        if isinstance(entry, dict) and "value" in entry:
            return entry["value"]
        # Original: entry.get("value") → None when "value" key absent (including
        # malformed / non-dict entries).  Returning None lets the controller surface
        # a 404 rather than leaking raw cache bytes to the caller.
        return None
