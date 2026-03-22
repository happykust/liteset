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
"""Tests for database command classes with mocked DAOs."""

from __future__ import annotations

import io
import zipfile
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from liteset.commands.database import (
    CreateDatabaseCommand,
    DatabaseTestConnectionCommand,
    DeleteDatabaseCommand,
    DeleteSSHTunnelCommand,
    ExportDatabasesCommand,
    ImportDatabasesCommand,
    SyncPermissionsCommand,
    UpdateDatabaseCommand,
    UploadCommand,
    ValidateParametersCommand,
    ValidateSQLCommand,
)
from liteset.exceptions import (
    CommandInvalidError,
    LitesetSecurityException,
    ObjectNotFoundError,
)


@pytest.fixture
def mock_dao():
    dao = AsyncMock()
    dao.session = AsyncMock()
    dao.session.add = MagicMock()
    dao.session.flush = AsyncMock()
    dao.session.delete = AsyncMock()
    return dao


@pytest.fixture
def mock_database():
    db = MagicMock()
    db.id = 1
    db.database_name = "test_db"
    db.sqlalchemy_uri = "sqlite:///test.db"
    db.cache_timeout = None
    db.expose_in_sqllab = True
    db.allow_run_async = False
    db.allow_ctas = False
    db.allow_cvas = False
    db.allow_dml = False
    db.allow_file_upload = False
    db.extra = ""
    db.uuid = None
    db.backend = "sqlite"
    return db


# ---------------------------------------------------------------------------
# CreateDatabaseCommand
# ---------------------------------------------------------------------------


async def test_create_database_validates_name_required(mock_dao):
    cmd = CreateDatabaseCommand(dao=mock_dao, data={})
    with pytest.raises(CommandInvalidError, match="database_name"):
        await cmd.validate()


async def test_create_database_validates_uniqueness(mock_dao):
    mock_dao.validate_uniqueness = AsyncMock(return_value=False)
    cmd = CreateDatabaseCommand(
        dao=mock_dao,
        data={
            "database_name": "existing_db",
            "sqlalchemy_uri": "postgresql://localhost/db",
        },
    )
    with pytest.raises(CommandInvalidError, match="already exists"):
        await cmd.validate()


async def test_create_database_validates_success(mock_dao):
    mock_dao.validate_uniqueness = AsyncMock(return_value=True)
    cmd = CreateDatabaseCommand(
        dao=mock_dao,
        data={"database_name": "new_db", "sqlalchemy_uri": "postgresql://localhost/db"},
        user_id=1,
    )
    await cmd.validate()  # Should not raise


# ---------------------------------------------------------------------------
# UpdateDatabaseCommand
# ---------------------------------------------------------------------------


async def test_update_database_not_found(mock_dao):
    mock_dao.find_by_id = AsyncMock(return_value=None)
    cmd = UpdateDatabaseCommand(
        dao=mock_dao, database_id=999, data={"database_name": "X"}
    )
    with pytest.raises(ObjectNotFoundError):
        await cmd.validate()


async def test_update_database_duplicate_name(mock_dao, mock_database):
    mock_dao.find_by_id = AsyncMock(return_value=mock_database)
    mock_dao.validate_update_uniqueness = AsyncMock(return_value=False)
    cmd = UpdateDatabaseCommand(
        dao=mock_dao,
        database_id=1,
        data={"database_name": "duplicate"},
    )
    with pytest.raises(CommandInvalidError, match="already exists"):
        await cmd.validate()


async def test_update_database_success(mock_dao, mock_database):
    mock_dao.find_by_id = AsyncMock(return_value=mock_database)
    mock_dao.validate_update_uniqueness = AsyncMock(return_value=True)
    cmd = UpdateDatabaseCommand(
        dao=mock_dao,
        database_id=1,
        data={"database_name": "updated_db"},
    )
    await cmd.validate()
    result = await cmd.run()
    assert result.database_name == "updated_db"
    mock_dao.session.flush.assert_awaited_once()


# ---------------------------------------------------------------------------
# DeleteDatabaseCommand
# ---------------------------------------------------------------------------


async def test_delete_database_not_found(mock_dao):
    mock_dao.find_by_id = AsyncMock(return_value=None)
    cmd = DeleteDatabaseCommand(dao=mock_dao, database_id=999)
    with pytest.raises(ObjectNotFoundError):
        await cmd.validate()


