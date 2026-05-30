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


class DatasourceRef(msgspec.Struct):
    """A datasource identified by name rather than UID.

    NB: NO ``rename="camel"`` — upstream ``Datasource`` (marshmallow) uses
    snake_case wire fields (``database_name``/``datasource_name``/
    ``datasource_type``), which is also what the published OpenAPI spec
    documents. Camel-renaming would silently drop the documented payload.
    """

    database_name: str = ""
    datasource_name: str = ""
    datasource_type: str = "table"
    catalog: str | None = None
    schema: str | None = None


class CacheInvalidateSchema(msgspec.Struct):
    """Body for POST /api/v1/cachekey/invalidate.

    1:1 with the original ``CacheInvalidationRequestSchema`` — accepts either
    direct UIDs or datasource name tuples (or both). The wire fields are
    snake_case (``datasource_uids``/``datasources``); do NOT add
    ``rename="camel"`` — upstream + the OpenAPI spec use snake_case, and
    camel-renaming silently drops a correctly-formed ``datasource_uids`` body
    (→ 201 invalidating nothing).
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
        # 1:1 with FAB: ``CacheRestApi`` exposes ``invalidate`` with
        # ``class_permission_name = "CacheRestApi"`` and no
        # ``method_permission_name`` override, so the resolved permission is
        # ``("can_invalidate", "CacheRestApi")``
        # (``superset_old/cachekeys/api.py:40-52``).
        guards=[require_permission("can_invalidate", "CacheRestApi")],
        status_code=201,
    )
    async def invalidate(
        self,
        data: CacheInvalidateSchema,
        dao: AsyncCacheKeyDAO,
    ) -> Response[None]:
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
            # Original falls through to ``self.response(201)`` (empty body).
            return Response(content=b"", status_code=201, media_type="application/json")

        # -- 2. Find matching CacheKey rows ----------------------------
        cache_keys = await dao.find_keys_by_datasource_uids(datasource_uids)

        if not cache_keys:
            # Original falls through to ``self.response(201)`` (empty body).
            return Response(content=b"", status_code=201, media_type="application/json")

        # -- 3. Actively evict keys from cache backend -----------------
        # Mirrors the original Flask ``cache_manager.cache.delete_many(*cache_keys)``
        # call in ``superset_old/cachekeys/api.py:103``.  We iterate and call
        # ``delete`` per key because the async cache protocol exposes a single-key
        # ``delete`` method (no ``delete_many`` batch endpoint on the protocol).
        # Best-effort: log misses but continue to the DB cleanup step.
        backend_deleted = 0
        try:
            # Try to get the async cache manager from the app state
            _cache_manager = None
            try:
                # The process-wide cache_manager singleton (set up in extensions.py)
                from superset.extensions import cache_manager as _global_cm

                _cache_manager = _global_cm
            except (ImportError, AttributeError):
                pass

            if _cache_manager is not None:
                for cache_key in cache_keys:
                    try:
                        await _cache_manager.cache.delete(cache_key)
                        backend_deleted += 1
                    except Exception:  # noqa: BLE001
                        logger.debug(
                            "Cache backend delete failed for key %s", cache_key
                        )
            else:
                logger.info(
                    "Cache manager not available; backend eviction deferred to TTL"
                )
        except Exception:  # noqa: BLE001
            logger.debug("Cache backend eviction failed; deferring to TTL", exc_info=True)

        logger.info(
            "Cache invalidation: %d/%d keys evicted from backend for %d datasources",
            backend_deleted,
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
        # 1:1 with the original ``return self.response(201)`` — empty body.
        return Response(content=b"", status_code=201, media_type="application/json")
