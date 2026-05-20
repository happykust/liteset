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
from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from superset.async_events.manager import (
    AsyncEventManager,
    build_job_metadata,
    increment_id,
    parse_event,
)


def test_build_job_metadata():
    meta = build_job_metadata(
        channel_id="ch-1",
        job_id="job-1",
        user_id=42,
        status="running",
        errors=[],
        result_url="/api/v1/chart/data/cache-key-123",
    )
    assert meta["channel_id"] == "ch-1"
    assert meta["job_id"] == "job-1"
    assert meta["user_id"] == 42
    assert meta["status"] == "running"
    assert meta["errors"] == []
    assert meta["result_url"] == "/api/v1/chart/data/cache-key-123"


def test_build_job_metadata_defaults():
    meta = build_job_metadata(channel_id="ch-1", job_id="job-1", user_id=None)
    assert meta["status"] is None
    assert meta["errors"] == []
    assert meta["result_url"] is None


def test_parse_event():
    raw = (
        "1607477697866-0",
        {"data": json.dumps({"channel_id": "ch-1", "status": "done"})},
    )
    parsed = parse_event(raw)
    assert parsed["id"] == "1607477697866-0"
    assert parsed["channel_id"] == "ch-1"
    assert parsed["status"] == "done"


def test_increment_id():
    assert increment_id("1607477697866-0") == "1607477697866-1"
    assert increment_id("1607477697866-9") == "1607477697866-10"


def test_increment_id_invalid():
    result = increment_id("invalid")
    assert result == "invalid"


@pytest.fixture
def mock_redis() -> AsyncMock:
    redis = AsyncMock()
    redis.xadd = AsyncMock(return_value="1607477697866-0")
    redis.xrange = AsyncMock(return_value=[])
    redis.publish = AsyncMock(return_value=1)
    redis.xlen = AsyncMock(return_value=0)
    redis.xtrim = AsyncMock()
    return redis


@pytest.fixture
def manager(mock_redis: AsyncMock) -> AsyncEventManager:
    return AsyncEventManager(
        redis=mock_redis,
        stream_prefix="async-events-",
        global_stream_key="async-events-full",
        global_stream_limit=1_000_000,
        channel_stream_limit=1_000,
    )


async def test_init_job(manager: AsyncEventManager, mock_redis: AsyncMock):
    channel_id, job_id = await manager.init_job(user_id=42)
    assert channel_id  # UUID string
    assert job_id  # UUID string
    # Should xadd to both channel stream and global stream
    assert mock_redis.xadd.call_count == 2


async def test_update_job(manager: AsyncEventManager, mock_redis: AsyncMock):
    await manager.update_job(
        channel_id="ch-1",
        job_id="job-1",
        user_id=42,
        status="done",
        result_url="/result",
    )
    # Should xadd to channel stream + global stream — and NOT publish to
    # pub/sub (the WebSocket relay reads the channel stream directly).
    assert mock_redis.xadd.call_count == 2
    # Channel stream then global firehose stream
    channel_call, global_call = mock_redis.xadd.call_args_list
    assert channel_call.args[0] == "async-events-ch-1"
    assert global_call.args[0] == "async-events-full"
    mock_redis.publish.assert_not_called()


async def test_read_events_empty(manager: AsyncEventManager, mock_redis: AsyncMock):
    mock_redis.xrange.return_value = []
    events = await manager.read_events(channel_id="ch-1", last_id=None)
    assert events == []


async def test_read_events_with_data(manager: AsyncEventManager, mock_redis: AsyncMock):
    mock_redis.xrange.return_value = [
        (
            "1607477697866-0",
            {"data": json.dumps({"channel_id": "ch-1", "status": "done"})},
        ),
    ]
    events = await manager.read_events(channel_id="ch-1", last_id=None)
    assert len(events) == 1
    assert events[0]["id"] == "1607477697866-0"
    assert events[0]["status"] == "done"


async def test_read_events_with_last_id(
    manager: AsyncEventManager, mock_redis: AsyncMock
):
    await manager.read_events(channel_id="ch-1", last_id="1607477697866-0")
    # Should call xrange with incremented last_id
    call_args = mock_redis.xrange.call_args
    assert call_args[0][1] == "1607477697866-1"  # incremented start


async def test_cleanup_empty_channels(
    manager: AsyncEventManager, mock_redis: AsyncMock
):
    mock_redis.xlen.return_value = 0
    await manager.cleanup_channel("ch-1")
    # No trim needed for empty channel
    mock_redis.xtrim.assert_not_called()


async def test_cleanup_oversized_channel(
    manager: AsyncEventManager, mock_redis: AsyncMock
):
    mock_redis.xlen.return_value = 2000
    await manager.cleanup_channel("ch-1")
    mock_redis.xtrim.assert_called_once()