async def test_delete_database_success(mock_dao, mock_database):
    mock_dao.find_by_id = AsyncMock(return_value=mock_database)
    mock_dao.has_dependent_datasets = AsyncMock(return_value=False)
    mock_dao.find_report_schedules_by_database_id = AsyncMock(return_value=[])
    cmd = DeleteDatabaseCommand(dao=mock_dao, database_id=1)
    await cmd.validate()
    await cmd.run()
    mock_dao.session.delete.assert_awaited_once_with(mock_database)


# ---------------------------------------------------------------------------
# DatabaseTestConnectionCommand
# ---------------------------------------------------------------------------


async def test_test_connection_validates_uri_required(mock_dao):
    cmd = DatabaseTestConnectionCommand(dao=mock_dao, data={})
    with pytest.raises(CommandInvalidError, match="sqlalchemy_uri"):
        await cmd.validate()


async def test_test_connection_validates_success(mock_dao):
    cmd = DatabaseTestConnectionCommand(
        dao=mock_dao,
        data={"sqlalchemy_uri": "sqlite:///test.db"},
    )
    await cmd.validate()  # Should not raise


async def test_test_connection_run(mock_dao):
    cmd = DatabaseTestConnectionCommand(
        dao=mock_dao,
        data={"sqlalchemy_uri": "sqlite:///test.db"},
    )
    await cmd.validate()
    result = await cmd.run()
    assert result["message"] == "OK"


# ---------------------------------------------------------------------------
# ValidateSQLCommand
# ---------------------------------------------------------------------------


async def test_validate_sql_empty_query(mock_dao):
    cmd = ValidateSQLCommand(dao=mock_dao, database_id=1, sql="")
    with pytest.raises(CommandInvalidError, match="SQL query"):
        await cmd.validate()


async def test_validate_sql_database_not_found(mock_dao):
    mock_dao.find_by_id = AsyncMock(return_value=None)
    cmd = ValidateSQLCommand(dao=mock_dao, database_id=999, sql="SELECT 1")
    with pytest.raises(ObjectNotFoundError):
        await cmd.validate()


async def test_validate_sql_success(mock_dao, mock_database):
    mock_dao.find_by_id = AsyncMock(return_value=mock_database)
    cmd = ValidateSQLCommand(dao=mock_dao, database_id=1, sql="SELECT 1")
    result = await cmd.execute()
    assert result == {"result": []}


# ---------------------------------------------------------------------------
# ValidateParametersCommand
# ---------------------------------------------------------------------------


async def test_validate_parameters_engine_required():
    cmd = ValidateParametersCommand(data={})
    with pytest.raises(CommandInvalidError, match="engine"):
        await cmd.validate()


async def test_validate_parameters_success():
    cmd = ValidateParametersCommand(data={"engine": "postgresql"})
    result = await cmd.execute()
    assert result == {"errors": []}


# ---------------------------------------------------------------------------
# ExportDatabasesCommand
# ---------------------------------------------------------------------------


async def test_export_databases_produces_zip(mock_dao, mock_database):
    mock_dao.find_by_id = AsyncMock(return_value=mock_database)
    cmd = ExportDatabasesCommand(model_ids=[1], dao=mock_dao)
    buf = await cmd.execute()
    assert isinstance(buf, io.BytesIO)
    with zipfile.ZipFile(buf) as zf:
        names = zf.namelist()
        assert any("databases/" in n for n in names)
        assert "metadata.yaml" in names
        # Verify YAML content contains known fields
        db_files = [n for n in names if n.startswith("databases/")]
        content = yaml.safe_load(zf.read(db_files[0]))
        assert content["database_name"] == "test_db"
        assert content["sqlalchemy_uri"] == "sqlite:///test.db"


async def test_export_databases_not_found(mock_dao):
    mock_dao.find_by_id = AsyncMock(return_value=None)
    cmd = ExportDatabasesCommand(model_ids=[999], dao=mock_dao)
    with pytest.raises(ObjectNotFoundError):
        await cmd.execute()


async def test_export_databases_no_dao():
    cmd = ExportDatabasesCommand(model_ids=[1], dao=None)
    with pytest.raises(CommandInvalidError, match="DAO not provided"):
        await cmd.execute()


