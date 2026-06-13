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
from timeit import default_timer
from typing import Any

import msgspec
from litestar import Controller, post
from litestar.di import Provide
from litestar.response import Response
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from superset.db.daos.cache import AsyncCacheKeyDAO
from superset.events import event_logger
from superset.extensions import stats_logger_manager
from superset.guards.rbac import require_permission
from superset.utils.core import DatasourceType

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------


class DatasourceRef(msgspec.Struct, kw_only=True):
    """A datasource identified by name rather than UID.

    NB: NO ``rename="camel"`` — upstream ``Datasource`` (marshmallow) uses
    snake_case wire fields (``database_name``/``datasource_name``/
    ``datasource_type``), which is also what the published OpenAPI spec
    documents. Camel-renaming would silently drop the documented payload.

    ``kw_only=True`` is required here because ``datasource_type`` is a
    required field (no default) that follows optional fields with defaults —
    msgspec mandates kw_only when required fields are not all first.
    """

    database_name: str = ""
    datasource_name: str = ""
    # required; 1:1 with original Marshmallow required=True +
    # validate.OneOf([ds.value for ds in DatasourceType])
    datasource_type: DatasourceType
    catalog: str | None = None
    # ``schema`` has NO allow_none upstream (superset_old/cachekeys/
    # schemas.py:39-41) — an explicit null must be rejected (→ 400), only
    # absence is allowed; ``catalog`` above IS allow_none=True.
    schema: str | msgspec.UnsetType = msgspec.UNSET


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
        # 1:1 with upstream: ``CacheRestApi`` exposes ``invalidate`` with
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
    ) -> Response[Any]:
        """POST /api/v1/cachekey/invalidate -- invalidate cache keys.

        Takes a list of datasource UIDs (and/or datasource name tuples),
        finds the associated ``CacheKey`` rows, attempts to delete the
        corresponding entries from the cache backend, and removes the
        database records.

        This is a 1:1 port of the original endpoint.
        """
        # -- statsd_metrics parity (superset_old/views/base_api.py:112-131) --
        # The original ``@statsd_metrics`` decorator wraps the entire method
        # and emits ``CacheRestApi.invalidate.{success|warning|error}``
        # counters plus ``CacheRestApi.invalidate.time`` timing.
        # We use the original class name ``CacheRestApi`` for metric key
        # continuity with existing monitoring dashboards.
        _metric_prefix = "CacheRestApi.invalidate"
        start = default_timer()
        try:
            response = await self._do_invalidate(data, dao)
        except Exception as ex:
            # 1:1 with @statsd_metrics exception branch
            if hasattr(ex, "status") and ex.status < 500:
                stats_logger_manager.instance.incr(f"{_metric_prefix}.warning")
            else:
                stats_logger_manager.instance.incr(f"{_metric_prefix}.error")
            raise

        # 1:1 with @statsd_metrics send_stats_metrics
        duration_ms = (default_timer() - start) * 1000.0
        # ``Response.status_code`` is Optional in Litestar's typing; every
        # branch above sets it explicitly, so 0 is unreachable.
        status_code = response.status_code or 0
        if 200 <= status_code < 400:
            stats_logger_manager.instance.incr(f"{_metric_prefix}.success")
        elif 400 <= status_code < 500:
            stats_logger_manager.instance.incr(f"{_metric_prefix}.warning")
        else:
            stats_logger_manager.instance.incr(f"{_metric_prefix}.error")
        stats_logger_manager.instance.timing(f"{_metric_prefix}.time", duration_ms)

        return response

    async def _do_invalidate(
        self,
        data: CacheInvalidateSchema,
        dao: AsyncCacheKeyDAO,
    ) -> Response[Any]:
        """Inner body of ``invalidate``, separated for metrics/logging wrapping.

        The event logger fires only on successful (non-exception) return,
        mirroring the original
        ``@event_logger.log_this_with_context(log_to_statsd=False)``
        decorator whose ``log_context`` contextmanager has no ``try/finally``
        around its ``yield`` — so code after ``yield`` (the ``log_with_context``
        call) is never reached when the decorated function raises
        (``superset_old/utils/log.py:271-278``).
        """
        # 1:1 with @event_logger.log_this_with_context(log_to_statsd=False):
        # the original log_context contextmanager only logs when the decorated
        # function returns normally (no try/finally around yield), so we must
        # NOT use a finally block here — we only log on success.
        from datetime import datetime

        start = datetime.now()
        result = await self._invalidate_body(data, dao)
        duration = datetime.now() - start
        # Pass object_ref="CacheRestApi.invalidate" to mirror the original
        # _wrapper computation: ``None or f.__qualname__`` = "CacheRestApi.invalidate"
        # (log_this_with_context with object_ref=None default).
        # Without this, _alog_with_context's ``if object_ref:`` guard is False
        # and the field is absent from logs.json — an admin-visible regression.
        await event_logger.alog_with_context(
            "invalidate",
            duration=duration,
            object_ref="CacheRestApi.invalidate",
            log_to_statsd=False,
        )
        return result

    async def _evict_from_cache_backend(self, cache_keys: list[str]) -> int:
        """Best-effort eviction of *cache_keys* from the cache backend.

        Returns the number of keys successfully deleted.  Never raises —
        failures are logged and the caller continues to the DB cleanup step,
        mirroring the original ``cache_manager.cache.delete_many(*cache_keys)``
        best-effort semantics in ``superset_old/cachekeys/api.py:103``.
        """
        backend_deleted = 0
        try:
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
            logger.debug(
                "Cache backend eviction failed; deferring to TTL", exc_info=True
            )
        return backend_deleted

    async def _invalidate_body(
        self,
        data: CacheInvalidateSchema,
        dao: AsyncCacheKeyDAO,
    ) -> Response[Any]:
        """Core invalidation logic."""
        # -- 1. Resolve datasource UIDs --------------------------------
        datasource_uids: set[str] = set(data.datasource_uids)

        for ds in data.datasources:
            uid = await dao.resolve_datasource_uid(
                database_name=ds.database_name,
                datasource_name=ds.datasource_name,
                catalog=ds.catalog,
                schema=None if ds.schema is msgspec.UNSET else ds.schema,
            )
            if uid is not None:
                datasource_uids.add(uid)

        if not datasource_uids:
            # Original falls through to ``self.response(201)`` (empty body).
            return Response(
                content=b"{}", status_code=201, media_type="application/json"
            )

        # -- 2. Find matching CacheKey rows ----------------------------
        cache_keys = await dao.find_keys_by_datasource_uids(datasource_uids)

        if not cache_keys:
            # Original falls through to ``self.response(201)`` (empty body).
            return Response(
                content=b"{}", status_code=201, media_type="application/json"
            )

        # -- 3. Actively evict keys from cache backend -----------------
        backend_deleted = await self._evict_from_cache_backend(cache_keys)

        logger.info(
            "Cache invalidation: %d/%d keys evicted from backend for %d datasources",
            backend_deleted,
            len(cache_keys),
            len(datasource_uids),
        )

        # -- 4. Delete CacheKey rows from DB ---------------------------
        try:
            deleted = await dao.delete_by_cache_keys(cache_keys)
            # 1:1 with upstream: emit the invalidation gauge on success.
            stats_logger_manager.instance.gauge("invalidated_cache", len(cache_keys))
            logger.info(
                "Invalidated %d cache records for %d datasources",
                deleted,
                len(datasource_uids),
            )
        except SQLAlchemyError as ex:
            # 1:1 with superset_old/cachekeys/api.py:128-131:
            #   ``db.session.rollback(); logger.error(ex, exc_info=True);
            #   return self.response_500(str(ex))``
            # The upstream response_500 returns {"message": str(ex)}, so we must
            # pass the actual exception text — not a fixed string — to match
            # the client-visible response body.
            await dao.session.rollback()
            logger.error(ex, exc_info=True)
            return Response(
                content={"message": str(ex)},
                status_code=500,
            )

        # 1:1 with the original ``return self.response(201)`` — empty body.
        return Response(content=b"{}", status_code=201, media_type="application/json")
