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
"""Tag command classes."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from liteset.commands.base import AsyncBaseCommand
from liteset.exceptions import CommandInvalidError, ObjectNotFoundError

if TYPE_CHECKING:
    from liteset.db.daos.tag import AsyncTagDAO


class CreateTagCommand(AsyncBaseCommand[Any]):
    def __init__(self, dao: AsyncTagDAO, data: dict[str, Any]) -> None:
        self._dao = dao
        self._data = data

    async def validate(self) -> None:
        name = self._data.get("name", "").strip()
        if not name:
            raise CommandInvalidError("name is required")

    async def run(self) -> Any:
        name = self._data["name"]
        description = self._data.get("description", "")
        tag = await self._dao.get_by_name(name, "custom")
        if description:
            tag.description = description  # type: ignore[attr-defined]
        # Create tagged object associations if provided
        objects_to_tag = self._data.get("objects_to_tag", [])
        for obj in objects_to_tag:
            await self._dao.create_custom_tagged_objects(
                object_type=obj["object_type"],
                object_id=obj["object_id"],
                tag_names=[name],
            )
        await self._dao.session.flush()
        return tag


class UpdateTagCommand(AsyncBaseCommand[Any]):
    def __init__(self, dao: AsyncTagDAO, pk: int, data: dict[str, Any]) -> None:
        self._dao = dao
        self._pk = pk
        self._data = data
        self._item: Any = None

    async def validate(self) -> None:
        self._item = await self._dao.find_by_id(self._pk)
        if self._item is None:
            raise ObjectNotFoundError("Tag", self._pk)

    async def run(self) -> Any:
        return await self._dao.update(self._item, self._data)


class DeleteTagCommand(AsyncBaseCommand[None]):
    def __init__(self, dao: AsyncTagDAO, pk: int) -> None:
        self._dao = dao
        self._pk = pk
        self._item: Any = None

    async def validate(self) -> None:
        self._item = await self._dao.find_by_id(self._pk)
        if self._item is None:
            raise ObjectNotFoundError("Tag", self._pk)

    async def run(self) -> None:
        await self._dao.delete([self._item])
        await self._dao.session.flush()


class BulkDeleteTagCommand(AsyncBaseCommand[int]):
    def __init__(self, dao: AsyncTagDAO, ids: list[int]) -> None:
        self._dao = dao
        self._ids = ids

    async def validate(self) -> None:
        if not self._ids:
            raise CommandInvalidError("No IDs provided for bulk delete")

    async def run(self) -> int:
        return await self._dao.bulk_delete(self._ids)


class BulkCreateTagCommand(AsyncBaseCommand[list[Any]]):
    def __init__(self, dao: AsyncTagDAO, tags_data: list[dict[str, Any]]) -> None:
        self._dao = dao
        self._tags_data = tags_data

    async def validate(self) -> None:
        for tag_data in self._tags_data:
            if not tag_data.get("name", "").strip():
                raise CommandInvalidError("All tags must have a name")

    async def run(self) -> list[Any]:
        results = []
        for tag_data in self._tags_data:
            cmd = CreateTagCommand(dao=self._dao, data=tag_data)
            result = await cmd.execute()
            results.append(result)
        return results
