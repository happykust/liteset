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
"""Synchronous, pickle-backed data cache for the legacy ``viz.py`` pipeline.

The legacy ``BaseViz.get_df_payload`` calls ``data_cache.get(key)`` /
``data_cache.set(key, value, timeout)`` *synchronously* (the original used an
upstream ``RedisCache`` backend). Liteset's :class:`AsyncCacheManager`
slots are coroutine-based, so they cannot be handed to the viz directly. This
module provides a thin **sync** adapter over a blocking ``redis.Redis`` client
that implements the cache contract the viz expects:

* values are pickle-serialized (``RedisCache`` stored pickle bytes), and
* keys are namespaced with the ``superset_cache:`` prefix -- the same literal
  prefix the async ``cache_manager.cache`` read path
  (:func:`superset.common.query_context_processor.load_cached_explore_form`)
  uses, so the explore_json result written by the Celery worker is read back
  verbatim by the web process.

Both the worker (``load_explore_json_into_cache``) and the web controller
(``/superset/explore_json/data/<key>``) build the adapter from the same
upstream-style config dict, so the DataFrame cached during the background
job is found on the cache-first / data-fetch reads.

Pickle is required here (not optional): it is the upstream ``RedisCache``
wire format, and liteset's existing async read paths
(``load_cached_explore_form`` / ``load_cached_query_context_form``) already
``pickle.loads`` these exact cache slots. JSON cannot represent the cached
pandas DataFrame payloads.
"""

from __future__ import annotations

import logging
import pickle  # noqa: S403 - upstream RedisCache parity (pickle wire format)
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# Literal key prefix shared with the async cache read path.  Must match
# ``superset.common.query_context_processor._FLASK_CACHE_KEY_PREFIX``.
CACHE_KEY_PREFIX = "superset_cache:"


class SyncVizCache:
    """Sync ``get``/``set`` cache adapter for the legacy viz pipeline.

    Wraps a blocking ``redis.Redis`` client (``decode_responses=False`` -- values
    are binary pickle). ``get`` returns the unpickled value (or ``None``);
    ``set`` pickles and applies the TTL via ``SETEX``. Keys are transparently
    prefixed with :data:`CACHE_KEY_PREFIX`.
    """

    def __init__(self, redis_client: Any, prefix: str = CACHE_KEY_PREFIX) -> None:
        self._redis = redis_client
        self._prefix = prefix

    def get(self, key: str) -> Any:
        try:
            raw = self._redis.get(f"{self._prefix}{key}")
        except Exception:  # noqa: BLE001 - never break a viz read on cache error
            logger.warning("SyncVizCache get failed for key %s", key, exc_info=True)
            return None
        if raw is None:
            return None
        if isinstance(raw, (bytes, bytearray)):
            try:
                return pickle.loads(raw)  # noqa: S301 - trusted cache payload
            except (pickle.UnpicklingError, TypeError, EOFError, ValueError):
                logger.warning("SyncVizCache failed to unpickle key %s", key)
                return None
        return raw

    def set(self, key: str, value: Any, timeout: int | None = None) -> None:
        # A negative TTL is ``CACHE_DISABLED_TIMEOUT`` ("do not cache") — skip
        # the write entirely (SETEX with a non-positive TTL is a Redis error).
        if timeout is not None and timeout < 0:
            return
        try:
            # Stamp dict payloads with ``dttm`` so ``cached_dttm`` reflects
            # the write time.
            # ``strftime`` (not isoformat) yields the original's naive
            # ``YYYY-MM-DDTHH:MM:SS`` shape — no ``+00:00`` tz suffix.
            if isinstance(value, dict) and "dttm" not in value:
                value = {
                    **value,
                    "dttm": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
                }
            data = pickle.dumps(value)
            full_key = f"{self._prefix}{key}"
            if timeout:  # positive TTL
                self._redis.setex(full_key, int(timeout), data)
            else:  # None or 0 -> store without expiry
                self._redis.set(full_key, data)
        except Exception:  # noqa: BLE001 - caching is best-effort
            logger.warning("SyncVizCache set failed for key %s", key, exc_info=True)


# Connection-keyed instance cache so repeated per-request calls reuse a single
# client (and its connection pool) instead of leaking a new pool each time —
# the sync twin of the worker's module-level ``_get_sync_redis`` singleton.
_INSTANCE_CACHE: dict[Any, SyncVizCache] = {}


def build_sync_viz_cache(
    cache_config: dict[str, Any] | None,
    fallback_redis_url: str | None = None,
) -> SyncVizCache | None:
    """Build (or reuse) a :class:`SyncVizCache` from an upstream cache config.

    Connection-detail resolution mirrors
    :func:`superset.cache.manager._build_async_redis_from_config` (sync twin):

    * ``CACHE_REDIS_URL`` -> ``redis.Redis.from_url``.
    * ``CACHE_REDIS_HOST`` / ``CACHE_REDIS_PORT`` / ``CACHE_REDIS_DB`` /
      ``CACHE_REDIS_PASSWORD`` -> explicit ``redis.Redis(**kwargs)``.
    * Otherwise fall back to ``fallback_redis_url`` (the process-wide
      ``settings.redis_url``).

    Instances are memoized by their resolved connection so the web process does
    not create a fresh Redis connection pool on every request. Returns ``None``
    when no Redis connection can be resolved (caching disabled) or the ``redis``
    package is unavailable, so callers degrade gracefully.
    """
    cfg = cache_config or {}
    if redis_url := cfg.get("CACHE_REDIS_URL"):
        conn_key: Any = ("url", redis_url)
    elif "CACHE_REDIS_HOST" in cfg:
        conn_key = (
            "host",
            cfg.get("CACHE_REDIS_HOST", "localhost"),
            int(cfg.get("CACHE_REDIS_PORT", 6379)),
            int(cfg.get("CACHE_REDIS_DB", 0)),
            cfg.get("CACHE_REDIS_PASSWORD") or None,
        )
    elif fallback_redis_url:
        conn_key = ("url", fallback_redis_url)
    else:
        return None

    if (cached := _INSTANCE_CACHE.get(conn_key)) is not None:
        return cached

    try:
        import redis
    except ImportError:
        logger.warning("redis package is not installed; viz data cache disabled.")
        return None

    if conn_key[0] == "url":
        client: Any = redis.Redis.from_url(conn_key[1], decode_responses=False)
    else:
        _, host, port, db, password = conn_key
        kwargs: dict[str, Any] = {
            "host": host,
            "port": port,
            "db": db,
            "decode_responses": False,
        }
        if password:
            kwargs["password"] = password
        client = redis.Redis(**kwargs)

    instance = SyncVizCache(client)
    _INSTANCE_CACHE[conn_key] = instance
    return instance
