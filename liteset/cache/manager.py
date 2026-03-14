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

from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

T = TypeVar("T")


class AsyncCacheManager:
    """Async cache manager wrapping a redis.asyncio client."""

    def __init__(self, redis: Any, default_ttl: int = 300) -> None:
        self._redis = redis
        self._default_ttl = default_ttl

    async def get(self, key: str) -> bytes | None:
        return await self._redis.get(key)

    async def set(
        self, key: str, value: bytes, ttl: int | None = None
    ) -> None:
        ex = ttl if ttl is not None else self._default_ttl
        await self._redis.set(key, value, ex=ex)

    async def delete(self, key: str) -> None:
        await self._redis.delete(key)

    async def has(self, key: str) -> bool:
        return bool(await self._redis.exists(key))

    async def get_or_set(
        self,
        key: str,
        factory: Callable[[], Awaitable[bytes]],
        ttl: int | None = None,
    ) -> bytes:
        cached = await self.get(key)
        if cached is not None:
            return cached
        value = await factory()
        await self.set(key, value, ttl=ttl)
        return value

    async def clear_prefix(self, prefix: str) -> int:
        """Delete all keys matching prefix*. Returns count deleted."""
        count = 0
        async for key in self._redis.scan_iter(match=f"{prefix}*"):
            await self._redis.delete(key)
            count += 1
        return count
