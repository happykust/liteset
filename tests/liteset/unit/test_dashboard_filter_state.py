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

import json  # noqa: TID251
from unittest.mock import AsyncMock

import pytest

from liteset.commands.dashboard_filter_state import (
    CreateFilterStateCommand,
    DeleteFilterStateCommand,
    GetFilterStateCommand,
    UpdateFilterStateCommand,
)
from liteset.commands.dashboard_permalink import (
    CreateDashboardPermalinkCommand,
    GetDashboardPermalinkCommand,
)
from liteset.exceptions import ForbiddenError, ObjectNotFoundError


@pytest.fixture
def mock_kv_dao():
    dao = AsyncMock()
    dao.set_value = AsyncMock()
    # Return an envelope-formatted value by default
    dao.get_value = AsyncMock(
        return_value=json.dumps({"owner": 1, "value": '{"filters": []}'})
    )
    dao.delete_value = AsyncMock(return_value=True)
    return dao


async def test_create_filter_state(mock_kv_dao):
    cmd = CreateFilterStateCommand(
        dao=mock_kv_dao, dashboard_id=1, value='{"key": "val"}', user_id=1
    )
    key = await cmd.execute()
    assert isinstance(key, str)
    assert len(key) > 0
    mock_kv_dao.set_value.assert_awaited_once()
    # Verify envelope format
    stored = json.loads(mock_kv_dao.set_value.call_args.kwargs["value"])
    assert stored["owner"] == 1
    assert stored["value"] == '{"key": "val"}'


async def test_get_filter_state(mock_kv_dao):
    cmd = GetFilterStateCommand(dao=mock_kv_dao, dashboard_id=1, key="test-key")
    value = await cmd.execute()
    # Should unwrap the envelope and return inner value
    assert value == '{"filters": []}'


async def test_get_filter_state_not_found(mock_kv_dao):
    mock_kv_dao.get_value = AsyncMock(return_value=None)
    cmd = GetFilterStateCommand(dao=mock_kv_dao, dashboard_id=1, key="missing")
    with pytest.raises(ObjectNotFoundError):
        await cmd.execute()


async def test_update_filter_state(mock_kv_dao):
    # Existing entry owned by user 1
    mock_kv_dao.get_value = AsyncMock(
        return_value=json.dumps({"owner": 1, "value": "old"})
    )
    cmd = UpdateFilterStateCommand(
        dao=mock_kv_dao, dashboard_id=1, key="test-key", value="updated", user_id=1
    )
    result = await cmd.execute()
    assert result == "test-key"


async def test_update_filter_state_not_found(mock_kv_dao):
    mock_kv_dao.get_value = AsyncMock(return_value=None)
    cmd = UpdateFilterStateCommand(
        dao=mock_kv_dao, dashboard_id=1, key="missing", value="x", user_id=1
    )
    with pytest.raises(ObjectNotFoundError):
        await cmd.validate()


async def test_update_filter_state_wrong_owner(mock_kv_dao):
    """Non-owner should be rejected with ForbiddenError."""
    mock_kv_dao.get_value = AsyncMock(
        return_value=json.dumps({"owner": 99, "value": "old"})
    )
    cmd = UpdateFilterStateCommand(
        dao=mock_kv_dao, dashboard_id=1, key="test-key", value="hacked", user_id=1
    )
    with pytest.raises(ForbiddenError):
        await cmd.validate()


async def test_delete_filter_state(mock_kv_dao):
    cmd = DeleteFilterStateCommand(dao=mock_kv_dao, dashboard_id=1, key="test-key")
    result = await cmd.execute()
    assert result is True


async def test_create_dashboard_permalink(mock_kv_dao):
    cmd = CreateDashboardPermalinkCommand(
        dao=mock_kv_dao,
        dashboard_id=1,
        state={"dataMask": {}, "activeTabs": []},
        dashboard_uuid="550e8400-e29b-41d4-a716-446655440000",
    )
    key = await cmd.execute()
    assert isinstance(key, str)
    assert len(key) >= 16
    # Verify stored payload uses UUID and nests state
    stored = json.loads(mock_kv_dao.set_value.call_args.kwargs["value"])
    assert stored["dashboardId"] == "550e8400-e29b-41d4-a716-446655440000"
    assert "state" in stored
    assert stored["state"]["dataMask"] == {}


async def test_create_dashboard_permalink_fallback_int_id(mock_kv_dao):
    """Without dashboard_uuid, falls back to int id."""
    cmd = CreateDashboardPermalinkCommand(
        dao=mock_kv_dao, dashboard_id=42, state={"dataMask": {}}
    )
    key = await cmd.execute()
    stored = json.loads(mock_kv_dao.set_value.call_args.kwargs["value"])
    assert stored["dashboardId"] == 42


async def test_get_dashboard_permalink(mock_kv_dao):
    mock_kv_dao.get_value = AsyncMock(
        return_value='{"dashboardId": "abc-uuid", "state": {"dataMask": {}}}'
    )
    cmd = GetDashboardPermalinkCommand(dao=mock_kv_dao, key="abc12345")
    result = await cmd.execute()
    assert result["dashboardId"] == "abc-uuid"
    assert result["state"] == {"dataMask": {}}


async def test_get_dashboard_permalink_not_found(mock_kv_dao):
    mock_kv_dao.get_value = AsyncMock(return_value=None)
    cmd = GetDashboardPermalinkCommand(dao=mock_kv_dao, key="missing")
    with pytest.raises(ObjectNotFoundError):
        await cmd.execute()
