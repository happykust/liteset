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

from superset.commands.query import (
    BulkDeleteSavedQueriesCommand,
    CreateSavedQueryCommand,
    DeleteSavedQueryCommand,
    ExportSavedQueriesCommand,
    StopQueryCommand,
    UpdateSavedQueryCommand,
)
from superset.exceptions import (
    CommandInvalidError,
    SupersetSecurityException,
    ObjectNotFoundError,
)


@pytest.fixture
def mock_query_dao():
    dao = AsyncMock()
    dao.session = AsyncMock()
    dao.session.flush = AsyncMock()
    dao.session.delete = AsyncMock()
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
    mock_q = MagicMock()
    mock_q.id = 1
    mock_query_dao.find_by_ids = AsyncMock(return_value=[mock_q])
    security_manager = AsyncMock()
    security_manager.raise_for_ownership = AsyncMock(return_value=None)
    cmd = BulkDeleteSavedQueriesCommand(
        dao=mock_query_dao, ids=[1], security_manager=security_manager, user_id=1
    )
    await cmd.execute()
    mock_query_dao.session.delete.assert_awaited_once_with(mock_q)


async def test_bulk_delete_saved_queries_not_owner(mock_query_dao):
    mock_q = MagicMock()
    mock_q.id = 1
    mock_query_dao.find_by_ids = AsyncMock(return_value=[mock_q])
    security_manager = AsyncMock()
    security_manager.raise_for_ownership = AsyncMock(
        side_effect=SupersetSecurityException(message="You don't have permission")
    )
    cmd = BulkDeleteSavedQueriesCommand(
        dao=mock_query_dao, ids=[1], security_manager=security_manager, user_id=99
    )
    with pytest.raises(SupersetSecurityException):
        await cmd.execute()


async def test_export_saved_queries(mock_query_dao):
    mock_q = MagicMock()
    mock_q.label = "Test Query"
    mock_q.sql = "SELECT 1"
    mock_q.schema = "public"
    mock_q.uuid = None
    mock_db = MagicMock()
    mock_db.database_name = "test_db"
    mock_db.sqlalchemy_uri = "sqlite:///test.db"
    mock_db.uuid = None
    mock_q.database = mock_db
    mock_query_dao.find_by_id = AsyncMock(return_value=mock_q)
    cmd = ExportSavedQueriesCommand(model_ids=[1], dao=mock_query_dao)
    buf = await cmd.execute()
    assert isinstance(buf, io.BytesIO)
    with zipfile.ZipFile(buf) as zf:
        assert any("queries/" in n for n in zf.namelist())
        # Verify YAML content contains known fields
        query_files = [n for n in zf.namelist() if n.startswith("queries/")]
        content = yaml.safe_load(zf.read(query_files[0]))
        assert content["label"] == "Test Query"
        assert content["sql"] == "SELECT 1"


# --- CreateSavedQueryCommand tests ---


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

    # Patch the local import inside run() via superset.models.sql_lab module
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
        mock_query_dao.session.add.assert_called_once_with(mock_instance)
        mock_query_dao.session.flush.assert_awaited()


# --- UpdateSavedQueryCommand tests ---


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


# --- DeleteSavedQueryCommand tests ---


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
