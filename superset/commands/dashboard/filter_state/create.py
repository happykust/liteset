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
"""Create command for dashboard filter state entries.

Storage goes through ``cache_manager.filter_state_cache``; the slot honours
``FILTER_STATE_CACHE_CONFIG["CACHE_TYPE"]`` (metastore by default, Redis when
configured) and owns the TTL (``CACHE_DEFAULT_TIMEOUT``).  Entry keys are
``cache_key(resource_id, key)`` strings so that rows persist across server
restarts.

Per-tab dedup uses a deterministic ``uuid5(NAMESPACE_DNS,
"{user_id}:{dashboard_id}:{tab_id}")`` key sourced from the JWT-authenticated
user.  When ``tab_id`` is falsy (None or empty string) a random UUID is used
instead.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, TYPE_CHECKING

from superset.commands.base import AsyncBaseCommand
from superset.commands.dashboard.filter_state.utils import check_access
from superset.temporary_cache.utils import cache_key

if TYPE_CHECKING:
    from superset.db.daos.dashboard import AsyncDashboardDAO

logger = logging.getLogger(__name__)


def _default_cache() -> Any:
    from superset.extensions import cache_manager

    return cache_manager.filter_state_cache


class CreateFilterStateCommand(AsyncBaseCommand[str]):
    def __init__(
        self,
        dashboard_id: int,
        value: str,
        user_id: int,
        tab_id: str | None = None,
        security_manager: Any | None = None,
        dashboard_dao: AsyncDashboardDAO | None = None,
        user: Any | None = None,
        cache: Any | None = None,
    ) -> None:
        self._dashboard_id = dashboard_id
        self._value = value
        self._user_id = user_id
        self._tab_id = tab_id
        self._security_manager = security_manager
        self._dashboard_dao = dashboard_dao
        self._user = user
        self._cache = cache if cache is not None else _default_cache()

    async def validate(self) -> None:
        # Pass Dashboard DAO (has get_by_id_or_slug), not the KV DAO.
        # Real user required so can_access_dashboard gate is enforced.
        await check_access(
            self._dashboard_dao,
            self._dashboard_id,
            security_manager=self._security_manager,
            user=self._user,
        )

    async def run(self) -> str:
        # Deterministic per-tab key: uuid5(user:dashboard:tab); falsy tab_id → random.
        if self._tab_id:
            seed = f"{self._user_id}:{self._dashboard_id}:{self._tab_id}"
            key = str(uuid.uuid5(uuid.NAMESPACE_DNS, seed))
        else:
            key = str(uuid.uuid4())

        # Every write resets the TTL (CACHE_DEFAULT_TIMEOUT).
        entry = {"owner": self._user_id, "value": self._value}
        await self._cache.set(cache_key(self._dashboard_id, key), entry)
        return key
