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

from unittest.mock import AsyncMock, MagicMock

import pytest

from superset.commands.sqllab import (
    CreateSqlLabPermalinkCommand,
    EstimateQueryCostCommand,
    ExecuteSQLCommand,
    FormatSQLCommand,
    GetSqlLabPermalinkCommand,
    GetSQLResultsCommand,
)
from superset.exceptions import (
    CommandInvalidError,
    ObjectNotFoundError,
    SupersetResultsBackendNotConfigureException,
)
from superset.key_value.utils import encode_permalink_key

_PERMALINK_SALT = "permalink-salt-for-tests"


def _configure_permalink_dao(dao, *, existing_entry=None, get_value=None):
    """Wire a KV DAO so the permalink-salt + entry round-trip works.

    ``get_permalink_salt`` builds a fresh ``AsyncKeyValueDAO(dao.session)`` and
    runs ``(await session.execute(stmt)).scalars().one_or_none()`` whose
    ``.value`` is JSON-decoded into the salt string. Configure the execute
    chain to yield a pre-existing salt so the command does not try to create
    one (which would need a real session/flush).
    """
    import json  # noqa: TID251

    dao.session = AsyncMock()
    dao.session.flush = AsyncMock()
    salt_entry = MagicMock()
    salt_entry.value = json.dumps(_PERMALINK_SALT).encode("utf-8")
    # 'app' salt entries never expire; configure ``expires_on`` so the
    # DAO's expiry guard (``entry.expires_on <= datetime.now()``) does not
    # blow up on an unconfigured MagicMock attribute.
    salt_entry.expires_on = None
    res = MagicMock()
    res.scalars.return_value.one_or_none.return_value = salt_entry
    dao.session.execute = AsyncMock(return_value=res)
    dao.get_entry_by_key = AsyncMock(return_value=existing_entry)
    created = MagicMock()
    created.id = 123
    dao.create_entry = AsyncMock(return_value=created)
    dao.get_value_by_key = AsyncMock(return_value=get_value)
    return dao


async def test_execute_sql_validates_empty_sql():
    dao = AsyncMock()
    cmd = ExecuteSQLCommand(dao=dao, database_id=1, sql="  ")
    with pytest.raises(CommandInvalidError, match="empty"):
        await cmd.validate()


async def test_execute_sql_validates_no_db():
    dao = AsyncMock()
    cmd = ExecuteSQLCommand(dao=dao, database_id=0, sql="SELECT 1")
    with pytest.raises(CommandInvalidError, match="database_id"):
        await cmd.validate()


@pytest.mark.skip(
    reason=(
        "Integration-only: ExecuteSQLCommand.run() loads a real Database row, "
        "constructs/flushes a Query ORM object, parses the SQL script, builds a "
        "sync connection URI and runs the query in a worker thread. None of that "
        "is faithfully exercisable with mocks; covered by integration tests."
    )
)
async def test_execute_sql_success():
    dao = AsyncMock()
    cmd = ExecuteSQLCommand(dao=dao, database_id=1, sql="SELECT 1")
    result = await cmd.execute()
    assert result["status"] == "success"


async def test_estimate_query_cost_empty_sql():
    cmd = EstimateQueryCostCommand(database_id=1, sql="")
    with pytest.raises(CommandInvalidError):
        await cmd.validate()


async def test_estimate_query_cost_returns_result():
    # Smoke test — stub implementation, revisit in superset/remaining-api
    cmd = EstimateQueryCostCommand(database_id=1, sql="SELECT 1")
    result = await cmd.execute()
    assert isinstance(result, list)


async def test_format_sql_empty():
    cmd = FormatSQLCommand(sql="  ")
    with pytest.raises(CommandInvalidError):
        await cmd.validate()


async def test_format_sql_passthrough():
    # Smoke test — stub implementation, revisit in superset/remaining-api
    cmd = FormatSQLCommand(sql="SELECT 1")
    result = await cmd.execute()
    assert isinstance(result, str)
    assert "SELECT" in result.upper() or "select" in result.lower()


async def test_get_results_empty_key():
    cmd = GetSQLResultsCommand(key="")
    with pytest.raises(CommandInvalidError):
        await cmd.validate()


async def test_get_results_no_cache():
    # No results backend configured (test env) -> production raises the
    # not-configured exception.
    cmd = GetSQLResultsCommand(key="test-key")
    with pytest.raises(SupersetResultsBackendNotConfigureException):
        await cmd.execute()


async def test_create_sqllab_permalink():
    dao = AsyncMock()
    _configure_permalink_dao(dao)
    cmd = CreateSqlLabPermalinkCommand(dao=dao, state={"sql": "SELECT 1"})
    key = await cmd.execute()
    assert isinstance(key, str)
    assert len(key) >= 11


async def test_get_sqllab_permalink():
    dao = AsyncMock()
    _configure_permalink_dao(dao, get_value={"sql": "SELECT 1"})
    key = encode_permalink_key(key=123, salt=_PERMALINK_SALT)
    cmd = GetSqlLabPermalinkCommand(dao=dao, key=key)
    result = await cmd.execute()
    assert result == {"sql": "SELECT 1"}


async def test_get_sqllab_permalink_not_found():
    dao = AsyncMock()
    _configure_permalink_dao(dao, get_value=None)
    key = encode_permalink_key(key=123, salt=_PERMALINK_SALT)
    cmd = GetSqlLabPermalinkCommand(dao=dao, key=key)
    with pytest.raises(ObjectNotFoundError):
        await cmd.execute()


# ---------------------------------------------------------------------------
# NEW-T7: FormatSQLCommand — ImportError and SqlglotError branches
# ---------------------------------------------------------------------------


