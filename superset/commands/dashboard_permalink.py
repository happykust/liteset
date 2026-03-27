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
"""Dashboard permalink command classes."""

from __future__ import annotations

import json  # noqa: TID251
import secrets
from typing import Any, TYPE_CHECKING

from superset.commands.base import AsyncBaseCommand
from superset.exceptions import ObjectNotFoundError

if TYPE_CHECKING:
    from superset.db.daos.key_value import AsyncKeyValueDAO

# Permalink resource uses resource_id=0 as a sentinel — the dashboard_id
# is stored inside the state payload, not as part of the KV lookup key.
_PERMALINK_RESOURCE_ID = 0


class CreateDashboardPermalinkCommand(AsyncBaseCommand[str]):
    def __init__(
        self,
        dao: AsyncKeyValueDAO,
        dashboard_id: int,
        state: dict[str, Any],
        dashboard_uuid: str | None = None,
    ) -> None:
        self._dao = dao
        self._dashboard_id = dashboard_id
        self._state = state
        self._dashboard_uuid = dashboard_uuid

    async def validate(self) -> None:
        pass

    async def run(self) -> str:
        # Use UUID when available; fall back to int id for backwards compat
        dash_id_value: str | int = (
            self._dashboard_uuid if self._dashboard_uuid else self._dashboard_id
        )
        payload = {
            "dashboardId": dash_id_value,
            "state": self._state,
        }
        state_json = json.dumps(payload, sort_keys=True)
        key = secrets.token_urlsafe(16)
        await self._dao.set_value(
            resource="dashboard_permalink",
            resource_id=_PERMALINK_RESOURCE_ID,
            key=key,
            value=state_json,
        )
        return key


class GetDashboardPermalinkCommand(AsyncBaseCommand[dict[str, Any]]):
    def __init__(self, dao: AsyncKeyValueDAO, key: str) -> None:
        self._dao = dao
        self._key = key

    async def validate(self) -> None:
        pass

    async def run(self) -> dict[str, Any]:
        value = await self._dao.get_value(
            resource="dashboard_permalink",
            resource_id=_PERMALINK_RESOURCE_ID,
            key=self._key,
        )
        if value is None:
            raise ObjectNotFoundError("DashboardPermalink", self._key)
        return json.loads(value)
