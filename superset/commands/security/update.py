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
"""Async port of ``superset_old/commands/security/update.py``."""

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
    """Update an existing Row Level Security filter.

    Async port of
    ``superset_old.commands.security.update.UpdateRLSRuleCommand``.
    """

    def __init__(self, dao: Any, model_id: int, data: dict[str, Any]) -> None:
        self._dao = dao
        self._model_id = model_id
        self._properties = dict(data)
        self._tables = (
            list(self._properties["tables"])
            if "tables" in self._properties and self._properties["tables"] is not None
            else None
        )
        self._roles = (
            list(self._properties["roles"])
            if "roles" in self._properties and self._properties["roles"] is not None
            else None
        )
        self._model: Any = None

    async def validate(self) -> None:
        self._model = await self._dao.find_by_id(int(self._model_id))
        if not self._model:
            raise RLSRuleNotFoundError()

        # Only resolve roles/tables when the caller actually sent them —
        # ``RLSPutSchema`` lets clients PATCH a subset of fields.
        if self._roles is not None:
            self._properties["roles"] = await populate_roles(
                self._dao.session, self._roles
            )
        if self._tables is not None:
            # Resolve dataset ids to ``SqlaTable`` objects — inlined from the
            # legacy sync ``UpdateRLSRuleCommand.validate``.  Raises
            # :class:`DatasourceNotFoundValidationError` if any of the
            # requested ids does not exist.
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
