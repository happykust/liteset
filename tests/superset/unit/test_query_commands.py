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

import io
import zipfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from superset.commands.query.create import CreateSavedQueryCommand
from superset.commands.query.delete import (
    BulkDeleteSavedQueriesCommand,
    DeleteSavedQueryCommand,
)
from superset.commands.query.export import ExportSavedQueriesCommand
from superset.commands.query.stop import StopQueryCommand
from superset.commands.query.update import UpdateSavedQueryCommand
from superset.errors import ErrorLevel, SupersetError, SupersetErrorType
from superset.exceptions import (
    CommandInvalidError,
    ObjectNotFoundError,
    SupersetSecurityException,
)


def _security_exception(msg: str = "denied") -> SupersetSecurityException:
    """Build a SupersetSecurityException the way production code does.

    The exception takes a ``SupersetError`` positionally (not ``message=``).
    """
    return SupersetSecurityException(
        SupersetError(
            error_type=SupersetErrorType.MISSING_OWNERSHIP_ERROR,
            message=msg,
            level=ErrorLevel.ERROR,
        )
    )


@pytest.fixture
def mock_query_dao():
    dao = AsyncMock()
    dao.session = AsyncMock()
    dao.session.flush = AsyncMock()
    dao.session.delete = AsyncMock()
    dao.session.add = MagicMock()
    # Owner-tagging (superset.tags.core.get_tag / owner-tag sync) issues
    # ``(await session.execute(stmt)).scalars().one_or_none()`` and ``.all()``
    # — both SYNC calls on the awaited result. A bare AsyncMock session makes
    # ``.scalars()`` a coroutine; configure a concrete result instead so the
    # tagging path is a no-op (no existing tags) rather than crashing the test.
    _exec_result = MagicMock()
    _exec_result.scalars.return_value.one_or_none.return_value = None
    _exec_result.scalars.return_value.all.return_value = []
    dao.session.execute = AsyncMock(return_value=_exec_result)
    # ``async with session.begin_nested():`` — a no-op async context manager.
    dao.session.begin_nested = MagicMock(return_value=AsyncMock())
    return dao


async def test_stop_query_empty_client_id(mock_query_dao):
    cmd = StopQueryCommand(dao=mock_query_dao, client_id="")
    with pytest.raises(CommandInvalidError, match="client_id"):
        await cmd.validate()


async def test_stop_query_not_found(mock_query_dao):
    mock_query_dao.stop_query = AsyncMock(return_value=None)
    cmd = StopQueryCommand(dao=mock_query_dao, client_id="abc-123")
    await cmd.validate()
    with pytest.raises(ObjectNotFoundError):
        await cmd.run()


async def test_stop_query_success(mock_query_dao):
    mock_query = MagicMock()
    mock_query.status = "stopped"
    mock_query_dao.stop_query = AsyncMock(return_value=mock_query)
    cmd = StopQueryCommand(dao=mock_query_dao, client_id="abc-123")
    await cmd.execute()
    mock_query_dao.stop_query.assert_awaited_once_with("abc-123")


async def test_bulk_delete_saved_queries_empty(mock_query_dao):
    cmd = BulkDeleteSavedQueriesCommand(dao=mock_query_dao, ids=[])
    with pytest.raises(CommandInvalidError):
        await cmd.validate()


async def test_bulk_delete_saved_queries_success(mock_query_dao):
    # In-scope query: ``find_all`` (scoped by saved_query_access_filters) returns it.
    mock_q = MagicMock()
    mock_q.id = 1
    mock_query_dao.find_all = AsyncMock(return_value=[mock_q])
    security_manager = AsyncMock()
    cmd = BulkDeleteSavedQueriesCommand(
        dao=mock_query_dao, ids=[1], security_manager=security_manager, user_id=1
    )
    await cmd.execute()
    mock_query_dao.session.delete.assert_awaited_once_with(mock_q)


async def test_bulk_delete_saved_queries_not_owner(mock_query_dao):
    # Out-of-scope ids are filtered out by ``saved_query_access_filters`` (the
    # FAB base_filter, created_by-scoped, NO admin bypass) → ``find_all``
    # returns empty → ObjectNotFoundError (404), NOT a 403. SavedQuery has no
    # ``owners`` M2M, so the old ``raise_for_ownership`` path was wrong.
    mock_query_dao.find_all = AsyncMock(return_value=[])
    security_manager = AsyncMock()
    cmd = BulkDeleteSavedQueriesCommand(
        dao=mock_query_dao, ids=[1], security_manager=security_manager, user_id=99
    )
    with pytest.raises(ObjectNotFoundError):
        await cmd.execute()


