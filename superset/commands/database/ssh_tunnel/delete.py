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
# mypy: ignore-errors
"""Async port of ``superset_old/commands/database/ssh_tunnel/delete.py``."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from superset.commands.base import AsyncBaseCommand
from superset.exceptions import ObjectNotFoundError

if TYPE_CHECKING:
    from superset.db.daos.database import AsyncDatabaseDAO


class DeleteSSHTunnelCommand(AsyncBaseCommand[None]):
    def __init__(
        self,
        dao: AsyncDatabaseDAO,
        database_id: int,
    ) -> None:
        self._dao = dao
        self._database_id = database_id
        self._tunnel: Any = None

    async def validate(self) -> None:
        self._tunnel = await self._dao.get_ssh_tunnel(self._database_id)
        if not self._tunnel:
            raise ObjectNotFoundError("SSHTunnel", self._database_id)

    async def run(self) -> None:
        assert self._tunnel is not None
        await self._dao.session.delete(self._tunnel)
        await self._dao.session.flush()
