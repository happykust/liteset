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
"""Async port of ``superset_old/commands/query/delete.py``."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from superset.commands.base import AsyncBaseCommand
from superset.exceptions import CommandInvalidError, ObjectNotFoundError
from superset.tags.core import delete_tagged_objects

if TYPE_CHECKING:
    from superset.db.daos.query import AsyncSavedQueryDAO


class DeleteSavedQueryCommand(AsyncBaseCommand[None]):
    def __init__(self, dao: AsyncSavedQueryDAO, query_id: int) -> None:
        self._dao = dao
        self._query_id = query_id
        self._query: Any | None = None

    async def validate(self) -> None:
        self._query = await self._dao.find_by_id(self._query_id)
        if not self._query:
            raise ObjectNotFoundError("SavedQuery", self._query_id)

    async def run(self) -> None:
        assert self._query is not None
        query_id = self._query.id
        # Remove implicit tags before deleting (async port of QueryUpdater.after_delete)
        await delete_tagged_objects(self._dao.session, "query", query_id)
        await self._dao.session.delete(self._query)
        await self._dao.session.flush()


class BulkDeleteSavedQueriesCommand(AsyncBaseCommand[None]):
    def __init__(
        self,
        dao: AsyncSavedQueryDAO,
        ids: list[int],
        security_manager: Any | None = None,
        user_id: int | None = None,
    ) -> None:
        self._dao = dao
        self._ids = ids
        self._security_manager = security_manager
        self._user_id = user_id
        self._queries: list[Any] = []

    async def validate(self) -> None:
        if not self._ids:
            raise CommandInvalidError("No saved query IDs provided")
        self._queries = await self._dao.find_by_ids(self._ids)
        found_ids = {int(q.id) for q in self._queries}
        missing = set(self._ids) - found_ids
        if missing:
            raise ObjectNotFoundError("SavedQuery", str(missing))
        if self._security_manager is not None:
            for query in self._queries:
                await self._security_manager.raise_for_ownership(query, self._user_id)

    async def run(self) -> None:
        for q in self._queries:
            await self._dao.session.delete(q)
        await self._dao.session.flush()