async def test_format_sql_propagates_format_error():
    """FormatSQLCommand does NOT swallow parse/format errors into the original.

    ``SQLScript(...).format()`` failures propagate (HTTP 4xx) rather than
    echoing the unformatted SQL with 200 (the previous
    swallow-and-return-original behaviour was an audited bug).
    """
    from unittest.mock import MagicMock, patch

    class _BoomError(Exception):
        pass

    cmd = FormatSQLCommand(sql="INVALID SQL %%%")
    await cmd.validate()

    mock_script = MagicMock()
    mock_script.return_value.format.side_effect = _BoomError("parse error")
    with patch("superset.sql.parse.SQLScript", mock_script):
        with pytest.raises(_BoomError):
            await cmd.run()


# ---------------------------------------------------------------------------
# NEW-T9: Cache exception paths in SQLLab GetSQLResultsCommand
# ---------------------------------------------------------------------------


async def test_get_results_cache_exception_falls_through():
    """A cache.get failure is swallowed; with no results backend configured
    the command then raises ``SupersetResultsBackendNotConfigureException``."""
    cache = MagicMock()
    cache.get = MagicMock(side_effect=RuntimeError("connection refused"))
    cmd = GetSQLResultsCommand(key="test-key", cache_manager=cache)
    await cmd.validate()
    with pytest.raises(SupersetResultsBackendNotConfigureException):
        await cmd.run()


async def test_get_results_cache_hit_returns_data():
    """GetSQLResultsCommand returns cached data on hit."""
    cached_data = {"status": "success", "data": [1, 2, 3], "columns": ["a"]}
    cache = MagicMock()
    cache.get = MagicMock(return_value=cached_data)
    cmd = GetSQLResultsCommand(key="test-key", cache_manager=cache)
    await cmd.validate()
    result = await cmd.run()
    assert result == cached_data


# ---------------------------------------------------------------------------
# Permalink error-status assertions (original: SqlLabPermalinkGetFailedError
# inherits CommandException → SupersetException.status = 500, NOT 400).
# ---------------------------------------------------------------------------


async def test_get_sqllab_permalink_decode_error_maps_to_500():
    """A malformed (non-kv:) permalink key raises an internal error with
    status_code=500 (SqlLabPermalinkGetFailedError: CommandException →
    SupersetException.status = 500).

    Pre-fix liteset raised ObjectNotFoundError (404) for decode errors,
    which diverged from the original. The current code is correct; this
    test pins the behaviour so a future "cleanup" cannot regress it.
    """
    from superset.commands.sqllab.permalink import _SqlLabPermalinkGetFailedError

    dao = AsyncMock()
    _configure_permalink_dao(dao)
    # "NOTAHASHID" is not a valid hashids-encoded key so decode_permalink_id
    # will raise KeyValueParseKeyError, which must propagate as HTTP 500.
    cmd = GetSqlLabPermalinkCommand(dao=dao, key="NOTAHASHID")
    exc = None
    with pytest.raises(_SqlLabPermalinkGetFailedError) as exc_info:
        await cmd.execute()
    exc = exc_info.value
    assert exc.status_code == 500


async def test_get_sqllab_permalink_failed_error_status_code():
    """_SqlLabPermalinkGetFailedError.status_code is 500, not 400.

    Pins the class-level attribute directly so a doc-comment stating '400'
    cannot mislead a future developer into changing the status.
    """
    from superset.commands.sqllab.permalink import _SqlLabPermalinkGetFailedError

    err = _SqlLabPermalinkGetFailedError("some error")
    assert err.status_code == 500


async def test_get_sqllab_permalink_kv_legacy_found():
    """Legacy kv:<id> key: returns parsed JSON dict from KeyValue row.

    When the row exists, ``json.loads(row.value)`` is returned directly.
    """
    import json  # noqa: TID251

    dao = AsyncMock()
    dao.session = AsyncMock()

    row = MagicMock()
    row.value = json.dumps({"sql": "SELECT kv"})
    dao.session.scalar = AsyncMock(return_value=row)

    cmd = GetSqlLabPermalinkCommand(dao=dao, key="kv:42")
    result = await cmd.execute()
    assert result == {"sql": "SELECT kv"}


async def test_get_sqllab_permalink_kv_legacy_not_found():
    """Legacy kv:<id> key: missing row raises ObjectNotFoundError (→ HTTP 404).

    The original returns None which the API converts to a 404 response; the
    liteset port raises ObjectNotFoundError which the global exception handler
    converts to the same HTTP 404.
    """
    dao = AsyncMock()
    dao.session = AsyncMock()
    dao.session.scalar = AsyncMock(return_value=None)

    cmd = GetSqlLabPermalinkCommand(dao=dao, key="kv:99")
    with pytest.raises(ObjectNotFoundError):
        await cmd.execute()


async def test_get_sqllab_permalink_kv_legacy_exception_maps_to_500():
    """Legacy kv:<id> key: unexpected exception raises HTTP-500 error.

    Any non-not-found exception in the kv: branch is
    wrapped in SqlLabPermalinkGetFailedError (status=500).
    """
    from superset.commands.sqllab.permalink import _SqlLabPermalinkGetFailedError

    dao = AsyncMock()
    dao.session = AsyncMock()
    dao.session.scalar = AsyncMock(side_effect=RuntimeError("db down"))

    cmd = GetSqlLabPermalinkCommand(dao=dao, key="kv:7")
    with pytest.raises(_SqlLabPermalinkGetFailedError) as exc_info:
        await cmd.execute()
    assert exc_info.value.status_code == 500
