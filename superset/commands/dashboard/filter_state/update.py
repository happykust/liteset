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
"""Async port of ``superset_old/commands/dashboard/filter_state/update.py``.

Storage goes through ``cache_manager.filter_state_cache`` — see create.py
for the CACHE_TYPE / key-format notes.

The original update command performs tab_id-based key rotation:
when ``tab_id`` changes (or is falsy), a new key is generated and the
contextual mapping is updated. This liteset port replicates that logic
using uuid5 deterministic keys (same approach as CreateFilterStateCommand)
instead of Flask's ``session._id`` + ``cache_manager`` contextual keys.
"""

from __future__ import annotations

import uuid
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


class UpdateFilterStateCommand(AsyncBaseCommand[str]):
    def __init__(
        self,
        dashboard_id: int,
        key: str,
        value: str,
        user_id: int,
        tab_id: str | None = None,
        security_manager: Any | None = None,
        dashboard_dao: AsyncDashboardDAO | None = None,
        user: Any | None = None,
        cache: Any | None = None,
    ) -> None:
        self._dashboard_id = dashboard_id
        self._key = key
        self._value = value
        self._user_id = user_id
        self._tab_id = tab_id
        self._security_manager = security_manager
        self._dashboard_dao = dashboard_dao
        self._user = user
        self._cache = cache if cache is not None else _default_cache()

    async def validate(self) -> None:
        # ``check_access`` raises ``TemporaryCacheResourceNotFoundError`` /
        # ``TemporaryCacheAccessDeniedError`` 1:1 with the original.
        # Pass the Dashboard DAO (which has get_by_id_or_slug) -- NOT the KV DAO.
        # The real user is required so the can_access_dashboard gate is enforced.
        await check_access(
            self._dashboard_dao,
            self._dashboard_id,
            security_manager=self._security_manager,
            user=self._user,
        )

    async def run(self) -> str:
        # Original (superset_old/commands/dashboard/filter_state/update.py:38-55):
        # entry = cache.get(cache_key(resource_id, key))
        # if entry:  <-- missing key → skip all writes, return original key
        #     if entry["owner"] != owner: raise TemporaryCacheAccessDeniedError()
        #     ... key rotation ...
        #     cache.set(cache_key(resource_id, key), new_entry)
        # return key  <-- always the original key when entry is falsy
        #
        # Do NOT raise ObjectNotFoundError on missing entry — that would change the HTTP
        # status from 200 to 404, breaking the API contract.
        entry = await self._cache.get(cache_key(self._dashboard_id, self._key))

        if entry is not None:
            if not isinstance(entry, dict):
                entry = {}
            # Original raises ``TemporaryCacheAccessDeniedError`` when the
            # current user is not the owner.
            owner = entry.get("owner")
            if owner != self._user_id:
                raise TemporaryCacheAccessDeniedError()

            # Key rotation — deterministic uuid5 when tab_id is truthy,
            # fresh random key otherwise, matching the original's
            # ``if not key or not tab_id: key = random_key()``.
            if self._tab_id:
                seed = f"{self._user_id}:{self._dashboard_id}:{self._tab_id}"
                key = str(uuid.uuid5(uuid.NAMESPACE_DNS, seed))
            else:
                key = str(uuid.uuid4())

            # Slot stamps a fresh TTL window on every write — 1:1 with
            # ``SupersetMetastoreCache.set()`` (see create.py).
            new_entry = {"owner": self._user_id, "value": self._value}
            await self._cache.set(cache_key(self._dashboard_id, key), new_entry)
            return key

        # Entry not found — original silently skips the write and returns the
        # original URL key unchanged (HTTP 200, no write performed).
        return self._key
