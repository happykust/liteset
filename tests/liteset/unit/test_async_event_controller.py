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

from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from litestar import Litestar
from litestar.di import Provide
from litestar.testing import AsyncTestClient

from liteset.controllers.async_event import AsyncEventController

# Skip msgspec validation for DI-injected mock params
from litestar._signature.model import (
    _normalize_annotation as _norm_fn,
)

_SKIP_VALIDATION_NAMES: set[str] = _norm_fn.__globals__["SKIP_VALIDATION_NAMES"]
_DI_PARAMS = frozenset({"event_manager", "current_user"})


@contextmanager
def _skip_di_validation():
    _SKIP_VALIDATION_NAMES.update(_DI_PARAMS)
    try:
        yield
    finally:
        _SKIP_VALIDATION_NAMES.difference_update(_DI_PARAMS)


@pytest.fixture
def mock_event_manager() -> AsyncMock:
    manager = AsyncMock()
    manager.read_events = AsyncMock(return_value=[
        {
            "id": "1607477697866-0",
            "channel_id": "ch-1",
            "job_id": "job-1",
            "user_id": 42,
            "status": "done",
            "errors": [],
            "result_url": "/api/v1/chart/data/cache-key-123",
        }
    ])
    return manager


@pytest.fixture
async def client(mock_event_manager: AsyncMock):
    """Create test client with mocked dependencies."""

    async def provide_event_manager() -> AsyncMock:
        return mock_event_manager

    async def provide_current_user() -> MagicMock:
        user = MagicMock()
        user.id = 42
        return user

    with _skip_di_validation():
        app = Litestar(
            route_handlers=[AsyncEventController],
            dependencies={
                "event_manager": Provide(provide_event_manager),
                "current_user": Provide(provide_current_user),
            },
        )
    async with AsyncTestClient(app) as tc:
        yield tc


async def test_get_events_no_last_id(client: AsyncTestClient, mock_event_manager: AsyncMock):
    resp = await client.get("/api/v1/async_event/")
    assert resp.status_code == 200
    data = resp.json()
    assert "result" in data
    assert len(data["result"]) == 1
    assert data["result"][0]["status"] == "done"


async def test_get_events_with_last_id(client: AsyncTestClient, mock_event_manager: AsyncMock):
    resp = await client.get("/api/v1/async_event/?last_id=1607477697866-0")
    assert resp.status_code == 200
    mock_event_manager.read_events.assert_called_once()
    call_kwargs = mock_event_manager.read_events.call_args
    assert call_kwargs[1].get("last_id") == "1607477697866-0" or call_kwargs[0][-1] == "1607477697866-0"


async def test_get_events_empty(client: AsyncTestClient, mock_event_manager: AsyncMock):
    mock_event_manager.read_events.return_value = []
    resp = await client.get("/api/v1/async_event/")
    assert resp.status_code == 200
    data = resp.json()
    assert data["result"] == []
