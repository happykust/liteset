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
from unittest.mock import AsyncMock

import pytest

from superset.cache.manager import AsyncCacheManager


@pytest.fixture
def mock_redis():
    r = AsyncMock()
    r.get = AsyncMock(return_value=None)
    r.set = AsyncMock()
    r.delete = AsyncMock()
    r.exists = AsyncMock(return_value=0)
    return r


async def test_get_miss(mock_redis):
    mgr = AsyncCacheManager(mock_redis)
    assert await mgr.get("x") is None


async def test_set_with_ttl(mock_redis):
    mgr = AsyncCacheManager(mock_redis, default_ttl=60)
    await mgr.set("k", b"v")
    mock_redis.set.assert_called_once_with("k", b"v", ex=60)


async def test_get_or_set_miss(mock_redis):
    mgr = AsyncCacheManager(mock_redis)
    factory = AsyncMock(return_value=b"computed")
    result = await mgr.get_or_set("k", factory)
    assert result == b"computed"
    factory.assert_called_once()


async def test_get_or_set_hit(mock_redis):
    mock_redis.get = AsyncMock(return_value=b"cached")
    mgr = AsyncCacheManager(mock_redis)
    factory = AsyncMock()
    result = await mgr.get_or_set("k", factory)
    assert result == b"cached"
    factory.assert_not_called()


async def test_delete(mock_redis):
    mgr = AsyncCacheManager(mock_redis)
    await mgr.delete("k")
    mock_redis.delete.assert_called_once_with("k")


async def test_has_false(mock_redis):
    mgr = AsyncCacheManager(mock_redis)
    assert await mgr.has("x") is False


async def test_has_true(mock_redis):
    mock_redis.exists = AsyncMock(return_value=1)
    mgr = AsyncCacheManager(mock_redis)
    assert await mgr.has("x") is True


async def test_clear_prefix(mock_redis):
    from unittest.mock import MagicMock

    mock_pipe = MagicMock()
    mock_pipe.delete = MagicMock()
    mock_pipe.execute = AsyncMock()
    mock_pipe.__aenter__ = AsyncMock(return_value=mock_pipe)
    mock_pipe.__aexit__ = AsyncMock(return_value=False)
    mock_redis.pipeline = lambda transaction=False: mock_pipe

    async def fake_scan_iter(match=""):
        for k in [b"prefix:a", b"prefix:b"]:
            yield k

    mock_redis.scan_iter = fake_scan_iter

    mgr = AsyncCacheManager(mock_redis)
    count = await mgr.clear_prefix("prefix:")
    assert count == 2
    assert mock_pipe.delete.call_count == 2
    mock_pipe.execute.assert_called()


async def test_close(mock_redis):
    mock_redis.aclose = AsyncMock()
    mgr = AsyncCacheManager(mock_redis)
    await mgr.close()
    mock_redis.aclose.assert_called_once()


async def test_binary_cache_default_is_non_decoding():
    """Binary cache slots must default to a non-decoding async client.

    Regression: reusing the ``decode_responses=True`` auth-cache client as the
    slot default corrupts binary reads (UnicodeDecodeError on byte 0x80 from
    serialized DataFrames / query-context forms / thumbnail bytes).
    """
    from superset.cache.manager import CacheManager

    auth_client = object()  # decode=True auth client stand-in — must NOT be used
    mgr = CacheManager()
    mgr.init_app(
        redis=auth_client,
        cache_default_timeout=300,
        # RedisCache with no host/url => the slot falls back to the manager's
        # default async client.
        cache_config={"CACHE_TYPE": "RedisCache"},
        redis_url="redis://localhost:6379/0",
    )
    default_async = mgr._default_async_redis
    assert default_async is not None
    assert (
        default_async.connection_pool.connection_kwargs.get("decode_responses") is False
    )
    # The binary cache slot uses the non-decoding default, not the auth client.
    assert getattr(mgr._cache, "_redis", None) is default_async
    assert getattr(mgr._cache, "_redis", None) is not auth_client
    await mgr.close()


def test_metastore_namespace_seeds_from_config_key():
    """filter_state and explore_form_data metastore slots must get DISTINCT
    UUID namespaces, seeded from the config-key name via
    ``cache_config.get("CACHE_KEY_PREFIX", cache_config_key)``. Collapsing
    both to ``get_uuid_namespace("")`` drops per-slot isolation and fails to
    read back rows written under the named namespace."""
    from unittest.mock import MagicMock

    from superset.cache.manager import _build_metastore_cache_from_config
    from superset.key_value.utils import get_uuid_namespace

    def _sf():
        return MagicMock()

    fs = _build_metastore_cache_from_config(
        cfg={},
        session_factory=_sf,
        fallback_default_ttl=300,
        config_key="FILTER_STATE_CACHE_CONFIG",
    )
    ex = _build_metastore_cache_from_config(
        cfg={},
        session_factory=_sf,
        fallback_default_ttl=300,
        config_key="EXPLORE_FORM_DATA_CACHE_CONFIG",
    )
    assert fs._namespace == get_uuid_namespace("FILTER_STATE_CACHE_CONFIG")
    assert ex._namespace == get_uuid_namespace("EXPLORE_FORM_DATA_CACHE_CONFIG")
    assert fs._namespace != ex._namespace
    assert fs._namespace != get_uuid_namespace("")
