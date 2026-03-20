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

from liteset.cache.manager import AsyncCacheManager


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
    mock_pipe.delete = MagicMock()  # sync mock to avoid coroutine warnings
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


async def test_close(mock_redis):
    mock_redis.aclose = AsyncMock()
    mgr = AsyncCacheManager(mock_redis)
    await mgr.close()
    mock_redis.aclose.assert_called_once()
