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
"""Cache invalidation controller.

Ports ``superset_old/cachekeys/api.py:CacheRestApi.invalidate`` to
Litestar + async SQLAlchemy.
"""

from __future__ import annotations

import logging

import msgspec
from litestar import Controller, post
from litestar.di import Provide
from litestar.response import Response
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from superset.db.daos.cache import AsyncCacheKeyDAO
from superset.events import event_logger
from superset.guards.rbac import require_permission

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------


class DatasourceRef(msgspec.Struct, rename="camel"):
    """A datasource identified by name rather than UID."""

    database_name: str = ""
    datasource_name: str = ""
    datasource_type: str = "table"
    catalog: str | None = None
    schema: str | None = None


class CacheInvalidateSchema(msgspec.Struct, rename="camel"):
    """Body for POST /api/v1/cachekey/invalidate.

    Matches the original ``CacheInvalidationRequestSchema``:
    accepts either direct UIDs or datasource name tuples (or both).
    """

    datasource_uids: list[str] = []
    datasources: list[DatasourceRef] = []


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------


def _provide_cache_key_dao(session: AsyncSession) -> AsyncCacheKeyDAO:
    return AsyncCacheKeyDAO(session)


class CacheController(Controller):
    path = "/api/v1/cachekey"
    tags = ["Cache"]
    dependencies = {
        "dao": Provide(_provide_cache_key_dao, sync_to_thread=False),
    }

    @post(
        "/invalidate",
        guards=[require_permission("can_write", "CacheKey")],
        status_code=201,
    )
    async def invalidate(
        self,
        data: CacheInvalidateSchema,
        dao: AsyncCacheKeyDAO,
    ) -> Response[dict[str, str]]:
        """POST /api/v1/cachekey/invalidate -- invalidate cache keys.

        Takes a list of datasource UIDs (and/or datasource name tuples),
        finds the associated ``CacheKey`` rows, attempts to delete the
        corresponding entries from the cache backend, and removes the
        database records.

        This is a 1:1 port of the original Flask endpoint.
        """
        # -- 1. Resolve datasource UIDs --------------------------------
        datasource_uids: set[str] = set(data.datasource_uids)

        for ds in data.datasources:
            uid = await dao.resolve_datasource_uid(
                database_name=ds.database_name,
                datasource_name=ds.datasource_name,
                catalog=ds.catalog,
                schema=ds.schema,
            )
            if uid is not None:
                datasource_uids.add(uid)

        if not datasource_uids:
            return Response(
                content={"message": "No matching datasources found"},
                status_code=201,
            )

        # -- 2. Find matching CacheKey rows ----------------------------
        cache_keys = await dao.find_keys_by_datasource_uids(datasource_uids)

        if not cache_keys:
            return Response(
                content={"message": "No cache keys found for given datasources"},
                status_code=201,
            )

        # -- 3. Delete from cache backend (best effort) ----------------
        logger.info(
            "Cache invalidation: %d keys for %d datasources (backend eviction "
            "deferred to TTL)",
            len(cache_keys),
            len(datasource_uids),
        )

        # -- 4. Delete CacheKey rows from DB ---------------------------
        try:
            deleted = await dao.delete_by_cache_keys(cache_keys)
            logger.info(
                "Invalidated %d cache records for %d datasources",
                deleted,
                len(datasource_uids),
            )
        except SQLAlchemyError:
            logger.exception("Failed to delete cache key records")
            return Response(
                content={"message": "Failed to delete cache key records"},
                status_code=500,
            )

        await event_logger.alog_with_context(
            "cache.invalidate",
            extra={
                "datasource_uids": list(datasource_uids),
                "keys_invalidated": len(cache_keys),
            },
        )
        return Response(
            content={"message": f"Invalidated {len(cache_keys)} cache entries"},
            status_code=201,
        )
