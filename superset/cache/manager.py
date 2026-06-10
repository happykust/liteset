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
"""Async cache manager(s) — Redis-asyncio + Null fallbacks.

Provides:

* :class:`AsyncCacheManager` – a single-cache Redis wrapper with
  ``get`` / ``set`` / ``delete`` / ``has`` / ``get_or_set`` /
  ``clear_prefix`` async methods.
* :class:`NullAsyncCacheManager` – a drop-in no-op replacement used when
  Redis is not configured.
* :class:`SyncRedisCacheAdapter` / :class:`NullSyncCacheManager` – sync
  counterparts used by purely synchronous callers (Celery worker tasks,
  Selenium / Playwright screenshot pipeline) so they can read and
  write the *same* Redis keyspace as the async runtime without paying
  the cost of a sync→async bridge or risking a cross-loop Redis client
  reuse.  The original Apache Superset achieved the same effect with a
  single Flask-Caching ``Cache`` instance shared by the request thread
  and Celery workers; we mirror that here by giving every async slot a
  matching sync sibling slot, both pointing at the *same* Redis
  cluster.
* :class:`CacheManager` – the multi-cache holder that mirrors the
  original ``superset_old.utils.cache_manager.CacheManager`` Flask
  extension.  Exposes ``cache``, ``data_cache``, ``thumbnail_cache``,
  ``filter_state_cache`` and ``explore_form_data_cache`` properties so
  legacy code paths (``utils.cache.memoized_func``, ``viz.py``,
  ``screenshots.py``) keep working unchanged, plus their sync
  counterparts (``sync_thumbnail_cache`` etc.) for Celery / Selenium
  call sites.
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any, Protocol, TypeVar
from uuid import UUID, uuid3

from superset.utils.core import DatasourceType

T = TypeVar("T")

logger = logging.getLogger(__name__)

_CLEAR_PREFIX_BATCH_SIZE = 100
_LOCK_TTL_SECONDS = 30
_LOCK_RETRY_DELAY = 0.05
_LOCK_MAX_RETRIES = 100


# ---------------------------------------------------------------------------
# Interface protocol
# ---------------------------------------------------------------------------


class AsyncCacheProtocol(Protocol):
    """Minimal interface that all async caches expose."""

    async def get(self, key: str) -> Any: ...

    async def set(self, key: str, value: Any, ttl: int | None = None) -> None: ...

    async def delete(self, key: str) -> None: ...

    async def has(self, key: str) -> bool: ...


# ---------------------------------------------------------------------------
# Null / no-op implementation (used when ``redis_url`` is empty)
# ---------------------------------------------------------------------------


class NullAsyncCacheManager:
    """No-op cache used when Redis is not configured.

    Mirrors ``flask_caching.backends.NullCache`` semantics: every read
    returns ``None``, writes are silently dropped.
    """

    def __init__(self, default_ttl: int = 300) -> None:
        self._default_ttl = default_ttl

    async def get(self, key: str) -> Any:  # noqa: ARG002
        return None

    async def set(  # noqa: ARG002
        self, key: str, value: Any, ttl: int | None = None
    ) -> None:
        return None

    async def delete(self, key: str) -> None:  # noqa: ARG002
        return None

    async def has(self, key: str) -> bool:  # noqa: ARG002
        return False

    async def get_or_set(
        self,
        key: str,
        factory: Callable[[], Awaitable[Any]],
        ttl: int | None = None,  # noqa: ARG002
    ) -> Any:
        # No backing store — always recompute.
        del key
        return await factory()

    async def clear_prefix(self, prefix: str) -> int:  # noqa: ARG002
        return 0

    async def close(self) -> None:
        return None


class SimpleAsyncCacheManager:
    """In-process dict-based cache with TTL — async port of
    ``flask_caching.backends.SimpleCache``.

    Mirrors the original behaviour:

    * threshold-bounded dict — once it overflows, the oldest entry is
      evicted (FIFO via ``dict`` insertion order, same as Werkzeug's
      ``SimpleCache``).
    * per-key TTL stored alongside the value; expired reads return
      ``None`` and lazily evict.
    * shared dict between async and sync siblings is **not** required —
      Flask-Caching also gave each cache slot its own backing store; we
      preserve that semantic.
    """

    def __init__(self, default_ttl: int = 300, threshold: int = 500) -> None:
        self._default_ttl = default_ttl
        self._threshold = threshold
        # Stored as ``{key: (expiry_ts | None, value)}``.
        self._store: dict[str, tuple[float | None, Any]] = {}
        self._lock = asyncio.Lock()

    def _is_expired(self, entry: tuple[float | None, Any]) -> bool:
        expiry, _ = entry
        return expiry is not None and expiry < time.time()

    def _prune(self) -> None:
        # Drop expired entries first; if still over threshold, evict
        # in insertion order (oldest first) — mirrors Werkzeug.
        if not self._store:
            return
        for key in list(self._store.keys()):
            if self._is_expired(self._store[key]):
                del self._store[key]
        while len(self._store) > self._threshold:
            self._store.pop(next(iter(self._store)))

    async def get(self, key: str) -> Any:
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            if self._is_expired(entry):
                del self._store[key]
                return None
            return entry[1]

    async def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        async with self._lock:
            ttl_val = ttl if ttl is not None else self._default_ttl
            expiry = time.time() + ttl_val if ttl_val else None
            # Re-insert so insertion order reflects most-recent write
            # (matches Werkzeug's overwrite behaviour).
            self._store.pop(key, None)
            self._store[key] = (expiry, value)
            self._prune()

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._store.pop(key, None)

    async def has(self, key: str) -> bool:
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return False
            if self._is_expired(entry):
                del self._store[key]
                return False
            return True

    async def get_or_set(
        self,
        key: str,
        factory: Callable[[], Awaitable[Any]],
        ttl: int | None = None,
    ) -> Any:
        cached = await self.get(key)
        if cached is not None:
            return cached
        value = await factory()
        await self.set(key, value, ttl=ttl)
        return value

    async def clear_prefix(self, prefix: str) -> int:
        async with self._lock:
            doomed = [k for k in self._store if k.startswith(prefix)]
            for k in doomed:
                del self._store[k]
            return len(doomed)

    async def close(self) -> None:
        async with self._lock:
            self._store.clear()


# ---------------------------------------------------------------------------
# Sync cache adapters (used by Celery / Selenium / Playwright pipelines)
# ---------------------------------------------------------------------------
#
# These mirror :class:`AsyncCacheManager` / :class:`NullAsyncCacheManager`
# but expose a *synchronous* ``get`` / ``set`` / ``delete`` shape and own
# their own (sync) Redis client.  Sharing a Redis client between
# async callers running on the ASGI loop and sync callers running on a
# Celery worker thread would create cross-event-loop awaits that
# ``redis.asyncio`` does not support — every Redis client (sync or
# async) is bound to whatever loop / thread first opened it.  Building
# distinct sync clients keeps the loop topology clean while still
# pointing both clients at the same Redis cluster (operator-configured
# via ``CACHE_REDIS_URL`` / ``CACHE_REDIS_HOST`` etc.) so the keyspace
# stays unified — exactly the behaviour the original Flask Superset
# code relied on.


class SyncCacheProtocol(Protocol):
    """Minimal interface that all sync caches expose.

    Mirrors :class:`AsyncCacheProtocol` exactly except every method is
    synchronous.  ``ttl`` semantics follow Redis ``EX`` (seconds).
    """

    def get(self, key: str) -> Any: ...

    def set(self, key: str, value: Any, ttl: int | None = None) -> None: ...

    def delete(self, key: str) -> None: ...

    def has(self, key: str) -> bool: ...


class NullSyncCacheManager:
    """Synchronous no-op cache, mirrors :class:`NullAsyncCacheManager`.

    Used when Redis is not configured for a given slot.  Reads always
    miss, writes are silently dropped — matches the behaviour of
    ``flask_caching.backends.NullCache`` that the original Apache
    Superset fell back to in the same conditions.
    """

    def __init__(self, default_ttl: int = 300) -> None:
        self._default_ttl = default_ttl

    def get(self, key: str) -> Any:  # noqa: ARG002
        return None

    def set(  # noqa: ARG002
        self, key: str, value: Any, ttl: int | None = None
    ) -> None:
        return None

    def delete(self, key: str) -> None:  # noqa: ARG002
        return None

    def has(self, key: str) -> bool:  # noqa: ARG002
        return False

    def close(self) -> None:
        return None


class SimpleSyncCacheManager:
    """Sync sibling of :class:`SimpleAsyncCacheManager`.

    Backed by a thread-safe dict so Celery worker threads can hit it
    without the asyncio Lock overhead.  Same threshold + TTL semantics
    as ``flask_caching.backends.SimpleCache``.
    """

    def __init__(self, default_ttl: int = 300, threshold: int = 500) -> None:
        self._default_ttl = default_ttl
        self._threshold = threshold
        self._store: dict[str, tuple[float | None, Any]] = {}
        self._lock = threading.Lock()

    def _is_expired(self, entry: tuple[float | None, Any]) -> bool:
        expiry, _ = entry
        return expiry is not None and expiry < time.time()

    def _prune(self) -> None:
        if not self._store:
            return
        for key in list(self._store.keys()):
            if self._is_expired(self._store[key]):
                del self._store[key]
        while len(self._store) > self._threshold:
            self._store.pop(next(iter(self._store)))

    def get(self, key: str) -> Any:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            if self._is_expired(entry):
                del self._store[key]
                return None
            return entry[1]

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        with self._lock:
            ttl_val = ttl if ttl is not None else self._default_ttl
            expiry = time.time() + ttl_val if ttl_val else None
            self._store.pop(key, None)
            self._store[key] = (expiry, value)
            self._prune()

    def delete(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)

    def has(self, key: str) -> bool:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return False
            if self._is_expired(entry):
                del self._store[key]
                return False
            return True

    def close(self) -> None:
        with self._lock:
            self._store.clear()


class SyncRedisCacheAdapter:
    """Sync Redis cache backed by a stdlib ``redis.Redis`` client.

    JSON-encodes values on write and decodes on read so the on-the-wire
    format is identical to what an async caller would see when reading
    the same key — both sides round-trip through ``json.dumps`` /
    ``json.loads`` exactly like Flask-Caching's ``RedisCache`` did
    historically.  ``bytes`` payloads are returned verbatim (legacy
    binary blobs from older deployments).

    All Redis exceptions are caught and logged at WARNING — the cache
    is treated as best-effort, matching the original behaviour.
    """

    def __init__(
        self,
        redis_client: Any,
        *,
        default_ttl: int = 300,
        key_prefix: str = "",
    ) -> None:
        self._redis = redis_client
        self._default_ttl = default_ttl
        self._key_prefix = key_prefix

    def _full_key(self, key: str) -> str:
        return f"{self._key_prefix}{key}" if self._key_prefix else key

    def get(self, key: str) -> Any:
        try:
            raw = self._redis.get(self._full_key(key))
        except Exception:  # noqa: BLE001
            logger.warning("Sync cache get failed for key=%s", key, exc_info=True)
            return None
        if raw is None:
            return None
        if isinstance(raw, (bytes, bytearray)):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError:
                # Legacy binary payload — surface it raw so callers
                # that historically wrote bytes still work.
                return bytes(raw)
        if isinstance(raw, str):
            try:
                return _json.loads(raw)
            except (ValueError, TypeError):
                return raw
        return raw

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        try:
            payload = _json.dumps(value)
        except (TypeError, ValueError):
            logger.warning("Sync cache encode failed for key=%s", key, exc_info=True)
            return None
        ex = ttl if ttl is not None else self._default_ttl
        try:
            self._redis.set(self._full_key(key), payload, ex=ex)
        except Exception:  # noqa: BLE001
            logger.warning("Sync cache set failed for key=%s", key, exc_info=True)
        return None

    def delete(self, key: str) -> None:
        try:
            self._redis.delete(self._full_key(key))
        except Exception:  # noqa: BLE001
            logger.warning("Sync cache delete failed for key=%s", key, exc_info=True)
        return None

    def has(self, key: str) -> bool:
        try:
            return bool(self._redis.exists(self._full_key(key)))
        except Exception:  # noqa: BLE001
            logger.warning("Sync cache has failed for key=%s", key, exc_info=True)
            return False

    def close(self) -> None:
        try:
            self._redis.close()
        except Exception:  # noqa: BLE001 — best effort
            logger.warning("Sync cache close failed", exc_info=True)


class MetastoreSyncCacheManager:
    """Sync sibling of :class:`MetastoreAsyncCacheManager`.

    Used by Celery tasks / Selenium / Playwright code paths that need to
    read or write the metadata-DB-backed cache from a sync context.
    Opens a fresh sync Session via :func:`superset.db.session.get_sync_session`
    on each operation so the caller's transaction lifecycle is not
    coupled to any other Celery task running in the same worker.
    """

    _RESOURCE = "superset_metastore_cache"

    def __init__(
        self,
        namespace: UUID,
        codec: Any,
        default_ttl: int = 300,
        refresh_timeout_on_retrieval: bool = False,
    ) -> None:
        self._namespace = namespace
        self._codec = codec
        self._default_ttl = default_ttl
        self._refresh_timeout_on_retrieval = refresh_timeout_on_retrieval

    def _key_uuid(self, key: str) -> UUID:
        return uuid3(self._namespace, key)

    def _expiry(self, ttl: int | None) -> datetime | None:
        ttl_val = ttl if ttl is not None else self._default_ttl
        if ttl_val and ttl_val > 0:
            return datetime.now() + timedelta(seconds=ttl_val)
        return None

    def _open_session(self) -> Any:
        from superset.db.session import get_sync_session

        return get_sync_session()

    def get(self, key: str) -> Any:
        from sqlalchemy import or_, select

        from superset.models.key_value import KeyValueEntry

        key_uuid = self._key_uuid(key)
        session = self._open_session()
        try:
            stmt = select(KeyValueEntry).where(
                KeyValueEntry.resource == self._RESOURCE,
                KeyValueEntry.uuid == key_uuid,
                or_(
                    KeyValueEntry.expires_on.is_(None),
                    KeyValueEntry.expires_on > datetime.now(),
                ),
            )
            entry = session.execute(stmt).scalars().one_or_none()
            if entry is None:
                return None
            try:
                value = self._codec.decode(entry.value)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Metastore sync cache: failed to decode entry for key %r",
                    key,
                    exc_info=True,
                )
                return None
            if self._refresh_timeout_on_retrieval and self._default_ttl > 0:
                entry.expires_on = self._expiry(None)
                session.commit()
            return value
        finally:
            session.close()

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        from sqlalchemy import select

        from superset.models.key_value import KeyValueEntry

        key_uuid = self._key_uuid(key)
        encoded = self._codec.encode(value)
        session = self._open_session()
        try:
            stmt = select(KeyValueEntry).where(
                KeyValueEntry.resource == self._RESOURCE,
                KeyValueEntry.uuid == key_uuid,
            )
            existing = session.execute(stmt).scalars().one_or_none()
            if existing is not None:
                existing.value = encoded
                existing.expires_on = self._expiry(ttl)
            else:
                session.add(
                    KeyValueEntry(
                        resource=self._RESOURCE,
                        uuid=key_uuid,
                        value=encoded,
                        created_on=datetime.now(),
                        expires_on=self._expiry(ttl),
                    )
                )
            session.commit()
        finally:
            session.close()

    def delete(self, key: str) -> None:
        from sqlalchemy import select

        from superset.models.key_value import KeyValueEntry

        key_uuid = self._key_uuid(key)
        session = self._open_session()
        try:
            stmt = select(KeyValueEntry).where(
                KeyValueEntry.resource == self._RESOURCE,
                KeyValueEntry.uuid == key_uuid,
            )
            entry = session.execute(stmt).scalars().one_or_none()
            if entry is not None:
                session.delete(entry)
                session.commit()
        finally:
            session.close()

    def has(self, key: str) -> bool:
        return self.get(key) is not None

    def close(self) -> None:
        return None


def _build_sync_redis_from_config(
    cache_config: dict[str, Any] | None,
    default_sync_redis: Any | None,
) -> Any | None:
    """Build (or reuse) a sync ``redis.Redis`` client per cache slot.

    Decision tree mirrors :func:`_build_async_redis_from_config`:

    * ``CACHE_REDIS_URL`` → ``redis.Redis.from_url``.
    * ``CACHE_REDIS_HOST`` / ``CACHE_REDIS_PORT`` / ``CACHE_REDIS_DB`` /
      ``CACHE_REDIS_PASSWORD`` → ``redis.Redis(...)``.
    * No connection details → reuse ``default_sync_redis`` (the
      process-wide sync Redis client built in :meth:`CacheManager.init_app`
      from ``settings.redis_url``).
    """
    cfg = cache_config or {}
    redis_url = cfg.get("CACHE_REDIS_URL")
    if redis_url:
        try:
            from redis import Redis as SyncRedis
        except ImportError:
            logger.warning(
                "redis package is not installed; sync cache slot "
                "falling back to NullSyncCacheManager."
            )
            return None
        return SyncRedis.from_url(redis_url)
    if "CACHE_REDIS_HOST" in cfg:
        try:
            from redis import Redis as SyncRedis
        except ImportError:
            logger.warning(
                "redis package is not installed; sync cache slot "
                "falling back to NullSyncCacheManager."
            )
            return None
        kwargs: dict[str, Any] = {
            "host": cfg.get("CACHE_REDIS_HOST", "localhost"),
            "port": cfg.get("CACHE_REDIS_PORT", 6379),
            "db": cfg.get("CACHE_REDIS_DB", 0),
        }
        if cfg.get("CACHE_REDIS_PASSWORD"):
            kwargs["password"] = cfg["CACHE_REDIS_PASSWORD"]
        return SyncRedis(**kwargs)
    return default_sync_redis


def _coerce_threshold(raw: Any) -> int:
    """Coerce a Flask-Caching ``CACHE_THRESHOLD`` value to a finite int.

    ``math.inf`` / ``None`` / non-positive values all map to the
    practical maximum so the FIFO eviction loop effectively never fires.
    Shared by the sync and async slot builders.
    """
    if raw == float("inf") or raw is None:
        return 2**31 - 1
    value = int(raw)
    return value if value > 0 else 2**31 - 1


def _build_sync_metastore_cache(
    cfg: dict[str, Any],
    fallback_default_ttl: int,
) -> SyncCacheProtocol:
    """Build a :class:`MetastoreSyncCacheManager` from a config dict.

    Extracted from :func:`_build_sync_cache_for_slot` to reduce complexity.
    """
    seed = cfg.get("CACHE_KEY_PREFIX", "") or ""
    try:
        from superset.key_value.utils import get_uuid_namespace

        namespace = get_uuid_namespace(seed)
    except Exception:  # noqa: BLE001
        namespace = uuid3(UUID("ee0e7df5-4ce8-4d0a-9b69-3018ea8c2e0c"), seed)
    codec = cfg.get("CODEC")
    if codec is None or not (hasattr(codec, "encode") and hasattr(codec, "decode")):
        from superset.key_value.manager import JsonCodec

        codec = JsonCodec()
    refresh = bool(cfg.get("REFRESH_TIMEOUT_ON_RETRIEVAL", False))
    return MetastoreSyncCacheManager(
        namespace=namespace,
        codec=codec,
        default_ttl=int(cfg.get("CACHE_DEFAULT_TIMEOUT", fallback_default_ttl)),
        refresh_timeout_on_retrieval=refresh,
    )


def _build_sync_cache_for_slot(
    cfg: dict[str, Any] | None,
    default_sync_redis: Any | None,
    *,
    fallback_default_ttl: int = 300,
    key_prefix: str = "",
) -> SyncCacheProtocol:
    """Wire a single sync cache slot from a Flask-Caching-style config.

    Mirrors :func:`_build_cache_for_slot` decisions exactly, just on the
    sync side.  Returns a :class:`NullSyncCacheManager` whenever Redis
    is unavailable or the slot is configured for ``NullCache``.
    """

    def _null_or_default() -> SyncCacheProtocol:
        ttl = fallback_default_ttl
        if default_sync_redis is None:
            return NullSyncCacheManager(default_ttl=ttl)
        return SyncRedisCacheAdapter(
            default_sync_redis, default_ttl=ttl, key_prefix=key_prefix
        )

    if cfg is None or not cfg:
        return _null_or_default()

    cache_type = cfg.get("CACHE_TYPE")
    if cache_type in _NULL_CACHE_TYPES:
        return NullSyncCacheManager(
            default_ttl=int(cfg.get("CACHE_DEFAULT_TIMEOUT", fallback_default_ttl))
        )

    if cache_type in _REDIS_CACHE_TYPES:
        client = _build_sync_redis_from_config(cfg, default_sync_redis)
        if client is None:
            return NullSyncCacheManager(
                default_ttl=int(cfg.get("CACHE_DEFAULT_TIMEOUT", fallback_default_ttl))
            )
        return SyncRedisCacheAdapter(
            client,
            default_ttl=int(cfg.get("CACHE_DEFAULT_TIMEOUT", fallback_default_ttl)),
            key_prefix=key_prefix,
        )

    if cache_type in _SIMPLE_CACHE_TYPES:
        return SimpleSyncCacheManager(
            default_ttl=int(cfg.get("CACHE_DEFAULT_TIMEOUT", fallback_default_ttl)),
            threshold=_coerce_threshold(cfg.get("CACHE_THRESHOLD", 500)),
        )

    if cache_type in _METASTORE_CACHE_TYPES:
        return _build_sync_metastore_cache(cfg, fallback_default_ttl)

    logger.warning(
        "Unsupported CACHE_TYPE %r in sync cache slot config; falling "
        "back to NullSyncCacheManager.",
        cache_type,
    )
    return NullSyncCacheManager(
        default_ttl=int(cfg.get("CACHE_DEFAULT_TIMEOUT", fallback_default_ttl))
    )


# ---------------------------------------------------------------------------
# AsyncCacheManager (single Redis-backed cache)
# ---------------------------------------------------------------------------


class AsyncCacheManager:
    """Async cache manager wrapping a redis.asyncio client."""

    def __init__(self, redis: Any, default_ttl: int = 300) -> None:
        self._redis = redis
        self._default_ttl = default_ttl

    async def get(self, key: str) -> bytes | None:
        try:
            return await self._redis.get(key)
        except Exception:
            logger.warning("Cache get failed for key=%s", key, exc_info=True)
            return None

    async def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        ex = ttl if ttl is not None else self._default_ttl
        try:
            await self._redis.set(key, value, ex=ex)
        except Exception:
            logger.warning("Cache set failed for key=%s", key, exc_info=True)

    async def delete(self, key: str) -> None:
        try:
            await self._redis.delete(key)
        except Exception:
            logger.warning("Cache delete failed for key=%s", key, exc_info=True)

    async def has(self, key: str) -> bool:
        try:
            return bool(await self._redis.exists(key))
        except Exception:
            logger.warning("Cache has failed for key=%s", key, exc_info=True)
            return False

    async def get_or_set(
        self,
        key: str,
        factory: Callable[[], Awaitable[bytes]],
        ttl: int | None = None,
    ) -> bytes:
        """Get a cached value or compute and store it.

        Uses a Redis SET NX lock to prevent thundering herd: only one caller
        executes the factory while others wait and retry for the cached value.
        """
        cached = await self.get(key)
        if cached is not None:
            return cached

        lock_key = f"{key}:lock"
        acquired = False
        try:
            acquired = bool(
                await self._redis.set(lock_key, b"1", nx=True, ex=_LOCK_TTL_SECONDS)
            )
        except Exception:
            logger.warning("Lock acquire failed for key=%s", key, exc_info=True)

        if acquired:
            try:
                value = await factory()
                await self.set(key, value, ttl=ttl)
                return value
            finally:
                try:
                    await self._redis.delete(lock_key)
                except Exception:
                    logger.warning(
                        "Lock release failed for key=%s", lock_key, exc_info=True
                    )
        else:
            # Another caller is computing the value; wait and retry.
            for _ in range(_LOCK_MAX_RETRIES):
                await asyncio.sleep(_LOCK_RETRY_DELAY)
                cached = await self.get(key)
                if cached is not None:
                    return cached

            # Lock holder may have failed; final cache check before fallback.
            cached = await self.get(key)
            if cached is not None:
                return cached
            value = await factory()
            await self.set(key, value, ttl=ttl)
            return value

    async def clear_prefix(self, prefix: str) -> int:
        """Delete all keys matching prefix*. Returns count deleted.

        Uses pipeline to batch deletes for efficiency.
        """
        count = 0
        batch: list[Any] = []
        async for key in self._redis.scan_iter(match=f"{prefix}*"):
            batch.append(key)
            if len(batch) >= _CLEAR_PREFIX_BATCH_SIZE:
                async with self._redis.pipeline(transaction=False) as pipe:
                    for k in batch:
                        pipe.delete(k)
                    await pipe.execute()
                count += len(batch)
                batch.clear()
        if batch:
            async with self._redis.pipeline(transaction=False) as pipe:
                for k in batch:
                    pipe.delete(k)
                await pipe.execute()
            count += len(batch)
        return count

    async def close(self) -> None:
        """Close the underlying Redis connection.

        Uses the version-tolerant helper: ``aclose`` exists on redis-py 5.x,
        but the installed 4.6 only has ``close`` — calling ``aclose`` there
        raises ``AttributeError`` and leaks the slot's connection on shutdown.
        """
        await _close_async_redis(self._redis)


# ---------------------------------------------------------------------------
# ExploreFormDataCache  (ports the legacy key-rewrite logic)
# ---------------------------------------------------------------------------


class MetastoreAsyncCacheManager:
    """Async port of ``superset.extensions.metastore_cache.SupersetMetastoreCache``.

    Stores cache entries in the metadata DB ``key_value`` table, keyed
    by a deterministic UUID3 (``namespace`` ⊕ ``user_supplied_key``).
    Used as the default backend for ``FILTER_STATE_CACHE_CONFIG`` and
    ``EXPLORE_FORM_DATA_CACHE_CONFIG`` — the original Apache Superset
    requires it to operate correctly even when no Redis is configured.

    Each method opens a dedicated AsyncSession via ``session_factory``
    and commits once the DAO call returns; this matches the original
    Flask version's ``db.session.commit()`` after every set/delete.
    Storing a long-lived session on the manager would cross event-loop
    boundaries on shared Litestar workers and lead to overlapping
    transactions.
    """

    # Resource name written to the ``key_value`` row's ``resource``
    # column — must match the original ``KeyValueResource.METASTORE_CACHE``
    # value so existing rows survive an upgrade from upstream Superset.
    _RESOURCE = "superset_metastore_cache"

    def __init__(
        self,
        session_factory: Callable[[], Any],
        namespace: UUID,
        codec: Any,
        default_ttl: int = 300,
        refresh_timeout_on_retrieval: bool = False,
    ) -> None:
        self._session_factory = session_factory
        self._namespace = namespace
        self._codec = codec
        self._default_ttl = default_ttl
        self._refresh_timeout_on_retrieval = refresh_timeout_on_retrieval

    def _key_uuid(self, key: str) -> UUID:
        """Derive the deterministic UUID used in the DB row.

        Mirrors ``SupersetMetastoreCache.get_key`` (uuid3(namespace, key)).
        """
        return uuid3(self._namespace, key)

    def _expiry(self, ttl: int | None) -> datetime | None:
        ttl_val = ttl if ttl is not None else self._default_ttl
        if ttl_val and ttl_val > 0:
            return datetime.now() + timedelta(seconds=ttl_val)
        return None

    async def get(self, key: str) -> Any:
        from superset.db.daos.key_value import AsyncKeyValueDAO

        key_uuid = self._key_uuid(key)
        async with self._session_factory() as session:
            dao = AsyncKeyValueDAO(session)
            entry = await dao.get_entry_by_key(self._RESOURCE, key_uuid)
            if entry is None:
                return None
            # Expiry check -- get_entry_by_key does not filter by expiry
            # (matching original get_entry).  The original get_value does
            # ``if not entry or entry.is_expired(): return None``.
            if entry.expires_on is not None and entry.expires_on <= datetime.now():
                return None
            try:
                value = self._codec.decode(entry.value)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Metastore cache: failed to decode entry for key %r",
                    key,
                    exc_info=True,
                )
                return None
            if self._refresh_timeout_on_retrieval and self._default_ttl > 0:
                # Mirrors the original
                # ``REFRESH_TIMEOUT_ON_RETRIEVAL`` knob: every read
                # extends the entry's TTL by ``default_timeout``.
                entry.expires_on = self._expiry(None)  # type: ignore[assignment]
                await session.commit()
            return value

    async def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        from superset.db.daos.key_value import AsyncKeyValueDAO

        key_uuid = self._key_uuid(key)
        encoded = self._codec.encode(value)
        async with self._session_factory() as session:
            dao = AsyncKeyValueDAO(session)
            existing = await dao.get_entry_by_key(self._RESOURCE, key_uuid)
            if existing is not None:
                existing.value = encoded
                existing.expires_on = self._expiry(ttl)  # type: ignore[assignment]
            else:
                await dao.create_entry(
                    resource=self._RESOURCE,
                    value=encoded,
                    key=key_uuid,
                    expires_on=self._expiry(ttl),
                )
            await session.commit()

    async def delete(self, key: str) -> None:
        from superset.db.daos.key_value import AsyncKeyValueDAO

        key_uuid = self._key_uuid(key)
        async with self._session_factory() as session:
            dao = AsyncKeyValueDAO(session)
            entry = await dao.get_entry_by_key(self._RESOURCE, key_uuid)
            if entry is not None:
                await session.delete(entry)
                await session.commit()

    async def has(self, key: str) -> bool:
        return (await self.get(key)) is not None


class ExploreFormDataCache:
    """Wrapper that rewrites legacy explore-form-data cache entries.

    Mirrors :class:`superset_old.utils.cache_manager.ExploreFormDataCache`:
    when an older payload uses ``dataset_id`` / lacks ``datasource_type``,
    it is upgraded to the new ``datasource_id`` / ``datasource_type``
    schema before being returned to callers.
    """

    def __init__(self, inner: AsyncCacheProtocol | Any) -> None:
        self._inner = inner

    async def get(self, key: str) -> Any:
        cache: Any = await self._inner.get(key)
        if not cache:
            return None
        if isinstance(cache, dict):
            cache = {
                ("datasource_id" if k == "dataset_id" else k): v
                for k, v in cache.items()
            }
            cache.setdefault("datasource_type", DatasourceType.TABLE.value)
        return cache

    async def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        await self._inner.set(key, value, ttl=ttl)

    async def delete(self, key: str) -> None:
        await self._inner.delete(key)

    async def has(self, key: str) -> bool:
        return await self._inner.has(key)


# ---------------------------------------------------------------------------
# CacheManager (multi-cache Flask-extension-style holder)
# ---------------------------------------------------------------------------

# Recognised ``CACHE_TYPE`` values that map to ``NullCache`` semantics
# (writes no-op, reads always miss).  Mirrors the operator-friendly
# aliases accepted by Flask-Caching.
_NULL_CACHE_TYPES = frozenset({None, "NullCache", "null", "NoneType", "none"})

# Recognised ``CACHE_TYPE`` values that map to a Redis backend.
_REDIS_CACHE_TYPES = frozenset(
    {
        "RedisCache",
        "redis",
        "Redis",
        "flask_caching.backends.RedisCache",
        "flask_caching.RedisCache",
    }
)

# Recognised ``CACHE_TYPE`` values that map to an in-process dict-based
# cache (Flask-Caching's ``SimpleCache``).
_SIMPLE_CACHE_TYPES = frozenset(
    {
        "SimpleCache",
        "simple",
        "Simple",
        "flask_caching.backends.SimpleCache",
        "flask_caching.SimpleCache",
    }
)

# Recognised ``CACHE_TYPE`` values that map to the metadata-DB-backed
# cache used by ``FILTER_STATE_CACHE_CONFIG`` /
# ``EXPLORE_FORM_DATA_CACHE_CONFIG``.  Mirrors the original
# ``superset.extensions.metastore_cache.SupersetMetastoreCache``.
_METASTORE_CACHE_TYPES = frozenset(
    {
        "SupersetMetastoreCache",
        "superset.extensions.metastore_cache.SupersetMetastoreCache",
        "MetastoreCache",
    }
)


def _build_async_redis_from_config(
    cache_config: dict[str, Any],
    default_redis: Any | None,
) -> Any | None:
    """Build (or reuse) an ``redis.asyncio.Redis`` client per cache slot.

    Decision tree mirrors Flask-Caching:

    * ``CACHE_REDIS_URL`` → connect via ``redis.asyncio.Redis.from_url``.
    * ``CACHE_REDIS_HOST``/``CACHE_REDIS_PORT``/``CACHE_REDIS_DB``/
      ``CACHE_REDIS_PASSWORD`` → instantiate ``redis.asyncio.Redis``
      with explicit kwargs.
    * No connection details → reuse ``default_redis`` (the process-wide
      Redis client built in :func:`superset.app.on_startup`).
    """
    redis_url = cache_config.get("CACHE_REDIS_URL")
    if redis_url:
        try:
            from redis.asyncio import Redis as AsyncRedis
        except ImportError:
            logger.warning(
                "redis package is not installed; cache slot falling back "
                "to NullAsyncCacheManager."
            )
            return None
        # decode_responses MUST be False: these cache slots store binary
        # values (serialized chart-data DataFrames / query-context forms and
        # thumbnail image bytes). AsyncCacheManager.get returns the raw value
        # verbatim, so a decoding client raises UnicodeDecodeError on binary
        # payloads (which start with byte 0x80) and the read silently None-s.
        return AsyncRedis.from_url(redis_url, decode_responses=False)

    if "CACHE_REDIS_HOST" in cache_config:
        try:
            from redis.asyncio import Redis as AsyncRedis
        except ImportError:
            logger.warning(
                "redis package is not installed; cache slot falling back "
                "to NullAsyncCacheManager."
            )
            return None
        kwargs: dict[str, Any] = {
            "host": cache_config.get("CACHE_REDIS_HOST", "localhost"),
            "port": cache_config.get("CACHE_REDIS_PORT", 6379),
            "db": cache_config.get("CACHE_REDIS_DB", 0),
            # See the from_url branch above: binary cache values require a
            # non-decoding client.
            "decode_responses": False,
        }
        if cache_config.get("CACHE_REDIS_PASSWORD"):
            kwargs["password"] = cache_config["CACHE_REDIS_PASSWORD"]
        return AsyncRedis(**kwargs)

    # NOTE: ``default_redis`` is the process-wide auth-cache client built in
    # ``superset.app.on_startup`` with ``decode_responses=True`` (it stores
    # string user records).  Reusing it here is only safe for slots that store
    # text; the binary cache slots (chart-data / qc-form / thumbnails) would
    # re-trigger the 0x80 UnicodeDecodeError on read.  Today every Redis cache
    # slot sets CACHE_REDIS_HOST so this fallback is not reached for them — if
    # that changes, build a dedicated ``decode_responses=False`` client instead.
    return default_redis


def _build_metastore_cache_from_config(
    cfg: dict[str, Any],
    session_factory: Callable[[], Any] | None,
    fallback_default_ttl: int,
) -> AsyncCacheProtocol:
    """Construct a :class:`MetastoreAsyncCacheManager` for a slot.

    Falls back to :class:`NullAsyncCacheManager` (with a warning) when
    ``session_factory`` is not yet wired — e.g. an ``alembic`` invocation
    that imports the cache manager before the Litestar app has run its
    startup hook.  Mirrors the original behaviour where the metastore
    cache is unusable without a metadata DB session.
    """
    if session_factory is None:
        logger.warning(
            "CACHE_TYPE='SupersetMetastoreCache' requires a session "
            "factory; falling back to NullAsyncCacheManager."
        )
        return NullAsyncCacheManager(
            default_ttl=int(cfg.get("CACHE_DEFAULT_TIMEOUT", fallback_default_ttl))
        )

    seed = cfg.get("CACHE_KEY_PREFIX", "") or ""
    try:
        from superset.key_value.utils import get_uuid_namespace

        namespace = get_uuid_namespace(seed)
    except Exception:  # noqa: BLE001
        namespace = uuid3(UUID("ee0e7df5-4ce8-4d0a-9b69-3018ea8c2e0c"), seed)

    # Honour a user-supplied ``CODEC`` if it exposes encode()/decode().
    # Default to JSON — original Apache Superset configures
    # ``JsonKeyValueCodec`` for both filter_state and explore_form_data,
    # which is the safe choice for untrusted payloads.
    codec = cfg.get("CODEC")
    if codec is None or not (hasattr(codec, "encode") and hasattr(codec, "decode")):
        from superset.key_value.manager import JsonCodec

        codec = JsonCodec()

    refresh = bool(cfg.get("REFRESH_TIMEOUT_ON_RETRIEVAL", False))
    return MetastoreAsyncCacheManager(
        session_factory=session_factory,
        namespace=namespace,
        codec=codec,
        default_ttl=int(cfg.get("CACHE_DEFAULT_TIMEOUT", fallback_default_ttl)),
        refresh_timeout_on_retrieval=refresh,
    )


def _wrap_if_explore(
    inner: AsyncCacheProtocol, is_explore_form_data: bool
) -> AsyncCacheProtocol:
    """Wrap *inner* in :class:`ExploreFormDataCache` when requested."""
    return ExploreFormDataCache(inner) if is_explore_form_data else inner


def _build_async_redis_slot(
    cfg: dict[str, Any],
    default_redis: Any | None,
    fallback_default_ttl: int,
    is_explore_form_data: bool,
) -> AsyncCacheProtocol:
    """Build a Redis async cache slot, falling back to Null on missing client."""
    client = _build_async_redis_from_config(cfg, default_redis)
    if client is None:
        inner: AsyncCacheProtocol = NullAsyncCacheManager(
            default_ttl=int(cfg.get("CACHE_DEFAULT_TIMEOUT", fallback_default_ttl))
        )
    else:
        inner = AsyncCacheManager(
            client,
            default_ttl=int(cfg.get("CACHE_DEFAULT_TIMEOUT", fallback_default_ttl)),
        )
    return _wrap_if_explore(inner, is_explore_form_data)


def _build_cache_for_slot(
    cfg: dict[str, Any] | None,
    default_redis: Any | None,
    *,
    is_explore_form_data: bool = False,
    fallback_default_ttl: int = 300,
    session_factory: Callable[[], Any] | None = None,
) -> AsyncCacheProtocol:
    """Wire a single cache slot from a Flask-Caching-style config dict.

    Honours the original Flask-Caching ``CACHE_TYPE`` semantics:

    * ``NullCache`` (or unset / ``None``) → :class:`NullAsyncCacheManager`.
    * ``RedisCache`` → :class:`AsyncCacheManager` wrapping either an
      explicit per-slot Redis client (built from
      ``CACHE_REDIS_URL`` / ``CACHE_REDIS_HOST`` / etc.) or the
      process-wide default Redis client supplied as ``default_redis``.
    * Any other type → :class:`NullAsyncCacheManager` with a warning,
      so an unsupported config never crashes startup.
    """
    # Slot not configured — fall back to the global default Redis client
    # when one is available, otherwise NullAsyncCacheManager.
    if cfg is None or not cfg:
        ttl = fallback_default_ttl
        if default_redis is None:
            default_inner: AsyncCacheProtocol = NullAsyncCacheManager(default_ttl=ttl)
        else:
            default_inner = AsyncCacheManager(default_redis, default_ttl=ttl)
        return _wrap_if_explore(default_inner, is_explore_form_data)

    cache_type = cfg.get("CACHE_TYPE")
    inner: AsyncCacheProtocol
    if cache_type in _NULL_CACHE_TYPES:
        inner = NullAsyncCacheManager(
            default_ttl=int(cfg.get("CACHE_DEFAULT_TIMEOUT", fallback_default_ttl))
        )
        return _wrap_if_explore(inner, is_explore_form_data)

    if cache_type in _REDIS_CACHE_TYPES:
        return _build_async_redis_slot(
            cfg, default_redis, fallback_default_ttl, is_explore_form_data
        )

    if cache_type in _SIMPLE_CACHE_TYPES:
        # ``math.inf`` is legitimate in Flask-Caching's SimpleCache and is
        # used by the Liteset test config — coerce to a large finite int
        # so the FIFO eviction loop never triggers in practice.
        inner = SimpleAsyncCacheManager(
            default_ttl=int(cfg.get("CACHE_DEFAULT_TIMEOUT", fallback_default_ttl)),
            threshold=_coerce_threshold(cfg.get("CACHE_THRESHOLD", 500)),
        )
        return _wrap_if_explore(inner, is_explore_form_data)

    if cache_type in _METASTORE_CACHE_TYPES:
        inner = _build_metastore_cache_from_config(
            cfg=cfg,
            session_factory=session_factory,
            fallback_default_ttl=fallback_default_ttl,
        )
        return _wrap_if_explore(inner, is_explore_form_data)

    logger.warning(
        "Unsupported CACHE_TYPE %r in cache slot config; falling back to "
        "NullAsyncCacheManager.",
        cache_type,
    )
    inner = NullAsyncCacheManager(
        default_ttl=int(cfg.get("CACHE_DEFAULT_TIMEOUT", fallback_default_ttl))
    )
    return _wrap_if_explore(inner, is_explore_form_data)


def _build_cache(
    redis: Any | None,
    default_ttl: int,
    is_explore_form_data: bool = False,
) -> AsyncCacheProtocol:
    """Legacy single-slot helper kept for back-compat.

    Used by :class:`CacheManager.__init__` to build no-op defaults; the
    multi-slot wiring goes through :func:`_build_cache_for_slot` which
    honours per-slot ``CACHE_REDIS_URL`` etc.
    """
    if redis is None:
        inner: AsyncCacheProtocol = NullAsyncCacheManager(default_ttl=default_ttl)
    else:
        inner = AsyncCacheManager(redis, default_ttl=default_ttl)

    if is_explore_form_data:
        return ExploreFormDataCache(inner)
    return inner


async def _close_async_redis(client: Any | None) -> None:
    """Close an async Redis client (``aclose`` on redis-py 5.x, ``close`` on 4.x)."""
    if client is None:
        return
    try:
        aclose = getattr(client, "aclose", None)
        if aclose is not None:
            await aclose()
        else:
            await client.close()
    except Exception:  # noqa: BLE001
        logger.warning("Async Redis close failed", exc_info=True)


class CacheManager:
    """Multi-cache holder mirroring the original Flask CacheManager.

    Holds five named caches: ``cache`` (default), ``data_cache``,
    ``thumbnail_cache``, ``filter_state_cache`` and
    ``explore_form_data_cache``.  Each cache is a separate
    :class:`AsyncCacheManager` so they can be configured (and cleared)
    independently — exactly like the original.

    By default every cache is a :class:`NullAsyncCacheManager`.  Call
    :meth:`init_app` from :func:`superset.app.on_startup` to wire the
    real Redis client(s).
    """

    def __init__(self) -> None:
        # Default to no-op caches; ``init_app`` swaps them out when Redis
        # connectivity is available.
        self._cache: AsyncCacheProtocol = NullAsyncCacheManager()
        self._data_cache: AsyncCacheProtocol = NullAsyncCacheManager()
        self._thumbnail_cache: AsyncCacheProtocol = NullAsyncCacheManager()
        self._filter_state_cache: AsyncCacheProtocol = NullAsyncCacheManager()
        self._explore_form_data_cache: AsyncCacheProtocol = ExploreFormDataCache(
            NullAsyncCacheManager()
        )
        # Sync siblings — used by Celery / Selenium / Playwright code
        # paths.  Default to no-op so importing this module from CLI /
        # alembic without going through ``init_app`` never raises.
        self._sync_cache: SyncCacheProtocol = NullSyncCacheManager()
        self._sync_data_cache: SyncCacheProtocol = NullSyncCacheManager()
        self._sync_thumbnail_cache: SyncCacheProtocol = NullSyncCacheManager()
        self._sync_filter_state_cache: SyncCacheProtocol = NullSyncCacheManager()
        self._sync_explore_form_data_cache: SyncCacheProtocol = NullSyncCacheManager()
        # Process-wide sync Redis client; built lazily in ``init_app``
        # from ``settings.redis_url``.  Owned by this manager so
        # :meth:`close` can drop it cleanly on shutdown.
        self._default_sync_redis: Any | None = None
        # Process-wide NON-decoding async Redis client
        # (``decode_responses=False``) used as the default for the binary
        # cache slots.  Built in ``init_app`` from ``redis_url`` and owned
        # here so :meth:`close` can drop it.
        self._default_async_redis: Any | None = None

    def init_app(
        self,
        redis: Any | None = None,
        *,
        cache_default_timeout: int = 300,
        cache_config: dict[str, Any] | None = None,
        data_cache_config: dict[str, Any] | None = None,
        thumbnail_cache_config: dict[str, Any] | None = None,
        filter_state_cache_config: dict[str, Any] | None = None,
        explore_form_data_cache_config: dict[str, Any] | None = None,
        sync_redis: Any | None = None,
        redis_url: str | None = None,
        session_factory: Callable[[], Any] | None = None,
    ) -> None:
        """Initialise all five cache slots (async + sync siblings).

        ``redis`` is the *default* async Redis client (the same handle
        that :func:`superset.app.on_startup` builds from
        ``settings.redis_url``).  Each ``*_cache_config`` argument is
        the corresponding Flask-Caching-style settings dict
        (``CACHE_TYPE``, ``CACHE_REDIS_URL``, ``CACHE_DEFAULT_TIMEOUT``
        …).  Per-slot ``CACHE_REDIS_URL`` overrides the default Redis
        client so an operator can point thumbnails at a separate Redis
        cluster from chart-data, exactly like the original Flask
        Superset.

        ``sync_redis`` (optional) is the *default* sync Redis client
        used by Celery / Selenium / Playwright code paths.  When not
        supplied but ``redis_url`` is, we build one ourselves so the
        sync side never has to share the async Redis pool — that share
        would create cross-event-loop awaits, which ``redis.asyncio``
        does not support.  Both clients still point at the same Redis
        cluster, so the keyspace stays unified.

        Pass ``redis=None``, ``sync_redis=None`` and no per-slot
        configs to disable caching entirely.
        """
        # Stash the metadata-DB session factory so any slot using
        # ``CACHE_TYPE='SupersetMetastoreCache'`` can open per-call
        # AsyncSessions on demand.  Held on the manager so reset via a
        # second ``init_app`` call (e.g. tests) updates downstream slots
        # next time they are rebuilt.
        self._session_factory = session_factory

        # The binary cache slots (chart-data DataFrames, qc- query-context
        # forms, thumbnail image bytes) store raw bytes, so their *default*
        # async client MUST NOT decode responses.  The caller's ``redis``
        # handle is the auth-cache client (decode_responses=True, it stores
        # string user records); reusing it as a slot fallback would corrupt
        # binary reads with UnicodeDecodeError on byte 0x80.  Build a dedicated
        # non-decoding client from ``redis_url`` when available; otherwise fall
        # back to ``redis`` (the Celery worker already passes a non-decoding
        # client and no ``redis_url``).
        default_async = redis
        self._default_async_redis = None
        if redis_url:
            try:
                from redis.asyncio import Redis as AsyncRedis

                default_async = AsyncRedis.from_url(redis_url, decode_responses=False)
                self._default_async_redis = default_async
            except ImportError:
                logger.warning(
                    "redis package is not installed; async caches fall back "
                    "to the provided client."
                )
            except Exception:  # noqa: BLE001 — never break startup
                logger.warning(
                    "Failed to build default async Redis client", exc_info=True
                )

        # ---- Async slots ----
        self._cache = _build_cache_for_slot(
            cache_config,
            default_async,
            fallback_default_ttl=cache_default_timeout,
            session_factory=session_factory,
        )
        self._data_cache = _build_cache_for_slot(
            data_cache_config,
            default_async,
            fallback_default_ttl=cache_default_timeout,
            session_factory=session_factory,
        )
        self._thumbnail_cache = _build_cache_for_slot(
            thumbnail_cache_config,
            default_async,
            fallback_default_ttl=cache_default_timeout,
            session_factory=session_factory,
        )
        self._filter_state_cache = _build_cache_for_slot(
            filter_state_cache_config,
            default_async,
            fallback_default_ttl=cache_default_timeout,
            session_factory=session_factory,
        )
        self._explore_form_data_cache = _build_cache_for_slot(
            explore_form_data_cache_config,
            default_async,
            is_explore_form_data=True,
            fallback_default_ttl=cache_default_timeout,
            session_factory=session_factory,
        )

        # ---- Sync siblings ----
        # Build (or reuse) the process-wide sync Redis client.  We
        # prefer the caller-provided handle; otherwise we synthesize
        # one from ``redis_url`` so operators don't have to thread two
        # configs through.
        default_sync = sync_redis
        if default_sync is None and redis_url:
            try:
                from redis import Redis as SyncRedis

                default_sync = SyncRedis.from_url(redis_url)
            except ImportError:
                logger.warning(
                    "redis package is not installed; sync caches will "
                    "fall back to NullSyncCacheManager."
                )
                default_sync = None
            except Exception:  # noqa: BLE001 — never break startup
                logger.warning(
                    "Failed to build default sync Redis client",
                    exc_info=True,
                )
                default_sync = None
        self._default_sync_redis = default_sync

        self._sync_cache = _build_sync_cache_for_slot(
            cache_config,
            default_sync,
            fallback_default_ttl=cache_default_timeout,
        )
        self._sync_data_cache = _build_sync_cache_for_slot(
            data_cache_config,
            default_sync,
            fallback_default_ttl=cache_default_timeout,
        )
        self._sync_thumbnail_cache = _build_sync_cache_for_slot(
            thumbnail_cache_config,
            default_sync,
            fallback_default_ttl=cache_default_timeout,
        )
        self._sync_filter_state_cache = _build_sync_cache_for_slot(
            filter_state_cache_config,
            default_sync,
            fallback_default_ttl=cache_default_timeout,
        )
        self._sync_explore_form_data_cache = _build_sync_cache_for_slot(
            explore_form_data_cache_config,
            default_sync,
            fallback_default_ttl=cache_default_timeout,
        )

    # ---- pass-through to the default cache (so callers can write
    # ``cache_manager.get(...)`` directly, just like the original) ---------
    async def get(self, key: str) -> Any:
        return await self._cache.get(key)

    async def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        await self._cache.set(key, value, ttl=ttl)

    async def delete(self, key: str) -> None:
        await self._cache.delete(key)

    async def has(self, key: str) -> bool:
        return await self._cache.has(key)

    # ---- properties (parity with original Flask CacheManager) -----------
    @property
    def cache(self) -> AsyncCacheProtocol:
        return self._cache

    @property
    def data_cache(self) -> AsyncCacheProtocol:
        return self._data_cache

    @property
    def thumbnail_cache(self) -> AsyncCacheProtocol:
        return self._thumbnail_cache

    @property
    def filter_state_cache(self) -> AsyncCacheProtocol:
        return self._filter_state_cache

    @property
    def explore_form_data_cache(self) -> AsyncCacheProtocol:
        return self._explore_form_data_cache

    # ---- sync siblings (Celery / Selenium / Playwright pipelines) -------
    @property
    def sync_cache(self) -> SyncCacheProtocol:
        return self._sync_cache

    @property
    def sync_data_cache(self) -> SyncCacheProtocol:
        return self._sync_data_cache

    @property
    def sync_thumbnail_cache(self) -> SyncCacheProtocol:
        return self._sync_thumbnail_cache

    @property
    def sync_filter_state_cache(self) -> SyncCacheProtocol:
        return self._sync_filter_state_cache

    @property
    def sync_explore_form_data_cache(self) -> SyncCacheProtocol:
        return self._sync_explore_form_data_cache

    async def close(self) -> None:
        """Close all underlying Redis connections (safe to call repeatedly)."""
        seen: set[int] = set()
        for c in (
            self._cache,
            self._data_cache,
            self._thumbnail_cache,
            self._filter_state_cache,
            self._explore_form_data_cache,
        ):
            target = getattr(c, "_inner", c)  # ExploreFormDataCache wraps inner
            close_fn = getattr(target, "close", None)
            if close_fn is None or id(target) in seen:
                continue
            seen.add(id(target))
            try:
                await close_fn()
            except Exception:  # noqa: BLE001
                logger.warning("Cache close failed", exc_info=True)

        # Tear down the dedicated non-decoding async client we own (a slot may
        # also hold it when it fell back to the default; closing twice is safe).
        await _close_async_redis(self._default_async_redis)

        # Drop sync clients too — sync close is non-awaitable and safe
        # to call from an ``await close()`` chain because Redis sync
        # ``close()`` doesn't block on network I/O (it just returns
        # connections to the pool and shuts the pool down).
        for sc in (
            self._sync_cache,
            self._sync_data_cache,
            self._sync_thumbnail_cache,
            self._sync_filter_state_cache,
            self._sync_explore_form_data_cache,
        ):
            close_fn = getattr(sc, "close", None)
            if close_fn is None:
                continue
            try:
                close_fn()
            except Exception:  # noqa: BLE001
                logger.warning("Sync cache close failed", exc_info=True)
        # Tear down the process-wide sync Redis client we own.
        if self._default_sync_redis is not None:
            try:
                self._default_sync_redis.close()
            except Exception:  # noqa: BLE001
                logger.warning("Default sync Redis close failed", exc_info=True)
            self._default_sync_redis = None


__all__ = [
    "AsyncCacheManager",
    "AsyncCacheProtocol",
    "CacheManager",
    "ExploreFormDataCache",
    "NullAsyncCacheManager",
    "NullSyncCacheManager",
    "SimpleAsyncCacheManager",
    "SimpleSyncCacheManager",
    "SyncCacheProtocol",
    "SyncRedisCacheAdapter",
]
