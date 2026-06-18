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
"""Create command for saved queries."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from superset.commands.base import AsyncBaseCommand
from superset.exceptions import CommandInvalidError
from superset.tags.core import add_implicit_tags_after_insert

if TYPE_CHECKING:
    from superset.db.daos.query import AsyncSavedQueryDAO
    from superset.models.sql_lab import SavedQuery


class CreateSavedQueryCommand(AsyncBaseCommand["SavedQuery"]):
    def __init__(
        self,
        dao: AsyncSavedQueryDAO,
        data: dict[str, Any],
        user_id: int | None = None,
    ) -> None:
        self._dao = dao
        self._data = data
        self._user_id = user_id

    async def validate(self) -> None:
        if not self._data.get("label"):
            raise CommandInvalidError("label is required")
        if not self._data.get("sql"):
            raise CommandInvalidError("sql is required")

    async def run(self) -> "SavedQuery":
        from superset.models.sql_lab import SavedQuery

        query = SavedQuery(**self._data)
        if self._user_id is not None:
            query.created_by_fk = self._user_id
            query.changed_by_fk = self._user_id
            # SavedQuery.user_id is a separate FK from AuditMixinNullable.created_by_fk;
            # it backs the ``user`` relationship, ``user_email`` property,
            # and owner tags.
            query.user_id = self._user_id
        self._dao.session.add(query)
        await self._dao.session.flush()

        owner_ids = [self._user_id] if self._user_id is not None else []
        await add_implicit_tags_after_insert(
            self._dao.session,
            "query",
            int(query.id),
            owner_ids,
        )

        return query
