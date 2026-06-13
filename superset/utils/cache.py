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
"""Async cache utilities — port of ``superset_old/utils/cache.py``.

Replaces the upstream caching primitives with async, cache-agnostic helpers:

* :func:`generate_cache_key` — deterministic ``md5`` hashing (unchanged).
* :func:`set_and_log_cache` — write to cache + optional ``CacheKey`` row
  in the metadata DB (now async, uses an :class:`AsyncSession`).
* :func:`memoized_func` — **async-only** decorator that caches coroutine
  results by formatted-key (mirrors the original signature, including
  the ``cache=False`` / ``force=True`` / ``cache_timeout=`` kwargs).
  Sync callers should use :attr:`CacheManager.sync_cache` (or one of
  the named sibling slots) directly — the previous sync path went
  through ``run_async`` which created a fresh asyncio loop on the
  worker thread and reused the async Redis client built on the main
  loop, breaking with a cross-loop ``RuntimeError`` whenever Redis I/O
  actually fired.  Splitting sync and async cache traffic onto
  independent Redis clients (both pointed at the same cluster — see
  :class:`CacheManager`) cleanly removes that whole class of bug.
* :func:`etag_cache` — *removed*.  Litestar provides native ``Cache``
  config and ``ETag`` middleware; the original response-cache
  decorator is no longer used anywhere in the Liteset code base
  (verified by grep).  The placeholder remains commented in
  documentation.

The legacy WSGI caching stack is no longer imported.
"""

from __future__ import annotations

import functools
import inspect
import logging
from datetime import datetime, timezone
from typing import Any, Callable, TYPE_CHECKING

from superset.constants import CACHE_DISABLED_TIMEOUT
from superset.utils.hashing import md5_sha_from_dict
from superset.utils.json import json_int_dttm_ser

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from superset.cache.manager import AsyncCacheProtocol

logger = logging.getLogger(__name__)

ONE_YEAR = 365 * 24 * 60 * 60  # 1 year in seconds


# ---------------------------------------------------------------------------
# generate_cache_key — pure function, ported 1:1
# ---------------------------------------------------------------------------


def generate_cache_key(values_dict: dict[str, Any], key_prefix: str = "") -> str:
    """Return a deterministic md5 hash of ``values_dict`` prefixed by
    ``key_prefix``.

    Identical to the original implementation; the only dependency change
    is that ``md5_sha_from_dict`` and ``json_int_dttm_ser`` now live
    under ``superset.utils.*`` (already async-friendly).
    """
    hash_str = md5_sha_from_dict(values_dict, default=json_int_dttm_ser)
    return f"{key_prefix}{hash_str}"


# ---------------------------------------------------------------------------
# set_and_log_cache — async port
# ---------------------------------------------------------------------------


def _resolve_cache_default_timeout() -> int:
    """Return ``cache_default_timeout`` from settings (lazy import).

    Caches the value via :func:`functools.lru_cache` so hot paths don't
    repeatedly instantiate :class:`SupersetSettings`.
    """
    return _cached_default_timeout()


@functools.lru_cache(maxsize=1)
def _cached_default_timeout() -> int:
    from superset.config import SupersetSettings

    settings = SupersetSettings()  # type: ignore[call-arg]
    return int(settings.cache_default_timeout)


@functools.lru_cache(maxsize=1)
def _cached_store_cache_keys() -> bool:
    from superset.config import SupersetSettings

    settings = SupersetSettings()  # type: ignore[call-arg]
    return bool(settings.store_cache_keys_in_metadata_db)


async def set_and_log_cache(
    cache: AsyncCacheProtocol,
    cache_key: str,
    cache_value: dict[str, Any],
    cache_timeout: int | None = None,
    datasource_uid: str | None = None,
    *,
    session: AsyncSession | None = None,
) -> None:
    """Write ``cache_value`` to ``cache`` and (optionally) record the key
    in the ``cache_keys`` metadata table.

    Mirrors the original :func:`superset_old.utils.cache.set_and_log_cache`:
    short-circuits when the cache is a no-op backend, respects
    ``CACHE_DISABLED_TIMEOUT``, decorates ``cache_value`` with a ``dttm``
    field, increments the ``set_cache_key`` stats counter and writes a
    ``CacheKey`` row when both ``datasource_uid`` and the
    ``store_cache_keys_in_metadata_db`` setting are truthy.

    Differences from the original:

    * ``cache`` is an :class:`AsyncCacheProtocol` rather than the upstream
      ``Cache``; the no-op detection now checks the imported
      :class:`NullAsyncCacheManager` class instead of ``NullCache``.
    * Persistence of ``CacheKey`` rows is performed via the supplied
      :class:`AsyncSession` (callers that don't pass a session simply
      skip the metadata-DB write).
    """
    # Lazy import to avoid a circular dependency: this module is imported
    # by ``superset.extensions``.
    from superset.cache.manager import NullAsyncCacheManager
    from superset.extensions import stats_logger_manager

    if isinstance(cache, NullAsyncCacheManager):
        return

    timeout = (
        cache_timeout if cache_timeout is not None else _resolve_cache_default_timeout()
    )

    # Skip caching if timeout is CACHE_DISABLED_TIMEOUT (no caching requested)
    if timeout == CACHE_DISABLED_TIMEOUT:
        return
    try:
        dttm = datetime.now(tz=timezone.utc).isoformat().split(".")[0]
        value = {**cache_value, "dttm": dttm}
        await cache.set(cache_key, value, ttl=timeout)
        stats_logger_manager.instance.incr("set_cache_key")

        if datasource_uid and _cached_store_cache_keys():
            from superset.models.cache import CacheKey

            ck = CacheKey(
                cache_key=cache_key,
                cache_timeout=cache_timeout,
                datasource_uid=datasource_uid,
            )
            if session is not None:
                # Caller-provided AsyncSession: defer commit to the
                # surrounding ``@transaction`` (or middleware) so the
                # cache-key row participates in the same transaction as
                # the rest of the request.
                session.add(ck)
            else:
                # No session passed in -- fall back to a short-lived
                # *sync* metadata session.  Mirrors the original
                # behaviour where ``db.session`` was a process-wide
                # implicit handle: missing one would silently drop the
                # audit trail, which is exactly what we want to avoid.
                from sqlalchemy.orm import Session

                from superset.utils.rls import _metadata_sync_engine

                engine = _metadata_sync_engine()
                fallback_session = Session(engine)
                try:
                    fallback_session.add(ck)
                    fallback_session.commit()
                except Exception:  # noqa: BLE001
                    fallback_session.rollback()
                    logger.warning(
                        "Failed to persist CacheKey row for %s",
                        cache_key,
                        exc_info=True,
                    )
                finally:
                    fallback_session.close()
    except Exception as ex:  # noqa: BLE001
        # cache.set call can fail if the backend is down or if
        # the key is too large or whatever other reasons.
        logger.warning("Could not cache key %s", cache_key)
        logger.exception(ex)