async def test_export_saved_queries(mock_query_dao):
    mock_q = MagicMock()
    mock_q.label = "Test Query"  # used by _file_name (secure_filename needs str)
    mock_q.uuid = "sq-uuid-1"
    mock_q.schema = "public"  # _file_name branches on schema (None vs str)
    # The export builds its payload from ``export_to_dict`` (not field reads),
    # so it must return a real, YAML-serializable dict.
    mock_q.export_to_dict.return_value = {
        "label": "Test Query",
        "sql": "SELECT 1",
        "schema": "public",
    }
    mock_db = MagicMock()
    mock_db.database_name = "test_db"
    mock_db.uuid = "db-uuid-1"
    mock_db.export_to_dict.return_value = {
        "database_name": "test_db",
        "sqlalchemy_uri": "sqlite:///test.db",
    }
    mock_q.database = mock_db
    # validate() applies the user-scoped access filter (IDOR fix): calls
    # ``dao.count(filters=...)`` and raises ObjectNotFoundError unless the
    # accessible count == requested count.
    # Mock the count to report the single requested id as accessible (owned).
    mock_query_dao.count = AsyncMock(return_value=1)
    # The export command loads the query via ``session.execute(...).scalars()
    # .one_or_none()`` (eager-loads database) — not ``find_by_id``.
    _res = MagicMock()
    _res.scalars.return_value.one_or_none.return_value = mock_q
    mock_query_dao.session.execute = AsyncMock(return_value=_res)
    security_manager = AsyncMock()
    user = MagicMock()
    user.id = 1
    cmd = ExportSavedQueriesCommand(
        model_ids=[1],
        dao=mock_query_dao,
        security_manager=security_manager,
        user=user,
    )
    buf = await cmd.execute()
    assert isinstance(buf, io.BytesIO)
    with zipfile.ZipFile(buf) as zf:
        assert any("queries/" in n for n in zf.namelist())
        # Verify YAML content contains known fields
        query_files = [n for n in zf.namelist() if n.startswith("queries/")]
        content = yaml.safe_load(zf.read(query_files[0]))
        assert content["label"] == "Test Query"
        assert content["sql"] == "SELECT 1"


async def test_create_saved_query_validates_label(mock_query_dao):
    cmd = CreateSavedQueryCommand(dao=mock_query_dao, data={"sql": "SELECT 1"})
    with pytest.raises(CommandInvalidError, match="label"):
        await cmd.validate()


async def test_create_saved_query_validates_sql(mock_query_dao):
    cmd = CreateSavedQueryCommand(dao=mock_query_dao, data={"label": "Test"})
    with pytest.raises(CommandInvalidError, match="sql"):
        await cmd.validate()


async def test_create_saved_query_success(mock_query_dao):
    mock_query_dao.session.add = MagicMock()

    mock_instance = MagicMock()
    mock_instance.id = 42
    mock_instance.label = "My Query"
    mock_instance.sql = "SELECT 1"
    mock_saved_query_cls = MagicMock(return_value=mock_instance)

    with patch.dict(
        "sys.modules",
        {
            "superset.models.sql_lab": MagicMock(SavedQuery=mock_saved_query_cls),
        },
    ):
        cmd = CreateSavedQueryCommand(
            dao=mock_query_dao,
            data={"label": "My Query", "sql": "SELECT 1", "db_id": 1},
            user_id=5,
        )
        result = await cmd.execute()
        assert result.id == 42
        assert result.created_by_fk == 5
        # ``session.add`` is now called more than once (the SavedQuery plus the
        # new owner Tag created by the owner-tagging path), so assert the
        # SavedQuery instance was among the adds.
        mock_query_dao.session.add.assert_any_call(mock_instance)
        mock_query_dao.session.flush.assert_awaited()


async def test_update_saved_query_not_found(mock_query_dao):
    mock_query_dao.find_by_id = AsyncMock(return_value=None)
    cmd = UpdateSavedQueryCommand(
        dao=mock_query_dao, query_id=999, data={"label": "New"}
    )
    with pytest.raises(ObjectNotFoundError):
        await cmd.execute()


async def test_update_saved_query_success(mock_query_dao):
    mock_q = MagicMock()
    mock_q.id = 1
    mock_q.label = "Old"
    mock_q.sql = "SELECT 1"
    mock_query_dao.find_by_id = AsyncMock(return_value=mock_q)
    cmd = UpdateSavedQueryCommand(
        dao=mock_query_dao, query_id=1, data={"label": "New Label"}
    )
    result = await cmd.execute()
    assert result.label == "New Label"
    mock_query_dao.session.flush.assert_awaited()


async def test_delete_saved_query_not_found(mock_query_dao):
    mock_query_dao.find_by_id = AsyncMock(return_value=None)
    cmd = DeleteSavedQueryCommand(dao=mock_query_dao, query_id=999)
    with pytest.raises(ObjectNotFoundError):
        await cmd.execute()


async def test_delete_saved_query_success(mock_query_dao):
    mock_q = MagicMock()
    mock_q.id = 1
    mock_query_dao.find_by_id = AsyncMock(return_value=mock_q)
    cmd = DeleteSavedQueryCommand(dao=mock_query_dao, query_id=1)
    await cmd.execute()
    mock_query_dao.session.delete.assert_awaited_once_with(mock_q)
    mock_query_dao.session.flush.assert_awaited()
