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
from typing import Any, TYPE_CHECKING

from superset.commands.base import AsyncBaseCommand
from superset.commands.dashboard.filter_state.utils import check_access
from superset.exceptions import ObjectNotFoundError

if TYPE_CHECKING:
    from superset.db.daos.key_value import AsyncKeyValueDAO


class GetFilterStateCommand(AsyncBaseCommand[str]):
    def __init__(
        self,
        dao: AsyncKeyValueDAO,
        dashboard_id: int,
        key: str,
        security_manager: Any | None = None,
        user_id: int | None = None,
    ) -> None:
        self._dao = dao
        self._dashboard_id = dashboard_id
        self._key = key
        self._security_manager = security_manager
        self._user_id = user_id

    async def validate(self) -> None:
        await check_access(
            self._dao,
            self._dashboard_id,
            security_manager=self._security_manager,
            user=None,
        )

    async def run(self) -> str:
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
            if isinstance(entry, dict) and "value" in entry:
                return entry["value"]
        except (json.JSONDecodeError, TypeError):
            pass
        return raw
