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
"""Update command for saved queries."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from superset.commands.base import AsyncBaseCommand
from superset.exceptions import ObjectNotFoundError
from superset.tags.core import sync_owner_tags_after_update

if TYPE_CHECKING:
    from superset.db.daos.query import AsyncSavedQueryDAO
    from superset.models.sql_lab import SavedQuery


class UpdateSavedQueryCommand(AsyncBaseCommand["SavedQuery"]):
    def __init__(
        self,
        dao: AsyncSavedQueryDAO,
        query_id: int,
        data: dict[str, Any],
        user_id: int | None = None,
        security_manager: Any | None = None,
        user: Any | None = None,
    ) -> None:
        self._dao = dao
        self._query_id = query_id
        self._data = data
        self._user_id = user_id
        self._security_manager = security_manager
        self._user = user
        self._query: Any | None = None

    async def validate(self) -> None:
        self._query = await self._dao.find_by_id(self._query_id)
        if not self._query:
            raise ObjectNotFoundError("SavedQuery", self._query_id)
        await self._assert_in_scope()

    async def _assert_in_scope(self) -> None:
        if self._security_manager is None or self._user is None:
            return
        from superset.db.filters import saved_query_access_filters
        from superset.models.sql_lab import SavedQuery

        base_filters = await saved_query_access_filters(
            self._security_manager, self._user
        )
        if not base_filters:
            return
        accessible = await self._dao.count(
            filters=[SavedQuery.id == self._query_id, *base_filters]
        )
        if not accessible:
            raise ObjectNotFoundError("SavedQuery", self._query_id)

    async def run(self) -> "SavedQuery":
        assert self._query is not None
        for key, value in self._data.items():
            if hasattr(self._query, key):
                setattr(self._query, key, value)
        if self._user_id is not None:
            self._query.changed_by_fk = self._user_id
            # Also update user_id — a separate FK from changed_by_fk that backs
            # the ``user`` relationship and owner: tags.
            self._query.user_id = self._user_id
        await self._dao.session.flush()

        query_user_id = getattr(self._query, "user_id", None)
        owner_ids = [query_user_id] if query_user_id is not None else []
        await sync_owner_tags_after_update(
            self._dao.session, "query", self._query.id, owner_ids
        )

        return self._query
