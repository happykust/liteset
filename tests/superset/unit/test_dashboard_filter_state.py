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
    # 'app' salt entries never expire; configure ``expires_on`` so the
    # DAO's expiry guard (``entry.expires_on <= datetime.now()``) does not
    # blow up on an unconfigured MagicMock attribute.
    salt_entry.expires_on = None
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


async def test_create_filter_state_with_tab_id_generates_deterministic_key(
    mock_kv_dao, mock_dashboard_dao
):
    """A truthy tab_id produces the same deterministic uuid5 key on every call.

    Mirrors the original's contextual-key cache logic
    (superset_old/commands/dashboard/filter_state/create.py:35-37):
    when ``tab_id`` is truthy and the contextual cache already holds a key,
    the same key is returned.  In liteset uuid5 replaces session+cache as the
    deterministic function.
    """
    mock_kv_dao.get_value = AsyncMock(return_value=None)  # no existing state

    cmd1 = CreateFilterStateCommand(
        dao=mock_kv_dao,
        dashboard_id=1,
        value="state1",
        user_id=42,
        tab_id="7",
        dashboard_dao=mock_dashboard_dao,
    )
    cmd2 = CreateFilterStateCommand(
        dao=mock_kv_dao,
        dashboard_id=1,
        value="state2",
        user_id=42,
        tab_id="7",
        dashboard_dao=mock_dashboard_dao,
    )
    key1 = await cmd1.execute()
    key2 = await cmd2.execute()
    # Same user + dashboard + tab_id must produce the same deterministic key.
    assert key1 == key2


async def test_create_filter_state_falsy_tab_id_generates_random_key(
    mock_kv_dao, mock_dashboard_dao
):
    """Falsy tab_id (None or empty string) always produces a fresh random key.

    Original (superset_old/commands/dashboard/filter_state/create.py:37):
    ``if not key or not tab_id: key = random_key()``
    Falsy tab_id — including the empty string sent via ``?tab_id=`` — must
    trigger the random branch, not the deterministic uuid5 branch.  The
    liteset UPDATE command already uses ``if self._tab_id:`` (truthy check);
    this test enforces the same contract for CREATE.
    """
    mock_kv_dao.get_value = AsyncMock(return_value=None)

    for falsy_tab_id in (None, ""):
        cmd1 = CreateFilterStateCommand(
            dao=mock_kv_dao,
            dashboard_id=1,
            value="v",
            user_id=1,
            tab_id=falsy_tab_id,
            dashboard_dao=mock_dashboard_dao,
        )
        cmd2 = CreateFilterStateCommand(
            dao=mock_kv_dao,
            dashboard_id=1,
            value="v",
            user_id=1,
            tab_id=falsy_tab_id,
            dashboard_dao=mock_dashboard_dao,
        )
        key1 = await cmd1.execute()
        key2 = await cmd2.execute()
        # Two calls with the same falsy tab_id must yield DIFFERENT random keys.
        assert key1 != key2, (
            f"tab_id={falsy_tab_id!r}: expected two distinct random keys, "
            f"got {key1!r} twice"
        )


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
    # 1:1 with upstream UpdateFilterStateCommand: an owned entry is updated
    # and a FRESH (rotated) key is returned, NOT the input key. With no
    # ``tab_id`` the original generates ``random_key()`` (here ``uuid4``).
    assert isinstance(result, str)
    assert len(result) > 0
    assert result != "test-key"
    # The rotated entry was persisted with the updated value.
    mock_kv_dao.set_value.assert_awaited_once()
    stored = json.loads(mock_kv_dao.set_value.call_args.kwargs["value"])
    assert stored["owner"] == 1
    assert stored["value"] == "updated"


async def test_update_filter_state_not_found(mock_kv_dao, mock_dashboard_dao):
    """Missing entry is a no-op returning the original key (HTTP 200).

    Mirrors original Superset
    (``superset_old/commands/dashboard/filter_state/update.py``): the command
    only writes / rotates the key when an entry exists. An absent entry returns
    the original key unchanged — it does NOT raise 404.
    """
    mock_kv_dao.get_value = AsyncMock(return_value=None)
    cmd = UpdateFilterStateCommand(
        dao=mock_kv_dao,
        dashboard_id=1,
        key="missing",
        value="x",
        user_id=1,
        dashboard_dao=mock_dashboard_dao,
    )
    await cmd.validate()
    result = await cmd.run()
    assert result == "missing"
    mock_kv_dao.set_value.assert_not_awaited()


async def test_update_filter_state_wrong_owner(mock_kv_dao, mock_dashboard_dao):
    """Non-owner is rejected with ``TemporaryCacheAccessDeniedError``.

    Production raises the temporary-cache variant (1:1 with upstream) which
    IS-A ``ForbiddenError``. The owner check lives in ``run`` (the original's
    ``update``), after ``validate`` clears dashboard-level access — so no write
    is performed for a non-owner.
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
    await cmd.validate()
    with pytest.raises(TemporaryCacheAccessDeniedError):
        await cmd.run()
    mock_kv_dao.set_value.assert_not_awaited()


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
