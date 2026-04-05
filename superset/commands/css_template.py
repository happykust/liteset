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
"""CSS Template command classes — business logic for CSS template CRUD."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from superset.commands.base import AsyncBaseCommand
from superset.exceptions import CommandInvalidError, ObjectNotFoundError

if TYPE_CHECKING:
    pass


class CreateCssTemplateCommand(AsyncBaseCommand[Any]):
    """Create a new CSS template."""

    def __init__(self, dao: Any, data: dict[str, Any]) -> None:
        self._dao = dao
        self._data = data

    async def validate(self) -> None:
        if not self._data.get("template_name", "").strip():
            raise CommandInvalidError("template_name is required")

    async def run(self) -> Any:
        result = await self._dao.create(self._data)
        await self._dao.session.flush()
        return result


class UpdateCssTemplateCommand(AsyncBaseCommand[Any]):
    """Update an existing CSS template."""

    def __init__(self, dao: Any, pk: int, data: dict[str, Any]) -> None:
        self._dao = dao
        self._pk = pk
        self._data = data
        self._template: Any | None = None

    async def validate(self) -> None:
        self._template = await self._dao.find_by_id(self._pk)
        if not self._template:
            raise ObjectNotFoundError("CssTemplate", self._pk)

    async def run(self) -> Any:
        assert self._template is not None
        result = await self._dao.update(self._template, self._data)
        await self._dao.session.flush()
        return result


class DeleteCssTemplateCommand(AsyncBaseCommand[None]):
    """Delete a single CSS template."""

    def __init__(self, dao: Any, pk: int) -> None:
        self._dao = dao
        self._pk = pk
        self._template: Any | None = None

    async def validate(self) -> None:
        self._template = await self._dao.find_by_id(self._pk)
        if not self._template:
            raise ObjectNotFoundError("CssTemplate", self._pk)

    async def run(self) -> None:
        assert self._template is not None
        await self._dao.delete([self._template])
        await self._dao.session.flush()


class BulkDeleteCssTemplateCommand(AsyncBaseCommand[None]):
    """Bulk-delete CSS templates by IDs."""

    def __init__(self, dao: Any, ids: list[int]) -> None:
        self._dao = dao
        self._ids = ids
        self._templates: list[Any] = []

    async def validate(self) -> None:
        if not self._ids:
            raise CommandInvalidError("No CSS template IDs provided")
        self._templates = await self._dao.find_by_ids(self._ids)
        found_ids = {int(t.id) for t in self._templates}
        missing = set(self._ids) - found_ids
        if missing:
            raise ObjectNotFoundError("CssTemplate", str(missing))

    async def run(self) -> None:
        await self._dao.delete(self._templates)
        await self._dao.session.flush()
