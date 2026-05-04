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

# pylint: disable=import-outside-toplevel, unused-argument

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pytest_mock import MockerFixture


class _AsyncMockCache:
    """Minimal async cache stub used by the ``memoized_func`` tests.

    Exposes ``get`` / ``set`` as awaitables and records every call so
    assertions can introspect what the decorator actually did.  We
    don't use ``unittest.mock.AsyncMock`` here because attaching it to
    ``return_value`` requires ceremony for each test step, and the
    real ``AsyncCacheProtocol`` is small enough that a hand-written
    stub keeps the test focused.
    """

    def __init__(self) -> None:
        self.store: dict[str, Any] = {}
        self.get_calls: list[str] = []
        self.set_calls: list[tuple[str, Any, int | None]] = []

    async def get(self, key: str) -> Any:
        self.get_calls.append(key)
        return self.store.get(key)

    async def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        self.set_calls.append((key, value, ttl))
        self.store[key] = value

    async def delete(self, key: str) -> None:
        self.store.pop(key, None)

    async def has(self, key: str) -> bool:
        return key in self.store


def test_memoized_func(mocker: MockerFixture) -> None:
    """``memoized_func`` caches async function results by formatted key."""
    from superset.utils.cache import memoized_func

    cache = _AsyncMockCache()

    decorator = memoized_func("db:{self.id}:schema:{schema}:view_list", cache)

    async def wrapped(self: Any, schema: str) -> int:
        return 42

    decorated = decorator(wrapped)

    self_obj = mocker.MagicMock()
    self_obj.id = 1

    # ── skip cache ──
    result = asyncio.run(decorated(self_obj, "public", cache=False))
    assert result == 42
    assert cache.get_calls == []

    # ── miss → write ──
    result = asyncio.run(decorated(self_obj, "public"))
    assert result == 42
    assert cache.get_calls == ["db:1:schema:public:view_list"]
    assert cache.set_calls
    assert cache.set_calls[0][0] == "db:1:schema:public:view_list"

    # ── hit (cache returns precomputed value) ──
    cache.store["db:1:schema:public:view_list"] = 43
    result = asyncio.run(decorated(self_obj, "public"))
    assert result == 43


def test_memoized_func_rejects_sync(mocker: MockerFixture) -> None:
    """The decorator must raise ``TypeError`` for sync wrapped functions.

    The previous bridged sync→async path created cross-event-loop
    awaits on the async Redis client whenever Redis I/O actually
    fired (the deadlock-guard fall-through path of the bridge spun
    up a fresh asyncio loop on a worker thread).  Splitting sync /
    async cache traffic onto independent Redis clients made the
    sync wrapper redundant — the decorator now refuses sync
    functions outright so callers can't accidentally re-introduce
    the bug.
    """
    from superset.utils.cache import memoized_func

    cache = _AsyncMockCache()
    decorator = memoized_func("k:{x}", cache)

    def sync_fn(x: int) -> int:
        return x

    with pytest.raises(TypeError, match="async \\(coroutine\\) function"):
        decorator(sync_fn)


def test_memoized_func_force(mocker: MockerFixture) -> None:
    """``force=True`` recomputes and overwrites the cached value."""
    from superset.utils.cache import memoized_func

    cache = _AsyncMockCache()
    decorator = memoized_func("k:{x}", cache)

    counter = {"n": 0}

    async def wrapped(x: int) -> int:
        counter["n"] += 1
        return x * 10

    decorated = decorator(wrapped)

    # Pre-populate the cache so a non-force call would return early.
    cache.store["k:5"] = 999

    # ── normal call: hits the cache ──
    assert asyncio.run(decorated(5)) == 999
    assert counter["n"] == 0

    # ── force=True: bypasses get(), writes through ──
    assert asyncio.run(decorated(5, force=True)) == 50
    assert counter["n"] == 1
    assert cache.store["k:5"] == 50


def test_memoized_func_disabled_timeout(mocker: MockerFixture) -> None:
    """``cache_timeout=CACHE_DISABLED_TIMEOUT`` skips the write path."""
    from superset.constants import CACHE_DISABLED_TIMEOUT
    from superset.utils.cache import memoized_func

    cache = _AsyncMockCache()
    decorator = memoized_func("k:{x}", cache)

    async def wrapped(x: int) -> int:
        return x + 1

    decorated = decorator(wrapped)

    asyncio.run(decorated(7, cache_timeout=CACHE_DISABLED_TIMEOUT))
    # No write happened.
    assert cache.set_calls == []
