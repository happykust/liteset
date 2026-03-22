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
"""Tests for AsyncDistributedLock."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from liteset.distributed_lock.lock import AsyncDistributedLock


@pytest.fixture
def mock_redis():
    redis = AsyncMock()
    return redis


async def test_acquire_success(mock_redis):
    mock_redis.set.return_value = True
    lock = AsyncDistributedLock(mock_redis, "test-key", timeout=10)
    result = await lock.acquire()
    assert result is True
    mock_redis.set.assert_called_once()


async def test_acquire_failure(mock_redis):
    mock_redis.set.return_value = None
    lock = AsyncDistributedLock(mock_redis, "test-key")
    result = await lock.acquire()
    assert result is False


async def test_release_calls_eval(mock_redis):
    mock_redis.set.return_value = True
    lock = AsyncDistributedLock(mock_redis, "test-key")
    await lock.acquire()
    await lock.release()
    mock_redis.eval.assert_called_once()


async def test_release_without_acquire(mock_redis):
    lock = AsyncDistributedLock(mock_redis, "test-key")
    await lock.release()
    mock_redis.eval.assert_not_called()


async def test_context_manager(mock_redis):
    mock_redis.set.return_value = True
    async with AsyncDistributedLock(mock_redis, "test-key") as lock:
        assert lock._acquired is True
    mock_redis.eval.assert_called_once()


async def test_context_manager_acquire_failure(mock_redis):
    mock_redis.set.return_value = None
    with pytest.raises(RuntimeError, match="Could not acquire"):
        async with AsyncDistributedLock(mock_redis, "test-key"):
            pass


async def test_no_redis_skip_locking():
    lock = AsyncDistributedLock(None, "test-key")
    result = await lock.acquire()
    assert result is True
    await lock.release()
    assert lock._acquired is False
