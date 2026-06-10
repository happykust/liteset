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
"""Async port of ``superset_old/commands/dashboard/filter_state/create.py``.

Mirrors the original layout — one Command per file under
``commands/dashboard/filter_state/``.

The original sync command keys cache entries with
``cache_key(session.get("_id"), tab_id, resource_id)`` so that repeated
opens of the same Flask session+tab+dashboard reuse the same key.  In
Liteset there is no Flask session id, so the deterministic-key path uses
``uuid5(NAMESPACE_DNS, "{user_id}:{dashboard_id}:{tab_id}")`` — same
purpose, sourced from the JWT-authenticated user instead of the Flask
``session._id`` cookie.  When ``tab_id`` is falsy (None or empty string)
we fall back to a random UUID, matching the original's falsy check
(``if not key or not tab_id: key = random_key()``).
"""

from __future__ import annotations

import json  # noqa: TID251
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, TYPE_CHECKING

from superset.commands.base import AsyncBaseCommand
from superset.commands.dashboard.filter_state.utils import check_access

if TYPE_CHECKING:
    from superset.db.daos.dashboard import AsyncDashboardDAO
    from superset.db.daos.key_value import AsyncKeyValueDAO

logger = logging.getLogger(__name__)


def _filter_state_expiry() -> datetime | None:
    """Expiry timestamp for a filter-state write.

    Mirrors ``SupersetMetastoreCache._get_expiry(self.default_timeout)``
    (superset_old/extensions/metastore_cache.py:74-78) with the
    FILTER_STATE_CACHE_CONFIG ``CACHE_DEFAULT_TIMEOUT`` (90 days default):
    a ``None`` or non-positive timeout means NO expiration (returns None),
    not a TypeError from ``timedelta(seconds=None)``.
    """
    from superset.config import SupersetSettings

    settings = SupersetSettings()  # type: ignore[call-arg]
    timeout: int | None = settings.filter_state_cache_config.get(
        "CACHE_DEFAULT_TIMEOUT",
        7776000,  # 90 days default
    )
    if timeout is not None and timeout > 0:
        return datetime.now() + timedelta(seconds=timeout)
    return None


class CreateFilterStateCommand(AsyncBaseCommand[str]):
    def __init__(
        self,
        dao: AsyncKeyValueDAO,
        dashboard_id: int,
        value: str,
        user_id: int,
        tab_id: str | None = None,
        security_manager: Any | None = None,
        dashboard_dao: AsyncDashboardDAO | None = None,
        user: Any | None = None,
    ) -> None:
        self._dao = dao
        self._dashboard_id = dashboard_id
        self._value = value
        self._user_id = user_id
        self._tab_id = tab_id
        self._security_manager = security_manager
        self._dashboard_dao = dashboard_dao
        self._user = user

    async def validate(self) -> None:
        # ``check_access`` raises ``TemporaryCacheResourceNotFoundError`` /
        # ``TemporaryCacheAccessDeniedError`` 1:1 with the original.
        # Pass the Dashboard DAO (which has get_by_id_or_slug) — NOT the KV DAO.
        # The real user is required so the can_access_dashboard gate is enforced.
        await check_access(
            self._dashboard_dao,
            self._dashboard_id,
            security_manager=self._security_manager,
            user=self._user,
        )

    async def run(self) -> str:
        # Contextual key generation: when tab_id is truthy, build a
        # deterministic UUID (uuid5) from user+dashboard+tab so repeated
        # opens of the same tab reuse the same filter-state entry instead
        # of creating duplicates.  The KV table stores UUIDs, so the key
        # itself must be a valid UUID — see AsyncKeyValueDAO.set_value.
        # Falsy tab_id (None or "") always produces a fresh random key,
        # matching the original: ``if not key or not tab_id: key = random_key()``
        # (superset_old/commands/dashboard/filter_state/create.py:37).
        if self._tab_id:
            seed = f"{self._user_id}:{self._dashboard_id}:{self._tab_id}"
            key = str(uuid.uuid5(uuid.NAMESPACE_DNS, seed))

            # Check cache for existing state at that key (deduplication)
            existing = await self._dao.get_value(
                resource="dashboard_filter_state",
                resource_id=self._dashboard_id,
                key=key,
            )
            if existing is not None:
                logger.debug("Reusing existing filter state key %s", key)
                # Fall through to write the new value with the same key
        else:
            key = str(uuid.uuid4())

        envelope = json.dumps({"owner": self._user_id, "value": self._value})
        # 1:1 with the original write path: ``SupersetMetastoreCache.set()``
        # stores ``expires_on = now + CACHE_DEFAULT_TIMEOUT`` (the
        # FILTER_STATE_CACHE_CONFIG ttl) on EVERY write — entries are not
        # immortal, and a re-create resets the TTL window.
        await self._dao.set_value(
            resource="dashboard_filter_state",
            resource_id=self._dashboard_id,
            key=key,
            value=envelope,
            expires_on=_filter_state_expiry(),
        )
        return key
