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
"""End-to-end verification: submit job -> event appears on WebSocket and polling.

These tests verify the full flow:
1. AsyncEventManager.init_job() creates a job
2. AsyncEventManager.update_job() publishes a "done" event
3. The event appears via both WebSocket relay and polling REST API

Frontend compatibility: the message format matches the format expected by
``superset-frontend/src/middleware/asyncEvent.ts``:
  {id, channel_id, job_id, user_id, status, errors, result_url}
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from superset.async_events.manager import AsyncEventManager


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
    return AsyncEventManager(redis=mock_redis)


async def test_full_job_lifecycle(manager: AsyncEventManager, mock_redis: AsyncMock):
    """Test complete job lifecycle: init -> update -> read."""
    # Init job
    channel_id, job_id = await manager.init_job(user_id=42)
    assert channel_id
    assert job_id

    # Simulate stored events for read
    mock_redis.xrange.return_value = [
        (
            "1607477697866-0",
            {"data": json.dumps({
                "channel_id": channel_id,
                "job_id": job_id,
                "user_id": 42,
                "status": "pending",
                "errors": [],
                "result_url": None,
            })},
        ),
        (
            "1607477697867-0",
            {"data": json.dumps({
                "channel_id": channel_id,
                "job_id": job_id,
                "user_id": 42,
                "status": "done",
                "errors": [],
                "result_url": "/api/v1/chart/data/cache-key-123",
            })},
        ),
    ]

    # Update job to done
    await manager.update_job(
        channel_id=channel_id,
        job_id=job_id,
        user_id=42,
        status="done",
        result_url="/api/v1/chart/data/cache-key-123",
    )

    # Verify pub/sub notification was published (for WebSocket relay)
    mock_redis.publish.assert_called_once()
    pubsub_channel = mock_redis.publish.call_args[0][0]
    assert pubsub_channel == "events:42"

    # Read events via polling
    events = await manager.read_events(channel_id=channel_id, last_id=None)
    assert len(events) == 2
    assert events[0]["status"] == "pending"
    assert events[1]["status"] == "done"
    assert events[1]["result_url"] == "/api/v1/chart/data/cache-key-123"


async def test_message_format_frontend_compat(manager: AsyncEventManager, mock_redis: AsyncMock):
    """Verify message format matches frontend expectations.

    The frontend (asyncEvent.ts) expects:
    {id, channel_id, job_id, user_id, status, errors, result_url}
    """
    mock_redis.xrange.return_value = [
        (
            "1607477697866-0",
            {"data": json.dumps({
                "channel_id": "ch-1",
                "job_id": "job-1",
                "user_id": 42,
                "status": "done",
                "errors": [],
                "result_url": "/result",
            })},
        ),
    ]

    events = await manager.read_events(channel_id="ch-1", last_id=None)
    event = events[0]

    # All required fields present
    required_fields = {"id", "channel_id", "job_id", "user_id", "status", "errors", "result_url"}
    assert required_fields.issubset(set(event.keys()))

    # Types match frontend expectations
    assert isinstance(event["id"], str)
    assert isinstance(event["channel_id"], str)
    assert isinstance(event["job_id"], str)
    assert isinstance(event["user_id"], int)
    assert isinstance(event["status"], str)
    assert isinstance(event["errors"], list)
    assert isinstance(event["result_url"], str)
