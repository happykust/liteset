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
"""Unit tests for the AsyncEvents controller and AsyncEventManager."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from liteset.async_events.manager import AsyncEventManager
from liteset.controllers.async_event import AsyncEventsController


# ---------------------------------------------------------------------------
# Controller metadata
# ---------------------------------------------------------------------------


def test_async_events_controller_path():
    assert AsyncEventsController.path == "/api/v1/async_event"


def test_async_events_controller_tags():
    assert AsyncEventsController.tags == ["Async Events"]


# ---------------------------------------------------------------------------
# AsyncEventManager — read_events
# ---------------------------------------------------------------------------


async def test_manager_read_events_none_redis():
    """Manager returns empty list when redis is None."""
    manager = AsyncEventManager(redis=None)
    events = await manager.read_events("some_channel")
    assert events == []


async def test_manager_read_events_success():
    """Manager parses Redis XREAD results into event dicts."""
    redis = AsyncMock()
    redis.xread = AsyncMock(return_value=[
        (b"user_42", [
            (b"1-0", {"status": "done", "job_id": "abc"}),
            (b"2-0", {"status": "error", "job_id": "def"}),
        ]),
    ])
    manager = AsyncEventManager(redis=redis)
    events = await manager.read_events("user_42")

    assert len(events) == 2
    assert events[0]["id"] == b"1-0"
    assert events[0]["status"] == "done"
    assert events[1]["id"] == b"2-0"
    assert events[1]["status"] == "error"


async def test_manager_read_events_empty_stream():
    """Manager returns empty list when stream has no messages."""
    redis = AsyncMock()
    redis.xread = AsyncMock(return_value=[])
    manager = AsyncEventManager(redis=redis)
    events = await manager.read_events("user_42")
    assert events == []


async def test_manager_read_events_passes_params():
    """Manager passes count, block, and last_id to xread."""
    redis = AsyncMock()
    redis.xread = AsyncMock(return_value=[])
    manager = AsyncEventManager(redis=redis)

    await manager.read_events("ch", last_id="5-0", count=50, block_ms=1000)

    redis.xread.assert_called_once_with(
        {"ch": "5-0"}, count=50, block=1000
    )


async def test_manager_read_events_exception():
    """Manager returns empty list on exception."""
    redis = AsyncMock()
    redis.xread = AsyncMock(side_effect=ConnectionError("connection lost"))
    manager = AsyncEventManager(redis=redis)
    events = await manager.read_events("channel")
    assert events == []


# ---------------------------------------------------------------------------
# AsyncEventManager — publish_event
# ---------------------------------------------------------------------------


async def test_manager_publish_event_none_redis():
    """Manager returns None when redis is None."""
    manager = AsyncEventManager(redis=None)
    result = await manager.publish_event("channel", {"key": "val"})
    assert result is None


async def test_manager_publish_event_success():
    """Manager returns event ID on success."""
    redis = AsyncMock()
    redis.xadd = AsyncMock(return_value=b"1-0")
    manager = AsyncEventManager(redis=redis)
    result = await manager.publish_event("channel", {"key": "val"})
    assert result == b"1-0"
    redis.xadd.assert_called_once_with("channel", {"key": "val"})


async def test_manager_publish_event_exception():
    """Manager returns None on exception."""
    redis = AsyncMock()
    redis.xadd = AsyncMock(side_effect=ConnectionError("connection lost"))
    manager = AsyncEventManager(redis=redis)
    result = await manager.publish_event("channel", {"key": "val"})
    assert result is None