# ---------------------------------------------------------------------------
# ImportDatabasesCommand
# ---------------------------------------------------------------------------


def _make_import_zip(db_name: str = "imported_db") -> io.BytesIO:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "databases/test.yaml",
            yaml.safe_dump(
                {"database_name": db_name, "sqlalchemy_uri": "sqlite:///"},
            ),
        )
        zf.writestr(
            "metadata.yaml",
            yaml.safe_dump({"version": "1.0.0", "type": "Database"}),
        )
    buf.seek(0)
    return buf


async def test_import_databases_success(mock_dao):
    mock_dao.create = AsyncMock(return_value=MagicMock())
    buf = _make_import_zip()
    cmd = ImportDatabasesCommand(contents=buf, dao=mock_dao)
    await cmd.execute()
    mock_dao.create.assert_awaited_once()
    mock_dao.session.flush.assert_awaited()


async def test_import_databases_missing_name(mock_dao):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "databases/bad.yaml",
            yaml.safe_dump({"sqlalchemy_uri": "sqlite:///"}),
        )
        zf.writestr(
            "metadata.yaml",
            yaml.safe_dump({"version": "1.0.0", "type": "Database"}),
        )
    buf.seek(0)
    cmd = ImportDatabasesCommand(contents=buf, dao=mock_dao)
    with pytest.raises(CommandInvalidError, match="Missing database_name"):
        await cmd.execute()


# ---------------------------------------------------------------------------
# UploadCommand
# ---------------------------------------------------------------------------


async def test_upload_validates_table_name(mock_dao):
    cmd = UploadCommand(dao=mock_dao, database_id=1, data={}, file_contents=b"data")
    with pytest.raises(CommandInvalidError, match="table_name"):
        await cmd.validate()


async def test_upload_database_not_found(mock_dao):
    mock_dao.find_by_id = AsyncMock(return_value=None)
    cmd = UploadCommand(
        dao=mock_dao,
        database_id=999,
        data={"table_name": "tbl"},
        file_contents=b"data",
    )
    with pytest.raises(ObjectNotFoundError):
        await cmd.validate()


async def test_upload_success(mock_dao, mock_database):
    mock_dao.find_by_id = AsyncMock(return_value=mock_database)
    cmd = UploadCommand(
        dao=mock_dao,
        database_id=1,
        data={"table_name": "tbl"},
        file_contents=b"data",
    )
    result = await cmd.execute()
    assert result["message"] == "OK"


# ---------------------------------------------------------------------------
# SyncPermissionsCommand
# ---------------------------------------------------------------------------


async def test_sync_permissions_not_found(mock_dao):
    mock_dao.find_by_id = AsyncMock(return_value=None)
    cmd = SyncPermissionsCommand(dao=mock_dao, database_id=999)
    with pytest.raises(ObjectNotFoundError):
        await cmd.validate()


async def test_sync_permissions_success(mock_dao, mock_database):
    mock_dao.find_by_id = AsyncMock(return_value=mock_database)
    cmd = SyncPermissionsCommand(dao=mock_dao, database_id=1)
    result = await cmd.execute()
    assert result["message"] == "OK"


# ---------------------------------------------------------------------------
# DeleteSSHTunnelCommand
# ---------------------------------------------------------------------------


async def test_delete_ssh_tunnel_not_found(mock_dao):
    mock_dao.get_ssh_tunnel = AsyncMock(return_value=None)
    cmd = DeleteSSHTunnelCommand(dao=mock_dao, database_id=1)
    with pytest.raises(ObjectNotFoundError):
        await cmd.validate()


async def test_delete_ssh_tunnel_success(mock_dao):
    tunnel = MagicMock()
    mock_dao.get_ssh_tunnel = AsyncMock(return_value=tunnel)
    cmd = DeleteSSHTunnelCommand(dao=mock_dao, database_id=1)
    await cmd.validate()
    await cmd.run()
    mock_dao.session.delete.assert_awaited_once_with(tunnel)


# ---------------------------------------------------------------------------
# Ownership checks & dependent datasets
# ---------------------------------------------------------------------------


