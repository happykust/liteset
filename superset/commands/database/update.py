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
"""Async port of ``superset_old/commands/database/update.py``."""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from superset.commands.base import AsyncBaseCommand
from superset.exceptions import CommandInvalidError, ObjectNotFoundError

if TYPE_CHECKING:
    from superset.db.daos.database import AsyncDatabaseDAO
    from superset.models.core import Database

logger = logging.getLogger(__name__)


class UpdateDatabaseCommand(AsyncBaseCommand["Database"]):
    def __init__(
        self,
        dao: AsyncDatabaseDAO,
        database_id: int,
        data: dict[str, Any],
        user_id: int | None = None,
    ) -> None:
        self._dao = dao
        self._database_id = database_id
        self._data = data
        self._user_id = user_id
        self._database: Any | None = None

    async def validate(self) -> None:
        self._database = await self._dao.find_by_id(self._database_id)
        if not self._database:
            raise ObjectNotFoundError("Database", self._database_id)

        new_name = self._data.get("database_name")
        if new_name:
            is_unique = await self._dao.validate_update_uniqueness(
                self._database_id,
                new_name,
            )
            if not is_unique:
                raise CommandInvalidError(
                    f'A database with the name "{new_name}" already exists'
                )

    async def run(self) -> "Database":
        assert self._database is not None

        # --- unmask_encrypted_extra ----------------------------------------
        # The PUT request may contain ``masked_encrypted_extra`` — a version of
        # ``encrypted_extra`` where sensitive fields (private keys, passwords,
        # etc.) are replaced with the "XXXXXXXXXX" sentinel by the
        # ``mask_encrypted_extra`` classmethod on the engine spec.
        #
        # Mirrors ``superset_old/commands/database/update.py`` lines 70-77:
        #   if "masked_encrypted_extra" in self._properties:
        #       self._properties["encrypted_extra"] = (
        #           self._model.db_engine_spec.unmask_encrypted_extra(
        #               self._model.encrypted_extra,
        #               self._properties.pop("masked_encrypted_extra"),
        #           )
        #       )
        #
        # Without this step the masked placeholders would be written verbatim
        # to the database, permanently destroying the real credentials.
        if "masked_encrypted_extra" in self._data:
            self._data["encrypted_extra"] = (
                self._database.db_engine_spec.unmask_encrypted_extra(
                    self._database.encrypted_extra,
                    self._data.pop("masked_encrypted_extra"),
                )
            )

        for key, value in self._data.items():
            if hasattr(self._database, key):
                setattr(self._database, key, value)
        if self._user_id is not None:
            self._database.changed_by_fk = self._user_id
        await self._dao.session.flush()
        return self._database
