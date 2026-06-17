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
"""Command for updating Row Level Security filters."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from superset.commands.base import AsyncBaseCommand
from superset.commands.utils import populate_roles
from superset.exceptions import (
    DatasourceNotFoundValidationError,
    RLSRuleNotFoundError,
)

logger = logging.getLogger(__name__)


class UpdateRLSRuleCommand(AsyncBaseCommand[Any]):
    """Update an existing Row Level Security filter."""

    def __init__(self, dao: Any, model_id: int, data: dict[str, Any]) -> None:
        self._dao = dao
        self._model_id = model_id
        self._properties = dict(data)
        # Omitted field clears the collection (full-replace semantics,
        # not partial-patch).
        self._tables: list[Any] = list(self._properties.get("tables") or [])
        self._roles: list[Any] = list(self._properties.get("roles") or [])
        self._model: Any = None

    async def validate(self) -> None:
        self._model = await self._dao.find_by_id(int(self._model_id))
        if not self._model:
            raise RLSRuleNotFoundError()

        # Eager-load both M2M relationships before re-assigning them.
        # Without this, ``dao.update``'s ``setattr(model, "roles", [...])``
        # triggers SA's diff-load of the existing collection — a sync SELECT
        # under asyncpg → MissingGreenlet 500.
        # See [[sa-lazy-load-on-transient-asyncpg]].
        await self._dao.session.refresh(self._model, ["roles", "tables"])

        self._properties["roles"] = await populate_roles(self._dao.session, self._roles)

        from superset.models.connectors import SqlaTable

        tables: list[Any] = []
        if self._tables:
            stmt = select(SqlaTable).where(SqlaTable.id.in_(self._tables))
            result = await self._dao.session.execute(stmt)
            tables = list(result.scalars().all())
            if len(tables) != len(self._tables):
                raise DatasourceNotFoundValidationError()
        self._properties["tables"] = tables

    async def run(self) -> Any:
        return await self._dao.update(self._model, self._properties)
