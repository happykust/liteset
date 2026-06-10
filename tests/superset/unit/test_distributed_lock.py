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

``sync_key_value_distributed_lock`` tests pin the synchronous sibling
behaviour, in particular that on an exception from the body the lock is
*not* released (matches the original, which has no try/finally around the
yield — the row simply expires after LOCK_EXPIRATION).
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from superset.distributed_lock import (
    KeyValueDistributedLock,
    sync_key_value_distributed_lock,
)
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


# ---------------------------------------------------------------------------
# sync_key_value_distributed_lock — 1:1 with the upstream sync original.
# The original has NO try/finally around the yield: if the body raises, the
# lock row stays in place until LOCK_EXPIRATION (30 s).  These tests assert
# that contract.
# ---------------------------------------------------------------------------


def _make_sync_session(
    existing_lock=None,
    release_row=None,
):
    """Return (ctx_manager, mock_session) for patching ``get_sync_session``.

    ``existing_lock`` is returned by the first ``one_or_none()`` call (the
    contention check).  ``release_row`` is returned by the second
    ``one_or_none()`` call (the delete-on-release lookup).
    """
    session = MagicMock()
    query = MagicMock()
    query.filter.return_value = query
    query.one_or_none.side_effect = [existing_lock, release_row]
    session.query.return_value = query

    @contextmanager
    def _ctx():
        yield session

    return _ctx, session


def test_sync_lock_acquired_and_released_on_happy_path() -> None:
    """Happy path: body exits normally -> delete IS called (lock released)."""
    row_mock = MagicMock()
    ctx_factory, session = _make_sync_session(existing_lock=None, release_row=row_mock)

    with patch("superset.db.session.get_sync_session", ctx_factory):
        with sync_key_value_distributed_lock("ns", user_id=1) as key:
            assert key == get_key("ns", user_id=1)
            # Lock row not yet deleted during body execution.
            session.delete.assert_not_called()

    # After context exit, the lock row was deleted.
    session.delete.assert_called_once_with(row_mock)
    assert session.commit.call_count >= 1


def test_sync_lock_not_released_on_exception() -> None:
    """When the body raises, the lock row must NOT be deleted (stays until
    expiry).  This mirrors the original, which has no try/finally.  Releasing
    early on exception would remove the natural back-pressure against a
    failing IDP and allow concurrent callers to re-enter immediately."""
    row_mock = MagicMock()
    ctx_factory, session = _make_sync_session(existing_lock=None, release_row=row_mock)

    with patch("superset.db.session.get_sync_session", ctx_factory):
        with pytest.raises(RuntimeError, match="body error"):
            with sync_key_value_distributed_lock("ns", user_id=1):
                raise RuntimeError("body error")

    # The delete must NOT have been called — lock stays alive until expiry.
    session.delete.assert_not_called()


def test_sync_lock_already_taken_raises() -> None:
    """When the lock row already exists, raises immediately without creating
    a new entry and without calling delete."""
    ctx_factory, session = _make_sync_session(existing_lock=MagicMock())

    with patch("superset.db.session.get_sync_session", ctx_factory):
        with pytest.raises(CreateKeyValueDistributedLockFailedException):
            with sync_key_value_distributed_lock("ns", user_id=1):
                pass

    # Never created a new entry, never deleted anything.
    session.add.assert_not_called()
    session.delete.assert_not_called()