async def test_delete_non_owner_raises_forbidden(mock_dao, mock_database):
    mock_dao.find_by_id = AsyncMock(return_value=mock_database)
    sm = AsyncMock()
    sm.raise_for_ownership = AsyncMock(
        side_effect=LitesetSecurityException(message="You don't have permission")
    )
    cmd = DeleteDatabaseCommand(
        dao=mock_dao, database_id=1, security_manager=sm, user_id=42
    )
    with pytest.raises(LitesetSecurityException, match="permission"):
        await cmd.validate()


async def test_delete_with_dependent_datasets_raises(mock_dao, mock_database):
    mock_dao.find_by_id = AsyncMock(return_value=mock_database)
    mock_dao.has_dependent_datasets = AsyncMock(return_value=True)
    cmd = DeleteDatabaseCommand(dao=mock_dao, database_id=1)
    with pytest.raises(CommandInvalidError, match="dependent dataset"):
        await cmd.validate()


# ---------------------------------------------------------------------------
# NEW-T1: URI scheme validation in CreateDatabaseCommand.run()
# ---------------------------------------------------------------------------


async def test_create_database_blocks_file_uri(mock_dao):
    """file:// URI scheme must be rejected in validate()."""
    mock_dao.validate_uniqueness = AsyncMock(return_value=True)
    cmd = CreateDatabaseCommand(
        dao=mock_dao,
        data={"database_name": "evil_db", "sqlalchemy_uri": "file:///etc/passwd"},
    )
    with pytest.raises(CommandInvalidError, match="not allowed"):
        await cmd.validate()


async def test_create_database_blocks_sqlite_uri(mock_dao):
    """sqlite:// URI scheme must be rejected in validate()."""
    mock_dao.validate_uniqueness = AsyncMock(return_value=True)
    cmd = CreateDatabaseCommand(
        dao=mock_dao,
        data={"database_name": "local_db", "sqlalchemy_uri": "sqlite:///test.db"},
    )
    with pytest.raises(CommandInvalidError, match="not allowed"):
        await cmd.validate()


async def test_create_database_blocks_sqlite_plus_driver_uri(mock_dao):
    """sqlite+pysqlite:// URI scheme must be rejected in validate()."""
    mock_dao.validate_uniqueness = AsyncMock(return_value=True)
    cmd = CreateDatabaseCommand(
        dao=mock_dao,
        data={
            "database_name": "local_db",
            "sqlalchemy_uri": "sqlite+pysqlite:///test.db",
        },
    )
    with pytest.raises(CommandInvalidError, match="not allowed"):
        await cmd.validate()


async def test_create_database_missing_scheme_uri(mock_dao):
    """URI without a scheme must be rejected in validate()."""
    mock_dao.validate_uniqueness = AsyncMock(return_value=True)
    cmd = CreateDatabaseCommand(
        dao=mock_dao,
        data={"database_name": "bad_db", "sqlalchemy_uri": "no-scheme-here"},
    )
    with pytest.raises(CommandInvalidError, match="missing scheme"):
        await cmd.validate()


async def test_create_database_allows_postgresql_uri(mock_dao):
    """postgresql:// URI scheme must be allowed."""
    mock_dao.validate_uniqueness = AsyncMock(return_value=True)
    mock_dao.create = AsyncMock(return_value=MagicMock())
    cmd = CreateDatabaseCommand(
        dao=mock_dao,
        data={
            "database_name": "pg_db",
            "sqlalchemy_uri": "postgresql://user:pass@host/db",
        },
        user_id=1,
    )
    await cmd.validate()
    await cmd.run()  # Should not raise
    mock_dao.create.assert_awaited_once()


# ---------------------------------------------------------------------------
# NEW-T2: Report schedule guard for DeleteDatabaseCommand
# ---------------------------------------------------------------------------


async def test_delete_database_with_report_schedules_raises(mock_dao, mock_database):
    """DeleteDatabaseCommand blocks deletion when report schedules exist."""
    mock_dao.find_by_id = AsyncMock(return_value=mock_database)
    mock_dao.has_dependent_datasets = AsyncMock(return_value=False)
    mock_dao.find_report_schedules_by_database_id = AsyncMock(
        return_value=[MagicMock()]
    )
    cmd = DeleteDatabaseCommand(dao=mock_dao, database_id=1)
    with pytest.raises(CommandInvalidError, match="report schedules"):
        await cmd.validate()
