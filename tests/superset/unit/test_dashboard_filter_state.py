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
from unittest.mock import AsyncMock, MagicMock

import pytest

from superset.commands.dashboard.filter_state.create import CreateFilterStateCommand
from superset.commands.dashboard.filter_state.delete import DeleteFilterStateCommand
from superset.commands.dashboard.filter_state.get import GetFilterStateCommand
from superset.commands.dashboard.filter_state.update import UpdateFilterStateCommand
from superset.commands.dashboard.permalink.create import (
    CreateDashboardPermalinkCommand,
)
from superset.commands.dashboard.permalink.get import GetDashboardPermalinkCommand
from superset.commands.temporary_cache.exceptions import (
    TemporaryCacheAccessDeniedError,
    TemporaryCacheResourceNotFoundError,
)
from superset.exceptions import ObjectNotFoundError


@pytest.fixture
def mock_dashboard_dao():
    """Dashboard DAO used by ``check_access`` in the filter-state commands.

    ``check_access`` calls ``get_full_by_id_or_slug`` and raises
    ``TemporaryCacheResourceNotFoundError`` when the dashboard is missing.
    With no ``security_manager``/``user`` wired, existence is all it checks.
    """
    dao = AsyncMock()
    dao.get_full_by_id_or_slug = AsyncMock(return_value=MagicMock())
    return dao


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


def _configure_permalink_dao(dao, *, existing_entry=None, get_value=None):
    """Wire a KV DAO so the permalink-salt + entry round-trip works.

    ``get_permalink_salt`` builds a fresh ``AsyncKeyValueDAO(dao.session)``
    and runs ``(await session.execute(stmt)).scalars().one_or_none()`` whose
    ``.value`` is JSON-decoded into the salt string. Configure the execute
    chain to yield a pre-existing salt so the command does not try to create
    one (which would need a real session/flush).
    """
    dao.session = AsyncMock()
    dao.session.flush = AsyncMock()
    salt_entry = MagicMock()
    salt_entry.value = json.dumps("permalink-salt-for-tests").encode("utf-8")
    res = MagicMock()
    res.scalars.return_value.one_or_none.return_value = salt_entry
    dao.session.execute = AsyncMock(return_value=res)
    # Create/get permalink-entry surface (used by Create command).
    dao.get_entry_by_key = AsyncMock(return_value=existing_entry)
    created = MagicMock()
    created.id = 123
    dao.create_entry = AsyncMock(return_value=created)
    # Used by Get command (must be set explicitly: a bare AsyncMock returns a
    # truthy MagicMock, masking the not-found path).
    dao.get_value_by_key = AsyncMock(return_value=get_value)
    return dao


async def test_create_filter_state(mock_kv_dao, mock_dashboard_dao):
    cmd = CreateFilterStateCommand(
        dao=mock_kv_dao,
        dashboard_id=1,
        value='{"key": "val"}',
        user_id=1,
        dashboard_dao=mock_dashboard_dao,
    )
    key = await cmd.execute()
    assert isinstance(key, str)
    assert len(key) > 0
    mock_kv_dao.set_value.assert_awaited_once()
    # Verify envelope format
    stored = json.loads(mock_kv_dao.set_value.call_args.kwargs["value"])
    assert stored["owner"] == 1
    assert stored["value"] == '{"key": "val"}'


async def test_get_filter_state(mock_kv_dao, mock_dashboard_dao):
    cmd = GetFilterStateCommand(
        dao=mock_kv_dao,
        dashboard_id=1,
        key="test-key",
        dashboard_dao=mock_dashboard_dao,
    )
    value = await cmd.execute()
    # Should unwrap the envelope and return inner value
    assert value == '{"filters": []}'


async def test_get_filter_state_not_found(mock_kv_dao, mock_dashboard_dao):
    mock_kv_dao.get_value = AsyncMock(return_value=None)
    cmd = GetFilterStateCommand(
        dao=mock_kv_dao,
        dashboard_id=1,
        key="missing",
        dashboard_dao=mock_dashboard_dao,
    )
    with pytest.raises(ObjectNotFoundError):
        await cmd.execute()


async def test_update_filter_state(mock_kv_dao, mock_dashboard_dao):
    # Existing entry owned by user 1
    mock_kv_dao.get_value = AsyncMock(
        return_value=json.dumps({"owner": 1, "value": "old"})
    )
    cmd = UpdateFilterStateCommand(
        dao=mock_kv_dao,
        dashboard_id=1,
        key="test-key",
        value="updated",
        user_id=1,
        dashboard_dao=mock_dashboard_dao,
    )
    result = await cmd.execute()
    assert result == "test-key"


