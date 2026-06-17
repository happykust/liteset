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
"""Command for creating Row Level Security filters."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from superset.commands.base import AsyncBaseCommand
from superset.commands.utils import populate_roles
from superset.exceptions import DatasourceNotFoundValidationError

logger = logging.getLogger(__name__)


class CreateRLSRuleCommand(AsyncBaseCommand[Any]):
    """Create a new Row Level Security filter."""

    def __init__(self, dao: Any, data: dict[str, Any]) -> None:
        self._dao = dao
        self._properties = dict(data)
        self._tables = list(self._properties.get("tables") or [])
        self._roles = list(self._properties.get("roles") or [])

    async def validate(self) -> None:
        roles = await populate_roles(self._dao.session, self._roles)
        tables: list[Any] = []
        if self._tables:
            from superset.models.connectors import SqlaTable

            stmt = select(SqlaTable).where(SqlaTable.id.in_(self._tables))
            result = await self._dao.session.execute(stmt)
            tables = list(result.scalars().all())
            if len(tables) != len(self._tables):
                raise DatasourceNotFoundValidationError()
        self._properties["roles"] = roles
        self._properties["tables"] = tables

    async def run(self) -> Any:
        item = await self._dao.create(self._properties)
        # Flush so the autoincrement ``id`` is populated before the controller builds
        # the ``{"id": item.id, "result": …}`` response — without this, id is null.
        await self._dao.session.flush()
        return item
