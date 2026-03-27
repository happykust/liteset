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
"""RLS command classes — business logic for Row Level Security CRUD."""

from __future__ import annotations

import logging
from typing import Any

from superset.commands.base import AsyncBaseCommand
from superset.exceptions import CommandInvalidError, ObjectNotFoundError

logger = logging.getLogger(__name__)


class CreateRLSCommand(AsyncBaseCommand[Any]):
    """Create a new Row Level Security filter."""

    def __init__(self, dao: Any, data: dict[str, Any]) -> None:
        self._dao = dao
        self._data = data

    async def validate(self) -> None:
        name = self._data.get("name")
        if not name or not str(name).strip():
            raise CommandInvalidError("name is required")
        clause = self._data.get("clause")
        if not clause or not str(clause).strip():
            raise CommandInvalidError("clause is required")
        filter_type = self._data.get("filter_type")
        if filter_type and filter_type not in ("Regular", "Base"):
            raise CommandInvalidError("filter_type must be 'Regular' or 'Base'")

    async def run(self) -> Any:
        item = await self._dao.create(self._data)
        return item


class UpdateRLSCommand(AsyncBaseCommand[Any]):
    """Update an existing Row Level Security filter."""

    def __init__(self, dao: Any, pk: int, data: dict[str, Any]) -> None:
        self._dao = dao
        self._pk = pk
        self._data = data
        self._model: Any = None

    async def validate(self) -> None:
        self._model = await self._dao.find_by_id(self._pk)
        if self._model is None:
            raise ObjectNotFoundError("RowLevelSecurityFilter", self._pk)
        filter_type = self._data.get("filter_type")
        if filter_type and filter_type not in ("Regular", "Base"):
            raise CommandInvalidError("filter_type must be 'Regular' or 'Base'")

    async def run(self) -> Any:
        return await self._dao.update(self._model, self._data)


class DeleteRLSCommand(AsyncBaseCommand[None]):
    """Delete a single Row Level Security filter."""

    def __init__(self, dao: Any, pk: int) -> None:
        self._dao = dao
        self._pk = pk
        self._model: Any = None

    async def validate(self) -> None:
        self._model = await self._dao.find_by_id(self._pk)
        if self._model is None:
            raise ObjectNotFoundError("RowLevelSecurityFilter", self._pk)

    async def run(self) -> None:
        await self._dao.delete([self._model])
        await self._dao.session.flush()


class BulkDeleteRLSCommand(AsyncBaseCommand[int]):
    """Bulk delete Row Level Security filters by IDs."""

    def __init__(self, dao: Any, ids: list[int]) -> None:
        self._dao = dao
        self._ids = ids

    async def validate(self) -> None:
        if not self._ids:
            raise CommandInvalidError("No IDs provided for bulk delete")

    async def run(self) -> int:
        return await self._dao.bulk_delete(self._ids)