async def test_update_filter_state_not_found(mock_kv_dao, mock_dashboard_dao):
    mock_kv_dao.get_value = AsyncMock(return_value=None)
    cmd = UpdateFilterStateCommand(
        dao=mock_kv_dao,
        dashboard_id=1,
        key="missing",
        value="x",
        user_id=1,
        dashboard_dao=mock_dashboard_dao,
    )
    with pytest.raises(ObjectNotFoundError):
        await cmd.validate()


async def test_update_filter_state_wrong_owner(mock_kv_dao, mock_dashboard_dao):
    """Non-owner is rejected with ``TemporaryCacheAccessDeniedError``.

    Production raises the temporary-cache variant (1:1 with upstream) which
    IS-A ``ForbiddenError``.
    """
    mock_kv_dao.get_value = AsyncMock(
        return_value=json.dumps({"owner": 99, "value": "old"})
    )
    cmd = UpdateFilterStateCommand(
        dao=mock_kv_dao,
        dashboard_id=1,
        key="test-key",
        value="hacked",
        user_id=1,
        dashboard_dao=mock_dashboard_dao,
    )
    with pytest.raises(TemporaryCacheAccessDeniedError):
        await cmd.validate()


async def test_delete_filter_state(mock_kv_dao, mock_dashboard_dao):
    cmd = DeleteFilterStateCommand(
        dao=mock_kv_dao,
        dashboard_id=1,
        key="test-key",
        dashboard_dao=mock_dashboard_dao,
    )
    result = await cmd.execute()
    assert result is True


async def test_check_access_dashboard_missing(mock_kv_dao):
    """Missing dashboard surfaces ``TemporaryCacheResourceNotFoundError``."""
    dashboard_dao = AsyncMock()
    dashboard_dao.get_full_by_id_or_slug = AsyncMock(return_value=None)
    cmd = GetFilterStateCommand(
        dao=mock_kv_dao,
        dashboard_id=1,
        key="test-key",
        dashboard_dao=dashboard_dao,
    )
    with pytest.raises(TemporaryCacheResourceNotFoundError):
        await cmd.validate()


async def test_create_dashboard_permalink(mock_kv_dao):
    _configure_permalink_dao(mock_kv_dao)
    cmd = CreateDashboardPermalinkCommand(
        dao=mock_kv_dao,
        dashboard_id=1,
        state={"dataMask": {}, "activeTabs": []},
        dashboard_uuid="550e8400-e29b-41d4-a716-446655440000",
    )
    key = await cmd.execute()
    assert isinstance(key, str)
    assert len(key) >= 11
    # New entry — payload is stored via ``create_entry(value=...)``.
    stored = json.loads(mock_kv_dao.create_entry.call_args.kwargs["value"])
    assert stored["dashboardId"] == "550e8400-e29b-41d4-a716-446655440000"
    assert "state" in stored
    assert stored["state"]["dataMask"] == {}


async def test_create_dashboard_permalink_fallback_int_id(mock_kv_dao):
    """Without dashboard_uuid, falls back to int id."""
    _configure_permalink_dao(mock_kv_dao)
    cmd = CreateDashboardPermalinkCommand(
        dao=mock_kv_dao, dashboard_id=42, state={"dataMask": {}}
    )
    await cmd.execute()
    stored = json.loads(mock_kv_dao.create_entry.call_args.kwargs["value"])
    assert stored["dashboardId"] == 42


async def test_get_dashboard_permalink(mock_kv_dao):
    payload = {"dashboardId": "abc-uuid", "state": {"dataMask": {}}}
    _configure_permalink_dao(mock_kv_dao, get_value=payload)
    # Encode a key that round-trips with the test salt so decode succeeds.
    from superset.key_value.utils import encode_permalink_key

    key = encode_permalink_key(key=123, salt="permalink-salt-for-tests")
    cmd = GetDashboardPermalinkCommand(dao=mock_kv_dao, key=key)
    result = await cmd.execute()
    assert result["dashboardId"] == "abc-uuid"
    assert result["state"] == {"dataMask": {}}


async def test_get_dashboard_permalink_not_found(mock_kv_dao):
    _configure_permalink_dao(mock_kv_dao, get_value=None)
    from superset.key_value.utils import encode_permalink_key

    key = encode_permalink_key(key=123, salt="permalink-salt-for-tests")
    cmd = GetDashboardPermalinkCommand(dao=mock_kv_dao, key=key)
    with pytest.raises(ObjectNotFoundError):
        await cmd.execute()