# ---------------------------------------------------------------------------
# memoized_func — sync- and async-aware decorator
# ---------------------------------------------------------------------------


def memoized_func(  # noqa: C901  # complex business logic
    key: str,
    cache: AsyncCacheProtocol | None = None,
) -> Callable[..., Any]:
    """Async-only memoization decorator with configurable key and backend.

    Same calling convention as the upstream caching helper::

        @memoized_func(key="{a}+{b}", cache=cache_manager.data_cache)
        async def my_sum(a: int, b: int) -> int:
            return a + b

    Recognised keyword arguments on the decorated call:

    * ``cache`` — when ``False`` skips the cache entirely.
    * ``force`` — when ``True`` recomputes and overwrites the cached value.
    * ``cache_timeout`` — overrides ``settings.cache_default_timeout``;
      pass ``CACHE_DISABLED_TIMEOUT`` (``-1``) to skip writing to the
      cache while still serving cached reads.

    The wrapped function **must** be a coroutine function.  Decorating a
    plain sync function raises :class:`TypeError` immediately at decoration
    time — the previous sync path bridged through
    :func:`superset.utils.async_bridge.run_async`, which on a worker
    thread spawned by :func:`asyncio.to_thread` would spin up a fresh
    asyncio loop and try to await an async-Redis client whose
    connection pool was bound to the *parent* loop, producing
    cross-event-loop ``RuntimeError`` /
    ``concurrent.futures.InvalidStateError`` failures the moment Redis
    I/O actually fired.

    Sync callers (Celery worker tasks, CLI, alembic migrations) should
    use :attr:`superset.cache.manager.CacheManager.sync_cache` (or one
    of the named sibling slots: ``sync_data_cache`` /
    ``sync_thumbnail_cache`` / etc.) directly.  Both the sync and
    async slots point at the same Redis cluster, so the keyspace
    stays unified — exactly the behaviour the original Superset
    relied on, just with the loop topology made explicit.
    """

    def wrap(f: Callable[..., Any]) -> Callable[..., Any]:
        if not inspect.iscoroutinefunction(f):
            raise TypeError(
                f"@memoized_func requires an async (coroutine) function; "
                f"{getattr(f, '__qualname__', repr(f))!r} is synchronous. "
                "Use ``cache_manager.sync_cache`` (or a named sibling like "
                "``sync_data_cache`` / ``sync_thumbnail_cache``) directly "
                "from sync callers."
            )

        signature = inspect.signature(f)
        # Sentinel used to lazy-resolve ``cache_default_timeout`` only when
        # the caller didn't supply an explicit value (so the decorator can
        # be applied at module-import time without forcing a settings load).
        unset = object()

        @functools.wraps(f)
        async def async_wrapped(*args: Any, **kwargs: Any) -> Any:
            should_cache = kwargs.pop("cache", True)
            force = kwargs.pop("force", False)
            cache_timeout = kwargs.pop("cache_timeout", unset)
            if cache_timeout is unset:
                cache_timeout = _resolve_cache_default_timeout()

            # Late binding of the default cache so users who configure
            # ``cache_manager`` after import still hit Redis.
            target_cache: AsyncCacheProtocol | None = cache
            if target_cache is None:
                from superset.extensions import cache_manager

                target_cache = cache_manager.cache

            if not should_cache or target_cache is None:
                return await f(*args, **kwargs)

            bound_args = signature.bind(*args, **kwargs)
            bound_args.apply_defaults()
            cache_key = key.format(**bound_args.arguments)

            if not force:
                cached = await target_cache.get(cache_key)
                if cached is not None:
                    return cached

            obj = await f(*args, **kwargs)

            if cache_timeout != CACHE_DISABLED_TIMEOUT:
                try:
                    await target_cache.set(cache_key, obj, ttl=cache_timeout)
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "Failed to write cache for key %s",
                        cache_key,
                        exc_info=True,
                    )
            return obj

        return async_wrapped

    return wrap


__all__ = [
    "ONE_YEAR",
    "generate_cache_key",
    "memoized_func",
    "set_and_log_cache",
]
