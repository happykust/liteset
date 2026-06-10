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

Storage goes through ``cache_manager.filter_state_cache`` exactly like the
original (``superset_old/commands/dashboard/filter_state/create.py:42``):
the slot honours ``FILTER_STATE_CACHE_CONFIG["CACHE_TYPE"]`` (metastore by
default, Redis when configured) and owns the TTL
(``CACHE_DEFAULT_TIMEOUT``).  Entry keys are the upstream
``cache_key(resource_id, key)`` strings, so metastore rows land in the
``superset_metastore_cache`` resource under uuid3 — entries written by an
upstream Superset instance keep resolving after a migration.

The original keys the per-tab dedup with
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
        # Dashboard must exist and be accessible — raises
        # ``TemporaryCacheResourceNotFoundError`` /
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
        # Deterministic per-tab token: uuid5(user:dashboard:tab) replaces the
        # original's session-contextual mapping (see module docstring).
        # Falsy tab_id (None or "") always produces a fresh random key,
        # matching the original ``if not key or not tab_id: key = random_key()``
        # (superset_old/commands/dashboard/filter_state/create.py:37).
        if self._tab_id:
            seed = f"{self._user_id}:{self._dashboard_id}:{self._tab_id}"
            key = str(uuid.uuid5(uuid.NAMESPACE_DNS, seed))
        else:
            key = str(uuid.uuid4())

        # 1:1 with ``cache_manager.filter_state_cache.set(
        # cache_key(resource_id, key), entry)`` — the slot stamps the TTL
        # (CACHE_DEFAULT_TIMEOUT) on every write, so a re-create resets the
        # TTL window.
        entry = {"owner": self._user_id, "value": self._value}
        await self._cache.set(cache_key(self._dashboard_id, key), entry)
        return key
