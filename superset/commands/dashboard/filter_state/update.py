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
"""Async port of ``superset_old/commands/dashboard/filter_state/update.py``."""

from __future__ import annotations

import json  # noqa: TID251
from typing import Any, TYPE_CHECKING

from superset.commands.base import AsyncBaseCommand
from superset.commands.dashboard.filter_state.utils import check_access
from superset.commands.temporary_cache.exceptions import (
    TemporaryCacheAccessDeniedError,
)
from superset.exceptions import ObjectNotFoundError

if TYPE_CHECKING:
    from superset.db.daos.dashboard import AsyncDashboardDAO
    from superset.db.daos.key_value import AsyncKeyValueDAO


class UpdateFilterStateCommand(AsyncBaseCommand[str]):
    def __init__(
        self,
        dao: AsyncKeyValueDAO,
        dashboard_id: int,
        key: str,
        value: str,
        user_id: int,
        security_manager: Any | None = None,
        dashboard_dao: AsyncDashboardDAO | None = None,
        user: Any | None = None,
    ) -> None:
        self._dao = dao
        self._dashboard_id = dashboard_id
        self._key = key
        self._value = value
        self._user_id = user_id
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

        existing = await self._dao.get_value(
            resource="dashboard_filter_state",
            resource_id=self._dashboard_id,
            key=self._key,
        )
        if existing is None:
            raise ObjectNotFoundError("FilterState", self._key)

        # Original raises ``TemporaryCacheAccessDeniedError`` when the
        # current user is not the owner — match that exception type
        # rather than the cross-cutting ``ForbiddenError``.
        try:
            entry = json.loads(existing)
        except (json.JSONDecodeError, TypeError):
            entry = {}
        owner = entry.get("owner")
        if owner is not None and owner != self._user_id:
            raise TemporaryCacheAccessDeniedError()
        # If owner is None (legacy data), allow the update.

    async def run(self) -> str:
        envelope = json.dumps({"owner": self._user_id, "value": self._value})
        await self._dao.set_value(
            resource="dashboard_filter_state",
            resource_id=self._dashboard_id,
            key=self._key,
            value=envelope,
        )
        return self._key
