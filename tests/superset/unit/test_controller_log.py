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
"""Tests for LogController."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest

from superset.controllers.log import LogController
from superset.exceptions import ObjectNotFoundError

# ---------------------------------------------------------------------------
# Helpers — Litestar decorators wrap methods; access the raw fn for unit tests.
# ---------------------------------------------------------------------------


def _get_raw_method(controller_cls: type, method_name: str):
    """Return the underlying async function from a Litestar-decorated controller
    method.
    """
    handler = getattr(controller_cls, method_name)
    if hasattr(handler, "fn"):
        return handler.fn
    return handler


_get_list = _get_raw_method(LogController, "get_list")
_get_single = _get_raw_method(LogController, "get_single")
_create_log = _get_raw_method(LogController, "create_log")
_recent_activity = _get_raw_method(LogController, "recent_activity")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_dao():
    return AsyncMock()


@pytest.fixture
def mock_user():
    user = MagicMock()
    type(user).id = PropertyMock(return_value=1)
    type(user).is_authenticated = PropertyMock(return_value=True)
    return user


@pytest.fixture
def controller():
    return LogController(owner=MagicMock())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_get_list(controller, mock_dao):
    user1 = MagicMock()
    user1.first_name = "Test"
    user1.last_name = "User"
    user1.username = "testuser"

    item1 = MagicMock()
    item1.id = 1
    item1.action = "explore"
    item1.user_id = 1
    item1.user = user1
    item1.dashboard_id = None
    item1.slice_id = 5
    item1.json = "{}"
    item1.dttm = "2026-01-01T00:00:00"
    item1.duration_ms = 100
    item1.referrer = "http://localhost"

    item2 = MagicMock()
    item2.id = 2
    item2.action = "dashboard"
    item2.user_id = 1
    item2.user = None
    item2.dashboard_id = 10
    item2.slice_id = None
    item2.json = "{}"
    item2.dttm = "2026-01-02T00:00:00"
    item2.duration_ms = None
    item2.referrer = None

    mock_dao.find_all.return_value = [item1, item2]
    mock_dao.count.return_value = 2

    result = await _get_list(controller, dao=mock_dao, rison_params=None)

    assert result["count"] == 2
    assert len(result["result"]) == 2

    # Verify user nested fields
    assert result["result"][0]["user"]["first_name"] == "Test"
    assert result["result"][0]["user"]["username"] == "testuser"
    assert result["result"][0]["duration_ms"] == 100
    assert result["result"][0]["referrer"] == "http://localhost"

    # Verify null user
    assert result["result"][1]["user"] is None
    assert result["result"][1]["duration_ms"] is None

    mock_dao.find_all.assert_awaited_once_with(page=0, page_size=25)
    mock_dao.count.assert_awaited_once()


async def test_get_list_with_pagination(controller, mock_dao):
    mock_dao.find_all.return_value = [MagicMock()]
    mock_dao.count.return_value = 50

    result = await _get_list(
        controller, dao=mock_dao, rison_params={"page": 2, "page_size": 10}
    )

    assert result["count"] == 50
    mock_dao.find_all.assert_awaited_once_with(page=2, page_size=10)


async def test_get_single(controller, mock_dao):
    item = MagicMock()
    item.id = 1
    mock_dao.find_by_id.return_value = item

    result = await _get_single(controller, pk=1, dao=mock_dao)

    assert result["result"] == item
    mock_dao.find_by_id.assert_awaited_once_with(1)


async def test_get_single_not_found(controller, mock_dao):
    mock_dao.find_by_id.return_value = None

    with pytest.raises(ObjectNotFoundError):
        await _get_single(controller, pk=999, dao=mock_dao)


async def test_recent_activity(controller, mock_dao, mock_user):
    from datetime import datetime, timezone

    log_item = MagicMock()
    log_item.action = "mount_explorer"
    log_item.slice_id = 5
    log_item.dashboard_id = None
    log_item.dttm = datetime(2026, 1, 1, tzinfo=timezone.utc)

    mock_dao.get_recent_activity.return_value = [log_item]

    result = await _recent_activity(
        controller, dao=mock_dao, current_user=mock_user, rison_params=None
    )

    assert len(result["result"]) == 1
    assert result["result"][0]["action"] == "mount_explorer"
    assert result["result"][0]["item_type"] == "slice"
    assert result["result"][0]["item_url"] == "/explore/?slice_id=5"
    assert "time_delta_humanized" in result["result"][0]
    mock_dao.get_recent_activity.assert_awaited_once_with(
        user_id=1,
        actions=["mount_explorer", "mount_dashboard"],
        page=0,
        page_size=25,
    )


async def test_recent_activity_with_params(controller, mock_dao, mock_user):
    mock_dao.get_recent_activity.return_value = []

    result = await _recent_activity(
        controller,
        dao=mock_dao,
        current_user=mock_user,
        rison_params={"page": 1, "page_size": 10, "actions": ["explore"]},
    )

    assert result["result"] == []
    mock_dao.get_recent_activity.assert_awaited_once_with(
        user_id=1,
        actions=["explore"],
        page=1,
        page_size=10,
    )


async def test_controller_path():
    assert LogController.path == "/api/v1/log"
