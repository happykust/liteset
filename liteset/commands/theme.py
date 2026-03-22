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
"""Theme command classes — business logic for theme CRUD and system default."""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from liteset.commands.base import AsyncBaseCommand
from liteset.exceptions import (
    CommandInvalidError,
    DeleteFailedError,
    ObjectNotFoundError,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from liteset.db.daos.theme import AsyncThemeDAO


class CreateThemeCommand(AsyncBaseCommand[Any]):
    def __init__(
        self,
        dao: AsyncThemeDAO,
        data: dict[str, Any],
    ) -> None:
        self._dao = dao
        self._data = data

    async def validate(self) -> None:
        theme_name = self._data.get("theme_name")
        if not theme_name or not theme_name.strip():
            raise CommandInvalidError("theme_name is required")

    async def run(self) -> Any:
        item = await self._dao.create(self._data)
        await self._dao.session.flush()
        return item


class UpdateThemeCommand(AsyncBaseCommand[Any]):
    def __init__(
        self,
        dao: AsyncThemeDAO,
        pk: int,
        data: dict[str, Any],
    ) -> None:
        self._dao = dao
        self._pk = pk
        self._data = data
        self._model: Any = None

    async def validate(self) -> None:
        self._model = await self._dao.find_by_id(self._pk)
        if not self._model:
            raise ObjectNotFoundError("Theme", self._pk)

    async def run(self) -> Any:
        item = await self._dao.update(self._model, self._data)
        await self._dao.session.flush()
        return item


class DeleteThemeCommand(AsyncBaseCommand[None]):
    def __init__(
        self,
        dao: AsyncThemeDAO,
        pk: int,
    ) -> None:
        self._dao = dao
        self._pk = pk
        self._model: Any = None

    async def validate(self) -> None:
        self._model = await self._dao.find_by_id(self._pk)
        if not self._model:
            raise ObjectNotFoundError("Theme", self._pk)
        # Prevent deletion of the current system default theme
        if getattr(self._model, "is_system_default", False):
            raise DeleteFailedError(
                "Cannot delete the system default theme. "
                "Unset it as default first."
            )

    async def run(self) -> None:
        await self._dao.delete([self._model])
        await self._dao.session.flush()


class SetSystemDefaultCommand(AsyncBaseCommand[Any]):
    """Set a theme as the system default, unsetting the previous default."""

    def __init__(
        self,
        dao: AsyncThemeDAO,
        pk: int,
    ) -> None:
        self._dao = dao
        self._pk = pk
        self._model: Any = None

    async def validate(self) -> None:
        self._model = await self._dao.find_by_id(self._pk)
        if not self._model:
            raise ObjectNotFoundError("Theme", self._pk)

    async def run(self) -> Any:
        # Unset previous system default
        current_default = await self._dao.find_system_default()
        if current_default and current_default.id != self._pk:
            current_default.is_system_default = False

        self._model.is_system_default = True
        await self._dao.session.flush()
        return self._model


class UnsetSystemDefaultCommand(AsyncBaseCommand[None]):
    """Remove the system default flag from the current default theme."""

    def __init__(
        self,
        dao: AsyncThemeDAO,
    ) -> None:
        self._dao = dao

    async def validate(self) -> None:
        pass

    async def run(self) -> None:
        current_default = await self._dao.find_system_default()
        if current_default:
            current_default.is_system_default = False
            await self._dao.session.flush()
