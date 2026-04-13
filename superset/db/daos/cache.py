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
from __future__ import annotations

from typing import Any

from sqlalchemy import delete as sa_delete, select

from superset.db.base_dao import BaseAsyncDAO
from superset.models.cache import CacheKey
from superset.models.connectors import SqlaTable
from superset.models.core import Database


class AsyncCacheKeyDAO(BaseAsyncDAO[CacheKey]):
    model_cls = CacheKey

    async def resolve_datasource_uid(
        self,
        database_name: str,
        datasource_name: str,
        catalog: str | None,
        schema: str | None,
    ) -> str | None:
        """Find the UID of a datasource matching the given name tuple."""
        stmt = (
            select(SqlaTable)
            .join(Database, SqlaTable.database_id == Database.id)
            .where(
                SqlaTable.table_name == datasource_name,
                Database.database_name == database_name,
                SqlaTable.catalog == catalog,
            )
        )
        result = await self.session.execute(stmt)
        candidates = result.scalars().all()

        normalized_schema = schema or None
        for tbl in candidates:
            if normalized_schema == (tbl.schema or None):
                return str(tbl.uid)
        return None

    async def find_keys_by_datasource_uids(
        self, datasource_uids: set[str]
    ) -> list[str]:
        """Return cache_key values for the given datasource UIDs."""
        if not datasource_uids:
            return []
        result = await self.session.execute(
            select(CacheKey.cache_key).where(
                CacheKey.datasource_uid.in_(datasource_uids)
            )
        )
        return [row[0] for row in result.all()]

    async def delete_by_cache_keys(self, cache_keys: list[str]) -> int:
        """Delete CacheKey rows matching the given cache keys. Returns count."""
        if not cache_keys:
            return 0
        result: Any = await self.session.execute(
            sa_delete(CacheKey).where(CacheKey.cache_key.in_(cache_keys))
        )
        await self.session.flush()
        return result.rowcount
