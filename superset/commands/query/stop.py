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
"""Stop-query command.

LITESET ADDITION (no 1:1 counterpart in
``superset_old/commands/query/``).  Apache Superset 6.0 calls
``QueryDAO.stop_query(client_id)`` directly inline in
``superset_old/queries/api.py::stop_query``; no Command class wraps the
call.  In Liteset the same logic lives here so the Litestar controller
stays thin.  File name kept short to match the single-verb convention in
``superset_old/commands/query/`` (``delete.py``, ``export.py``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from superset.commands.base import AsyncBaseCommand
from superset.exceptions import CommandInvalidError, ObjectNotFoundError

if TYPE_CHECKING:
    from superset.db.daos.query import AsyncQueryDAO


class StopQueryCommand(AsyncBaseCommand[None]):
    def __init__(self, dao: AsyncQueryDAO, client_id: str) -> None:
        self._dao = dao
        self._client_id = client_id

    async def validate(self) -> None:
        if not self._client_id:
            raise CommandInvalidError("client_id is required")

    async def run(self) -> None:
        query = await self._dao.stop_query(self._client_id)
        if query is None:
            raise ObjectNotFoundError("Query", self._client_id)
