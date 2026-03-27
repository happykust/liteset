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
"""Async cache manager backed by redis.asyncio."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

T = TypeVar("T")

logger = logging.getLogger(__name__)

_CLEAR_PREFIX_BATCH_SIZE = 100
_LOCK_TTL_SECONDS = 30
_LOCK_RETRY_DELAY = 0.05
_LOCK_MAX_RETRIES = 100


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

    async def set(
        self, key: str, value: bytes, ttl: int | None = None
    ) -> None:
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
                await self._redis.set(
                    lock_key, b"1", nx=True, ex=_LOCK_TTL_SECONDS
                )
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
        """Close the underlying Redis connection."""
        await self._redis.aclose()
