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
"""Async port of ``superset_old/commands/dashboard/filter_state/get.py``."""

from __future__ import annotations

import json  # noqa: TID251
import logging
from datetime import datetime, timedelta
from typing import Any, TYPE_CHECKING

from superset.commands.base import AsyncBaseCommand
from superset.commands.dashboard.filter_state.utils import check_access
from superset.exceptions import ObjectNotFoundError

if TYPE_CHECKING:
    from superset.db.daos.dashboard import AsyncDashboardDAO
    from superset.db.daos.key_value import AsyncKeyValueDAO

logger = logging.getLogger(__name__)


class GetFilterStateCommand(AsyncBaseCommand[str | None]):
    def __init__(
        self,
        dao: AsyncKeyValueDAO,
        dashboard_id: int,
        key: str,
        security_manager: Any | None = None,
        user_id: int | None = None,
        dashboard_dao: AsyncDashboardDAO | None = None,
        user: Any | None = None,
    ) -> None:
        self._dao = dao
        self._dashboard_id = dashboard_id
        self._key = key
        self._security_manager = security_manager
        self._user_id = user_id
        self._dashboard_dao = dashboard_dao
        self._user = user

        # 1:1 with original: read REFRESH_TIMEOUT_ON_RETRIEVAL from
        # FILTER_STATE_CACHE_CONFIG (superset_old/commands/dashboard/
        # filter_state/get.py:31-33).
        from superset.config import SupersetSettings

        settings = SupersetSettings()  # type: ignore[call-arg]
        self._refresh_timeout = settings.filter_state_cache_config.get(
            "REFRESH_TIMEOUT_ON_RETRIEVAL"
        )
        # Cache default timeout (seconds) used to compute expires_on when
        # re-storing the entry on retrieval — mirrors the original's
        # ``SupersetMetastoreCache.set()`` which calls
        # ``_get_expiry(self.default_timeout)``
        # to refresh the TTL to a full window from *now*.
        self._cache_default_timeout: int = settings.filter_state_cache_config.get(
            "CACHE_DEFAULT_TIMEOUT",
            7776000,  # 90 days default
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
        raw = await self._dao.get_value(
            resource="dashboard_filter_state",
            resource_id=self._dashboard_id,
            key=self._key,
        )
        if raw is None:
            raise ObjectNotFoundError("FilterState", self._key)
        # Unwrap envelope written by Create/Update commands
        try:
            entry = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            entry = {}

        # 1:1 with original (superset_old/commands/dashboard/filter_state/
        # get.py:40-41): if the entry exists and REFRESH_TIMEOUT_ON_RETRIEVAL
        # is truthy, re-store the entry to refresh its TTL.
        # The original ``cache_manager.filter_state_cache.set(key, entry)``
        # calls ``SupersetMetastoreCache.set()`` which passes
        # ``_get_expiry(self.default_timeout)`` — a fresh expiry window from
        # *now* using CACHE_DEFAULT_TIMEOUT.  Replicate that here.
        if entry and self._refresh_timeout:
            await self._dao.set_value(
                resource="dashboard_filter_state",
                resource_id=self._dashboard_id,
                key=self._key,
                value=raw,
                expires_on=datetime.now()
                + timedelta(seconds=self._cache_default_timeout),
            )

        if isinstance(entry, dict) and "value" in entry:
            return entry["value"]
        # Original: entry.get("value") → None when "value" key absent (including
        # malformed / non-dict entries).  Returning None lets the controller surface
        # {"value": null} rather than leaking raw KV-store bytes to the caller.
        return None
