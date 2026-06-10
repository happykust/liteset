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
"""Async port of ``superset_old/commands/dashboard/filter_state/delete.py``.

Storage goes through ``cache_manager.filter_state_cache`` — see create.py
for the CACHE_TYPE / key-format notes.
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from superset.commands.base import AsyncBaseCommand
from superset.commands.dashboard.filter_state.create import _default_cache
from superset.commands.dashboard.filter_state.utils import check_access
from superset.commands.temporary_cache.exceptions import (
    TemporaryCacheAccessDeniedError,
)
from superset.temporary_cache.utils import cache_key

if TYPE_CHECKING:
    from superset.db.daos.dashboard import AsyncDashboardDAO


class DeleteFilterStateCommand(AsyncBaseCommand[bool]):
    def __init__(
        self,
        dashboard_id: int,
        key: str,
        user_id: int | None = None,
        security_manager: Any | None = None,
        dashboard_dao: AsyncDashboardDAO | None = None,
        user: Any | None = None,
        cache: Any | None = None,
    ) -> None:
        self._dashboard_id = dashboard_id
        self._key = key
        self._user_id = user_id
        self._security_manager = security_manager
        self._dashboard_dao = dashboard_dao
        self._user = user
        self._cache = cache if cache is not None else _default_cache()

    async def validate(self) -> None:
        # Pass the Dashboard DAO (which has get_by_id_or_slug) — NOT the KV DAO.
        # The real user is required so the can_access_dashboard gate is enforced.
        await check_access(
            self._dashboard_dao,
            self._dashboard_id,
            security_manager=self._security_manager,
            user=self._user,
        )

    async def run(self) -> bool:
        # 1:1 with original — read the entry first, raise
        # ``TemporaryCacheAccessDeniedError`` if the current user is not
        # the owner, then delete.  Returns ``False`` for missing keys.
        ck = cache_key(self._dashboard_id, self._key)
        entry = await self._cache.get(ck)
        if entry is None:
            return False
        if not isinstance(entry, dict):
            entry = {}
        owner = entry.get("owner")
        if owner is not None and self._user_id is not None and owner != self._user_id:
            raise TemporaryCacheAccessDeniedError()

        await self._cache.delete(ck)
        return True
