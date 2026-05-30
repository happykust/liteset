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
"""Tests for the KV-store distributed lock.

Liteset does NOT use a Redis ``AsyncDistributedLock`` — the lock is a
metadata-DB / key-value construct: :func:`KeyValueDistributedLock` is an
``@asynccontextmanager`` that checks contention via ``GetDistributedLock``,
acquires via ``CreateDistributedLock``, and always releases via
``DeleteDistributedLock`` (``superset/distributed_lock/__init__.py``). These
tests pin the acquire/contend/release flow and the deterministic key.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from superset.distributed_lock import KeyValueDistributedLock
from superset.distributed_lock.utils import get_key
from superset.exceptions import CreateKeyValueDistributedLockFailedException


def _command(run_return=None, run_side_effect=None) -> MagicMock:
    """A command class stand-in whose instances expose an async ``run()``."""
    cmd_cls = MagicMock()
    instance = MagicMock()
    instance.run = AsyncMock(return_value=run_return, side_effect=run_side_effect)
    cmd_cls.return_value = instance
    return cmd_cls


def _patch_commands(get_cmd, create_cmd, delete_cmd):
    return (
        patch("superset.commands.distributed_lock.get.GetDistributedLock", get_cmd),
        patch(
            "superset.commands.distributed_lock.create.CreateDistributedLock",
            create_cmd,
        ),
        patch(
            "superset.commands.distributed_lock.delete.DeleteDistributedLock",
            delete_cmd,
        ),
    )


def test_get_key_is_deterministic() -> None:
    # Identical namespace + params (same insertion order) -> same key.
    k1 = get_key("refresh_oauth2_token", user_id=1, database_id=2)
    k2 = get_key("refresh_oauth2_token", user_id=1, database_id=2)
    assert isinstance(k1, uuid.UUID)
    assert k1 == k2
    # Different namespace -> different key.
    assert k1 != get_key("other_namespace", user_id=1, database_id=2)
    # NOTE: ``serialize`` is ``json.dumps(params)`` (the upstream ``sort``
    # helper is dead code), so the key is insertion-order dependent — a
    # faithful quirk of the 1:1 port, asserted here so it can't regress silently.
    assert get_key("ns", user_id=1, database_id=2) != get_key(
        "ns", database_id=2, user_id=1
    )


async def test_lock_acquired_and_released_when_free() -> None:
    session = AsyncMock()
    get_cmd = _command(run_return=None)  # not currently held
    create_cmd = _command()
    delete_cmd = _command()
    p_get, p_create, p_delete = _patch_commands(get_cmd, create_cmd, delete_cmd)
    with p_get, p_create, p_delete:
        async with KeyValueDistributedLock("ns", session, user_id=1) as key:
            assert key == get_key("ns", user_id=1)
            create_cmd.return_value.run.assert_awaited_once()
            # Not released until the context exits.
            delete_cmd.return_value.run.assert_not_awaited()
    # Released on exit.
    delete_cmd.return_value.run.assert_awaited_once()


async def test_lock_already_taken_raises() -> None:
    session = AsyncMock()
    get_cmd = _command(run_return={"value": "taken"})  # contended
    create_cmd = _command()
    delete_cmd = _command()
    p_get, p_create, p_delete = _patch_commands(get_cmd, create_cmd, delete_cmd)
    with p_get, p_create, p_delete:
        with pytest.raises(CreateKeyValueDistributedLockFailedException):
            async with KeyValueDistributedLock("ns", session, user_id=1):
                pass
    # Never acquired -> never created, never released.
    create_cmd.return_value.run.assert_not_awaited()
    delete_cmd.return_value.run.assert_not_awaited()


async def test_lock_create_contention_raises() -> None:
    session = AsyncMock()
    get_cmd = _command(run_return=None)  # looked free...
    # ...but CreateDistributedLock loses the race (unique-key violation).
    create_cmd = _command(
        run_side_effect=CreateKeyValueDistributedLockFailedException("taken")
    )
    delete_cmd = _command()
    p_get, p_create, p_delete = _patch_commands(get_cmd, create_cmd, delete_cmd)
    with p_get, p_create, p_delete:
        with pytest.raises(CreateKeyValueDistributedLockFailedException):
            async with KeyValueDistributedLock("ns", session, user_id=1):
                pass
    delete_cmd.return_value.run.assert_not_awaited()
