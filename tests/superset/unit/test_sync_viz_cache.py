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
"""Unit tests for the legacy-viz sync data cache adapter.

Locks the wire contract the explore_json round-trip depends on: values are
serialized so the worker write and the web read agree, keys are namespaced
with the shared ``superset_cache:`` prefix, and connection-detail resolution
mirrors the async ``_build_async_redis_from_config`` twin.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from superset.cache import sync_viz_cache as svc
from superset.cache.sync_viz_cache import (
    build_sync_viz_cache,
    CACHE_KEY_PREFIX,
    SyncVizCache,
)


@pytest.fixture(autouse=True)
def _clear_instance_cache() -> Any:
    """Reset the connection-keyed instance cache so each build_* test is isolated."""
    svc._INSTANCE_CACHE.clear()
    yield
    svc._INSTANCE_CACHE.clear()


class _FakeRedis:
    """Minimal dict-backed stand-in for a blocking ``redis.Redis`` client."""

    def __init__(self) -> None:
        self.store: dict[str, Any] = {}
        self.last_ttl: int | None = None

    def get(self, key: str) -> Any:
        return self.store.get(key)

    def set(self, key: str, value: Any) -> None:
        self.store[key] = value

    def setex(self, key: str, ttl: int, value: Any) -> None:
        self.store[key] = value
        self.last_ttl = ttl


def test_set_get_round_trip_serializes_under_prefix() -> None:
    r = _FakeRedis()
    cache = SyncVizCache(r)
    value = {"df": [1, 2, 3], "query": "SELECT 1"}

    cache.set("ejr-abc", value, timeout=300)

    # Stored under the shared prefix with a TTL...
    assert f"{CACHE_KEY_PREFIX}ejr-abc" in r.store
    assert r.last_ttl == 300
    # ...as opaque serialized bytes, not the live object.
    assert isinstance(r.store[f"{CACHE_KEY_PREFIX}ejr-abc"], (bytes, bytearray))
    # Round-trips back to an equal value.
    assert cache.get("ejr-abc") == value


def test_get_missing_returns_none() -> None:
    assert SyncVizCache(_FakeRedis()).get("nope") is None


def test_get_corrupt_bytes_returns_none() -> None:
    r = _FakeRedis()
    r.store[f"{CACHE_KEY_PREFIX}bad"] = b"\x80not-serialized"
    assert SyncVizCache(r).get("bad") is None


def test_set_without_timeout_uses_set_not_setex() -> None:
    r = _FakeRedis()
    SyncVizCache(r).set("k", {"a": 1})
    assert f"{CACHE_KEY_PREFIX}k" in r.store
    assert r.last_ttl is None


def test_build_from_redis_url() -> None:
    cfg = {"CACHE_TYPE": "RedisCache", "CACHE_REDIS_URL": "redis://h:6379/2"}
    with patch("redis.Redis.from_url") as from_url:
        cache = build_sync_viz_cache(cfg)
    assert isinstance(cache, SyncVizCache)
    from_url.assert_called_once_with("redis://h:6379/2", decode_responses=False)


def test_build_from_host_kwargs() -> None:
    cfg = {
        "CACHE_TYPE": "RedisCache",
        "CACHE_REDIS_HOST": "redis",
        "CACHE_REDIS_PORT": "6379",
        "CACHE_REDIS_DB": "2",
    }
    with patch("redis.Redis") as redis_cls:
        cache = build_sync_viz_cache(cfg)
    assert isinstance(cache, SyncVizCache)
    kwargs = redis_cls.call_args.kwargs
    assert kwargs["host"] == "redis"
    assert kwargs["port"] == 6379
    assert kwargs["db"] == 2
    assert kwargs["decode_responses"] is False


def test_build_falls_back_to_redis_url_when_no_cache_details() -> None:
    with patch("redis.Redis.from_url") as from_url:
        cache = build_sync_viz_cache(None, fallback_redis_url="redis://h:6379/3")
    assert isinstance(cache, SyncVizCache)
    from_url.assert_called_once_with("redis://h:6379/3", decode_responses=False)


def test_build_returns_none_without_any_connection() -> None:
    assert build_sync_viz_cache(None) is None
    assert build_sync_viz_cache({}) is None


def test_build_memoizes_per_connection() -> None:
    cfg = {"CACHE_TYPE": "RedisCache", "CACHE_REDIS_URL": "redis://h:6379/9"}
    with patch("redis.Redis.from_url") as from_url:
        first = build_sync_viz_cache(cfg)
        second = build_sync_viz_cache(cfg)
    # One client/pool built and reused across calls (no per-request leak).
    from_url.assert_called_once()
    assert first is second


def test_set_negative_timeout_skips_write() -> None:
    r = _FakeRedis()
    # CACHE_DISABLED_TIMEOUT (-1) must not reach Redis (SETEX -1 errors).
    SyncVizCache(r).set("k", {"a": 1}, timeout=-1)
    assert r.store == {}
    assert r.last_ttl is None
