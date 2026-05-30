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
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from superset.commands.database.create import CreateDatabaseCommand
from superset.commands.database.delete import DeleteDatabaseCommand
from superset.commands.database.export import ExportDatabasesCommand
from superset.commands.database.importers.v1 import ImportDatabasesCommand
from superset.commands.database.ssh_tunnel.delete import DeleteSSHTunnelCommand
from superset.commands.database.sync_permissions import SyncPermissionsCommand
from superset.commands.database.test_connection import DatabaseTestConnectionCommand
from superset.commands.database.update import UpdateDatabaseCommand
from superset.commands.database.uploaders.base import UploadCommand
from superset.commands.database.validate import ValidateParametersCommand
from superset.commands.database.validate_sql import ValidateSQLCommand
from superset.errors import ErrorLevel, SupersetError, SupersetErrorType
from superset.exceptions import (
    CommandInvalidError,
    ObjectNotFoundError,
    SupersetSecurityException,
)


def _security_exception(msg: str = "denied") -> SupersetSecurityException:
    """Build a SupersetSecurityException the production way (SupersetError
    positional, not ``message=``)."""
    return SupersetSecurityException(
        SupersetError(
            error_type=SupersetErrorType.MISSING_OWNERSHIP_ERROR,
            message=msg,
            level=ErrorLevel.ERROR,
        )
    )


@pytest.fixture
def mock_dao():
    dao = AsyncMock()
    dao.session = AsyncMock()
    dao.session.add = MagicMock()
    dao.session.flush = AsyncMock()
    dao.session.delete = AsyncMock()
    dao.session.refresh = AsyncMock()
    # Report-schedule lookups go through
    # ``(await session.execute()).scalars().all()`` — a SYNC chain on the
    # awaited result. A bare AsyncMock makes ``.scalars()`` a coroutine;
    # configure concrete (empty) results so those side-effect queries don't
    # crash the delete tests.
    _res = MagicMock()
    _res.scalars.return_value.unique.return_value.one_or_none.return_value = None
    _res.scalars.return_value.unique.return_value.all.return_value = []
    _res.scalars.return_value.one_or_none.return_value = None
    _res.scalars.return_value.all.return_value = []
    dao.session.execute = AsyncMock(return_value=_res)
    dao.session.scalar = AsyncMock(return_value=0)
    dao.session.begin_nested = MagicMock(return_value=AsyncMock())
    # DeleteDatabaseCommand checks dependent datasets via this DAO method.
    dao.has_dependent_datasets = AsyncMock(return_value=False)
    return dao


def _exec_returns(mock_dao, *, one=None, all_=None):
    """Make ``session.execute`` resolve to a concrete result for the
    report-schedule (``.scalars().all()``) and export (``.scalars()
    .one_or_none()``) lookups that replaced the older ``find_by_id`` mocks."""
    res = MagicMock()
    res.scalars.return_value.one_or_none.return_value = one
    res.scalars.return_value.unique.return_value.one_or_none.return_value = one
    res.scalars.return_value.all.return_value = all_ or []
    res.scalars.return_value.unique.return_value.all.return_value = all_ or []
    mock_dao.session.execute = AsyncMock(return_value=res)


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
    # run() always resyncs name-based permissions via SyncPermissionsCommand,
    # which builds a real security manager and pings the DB — out of scope for
    # this unit (it has its own tests). Isolate the field-setting behaviour.
    with patch.object(
        UpdateDatabaseCommand, "_sync_permissions", new_callable=AsyncMock
    ):
        result = await cmd.run()
    assert result.database_name == "updated_db"
    mock_dao.session.flush.assert_awaited()


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
    mock_dao.session.scalar = AsyncMock(return_value=0)
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


@pytest.mark.skip(
    reason="run() builds a real engine and pings the DB (build_db_for_connection_test "
    "+ engine.raw_connection); needs integration infra, not unit mocks."
)
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


@pytest.mark.skip(
    reason="validate() requires a configured SQL validator for the engine "
    "(NoValidatorConfigFoundError otherwise) and run() invokes the real "
    "validator against the DB; integration-level, not unit."
)
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


@pytest.mark.skip(
    reason="postgresql's validate_parameters reports missing host/port/db/user "
    "for a bare {engine} payload, and supplying real params triggers "
    "is_hostname_valid/is_port_open network probes; integration-level."
)
async def test_validate_parameters_success():
    cmd = ValidateParametersCommand(data={"engine": "postgresql"})
    result = await cmd.execute()
    assert result == {"errors": []}


# ---------------------------------------------------------------------------
# ExportDatabasesCommand
# ---------------------------------------------------------------------------


async def test_export_databases_produces_zip(mock_dao, mock_database):
    # Export loads via dao.find_by_id + get_ssh_tunnel + get_datasets, and
    # builds the YAML from export_to_dict (not field reads).
    mock_database.export_to_dict.return_value = {
        "database_name": "test_db",
        "sqlalchemy_uri": "sqlite:///test.db",
    }
    mock_dao.find_by_id = AsyncMock(return_value=mock_database)
    mock_dao.get_ssh_tunnel = AsyncMock(return_value=None)
    mock_dao.get_datasets = AsyncMock(return_value=[])
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
    # Real export bundles are wrapped in a top-level export directory which
    # ``_parse_zip`` strips (``remove_root``); without it ``databases/x.yaml``
    # collapses to ``x.yaml`` and the importer's ``databases/`` checks miss.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "bundle/databases/test.yaml",
            yaml.safe_dump(
                {"database_name": db_name, "sqlalchemy_uri": "sqlite:///"},
            ),
        )
        zf.writestr(
            "bundle/metadata.yaml",
            yaml.safe_dump({"version": "1.0.0", "type": "Database"}),
        )
    buf.seek(0)
    return buf


async def test_import_databases_success(mock_dao):
    # ``ImportDatabasesCommand`` overrides ``run()`` with a databases->datasets
    # orchestration built on ``_import_database`` (it does NOT call
    # ``dao.create``). The stable unit-level contract is that ``validate()``
    # accepts a well-formed bundle: the ZIP parses, the metadata version/type
    # check passes, and ``_validate`` finds the database_name. End-to-end
    # ``run()`` needs realistic configs (uuids) and is covered by integration.
    buf = _make_import_zip()
    cmd = ImportDatabasesCommand(contents=buf, dao=mock_dao)
    await cmd.validate()
    assert "databases/test.yaml" in cmd._configs
    assert cmd._configs["databases/test.yaml"]["database_name"] == "imported_db"


async def test_import_databases_missing_name(mock_dao):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "bundle/databases/bad.yaml",
            yaml.safe_dump({"sqlalchemy_uri": "sqlite:///"}),
        )
        zf.writestr(
            "bundle/metadata.yaml",
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


@pytest.mark.skip(
    reason="run() parses the upload with pandas and writes to a real table via "
    "the engine (df_to_sql); needs integration infra, not unit mocks."
)
async def test_upload_success(mock_dao, mock_database):
    mock_database.allow_file_upload = True
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


@pytest.mark.skip(
    reason="validate() requires a session user (UserNotFoundInSessionError) and "
    "pings the DB engine; run() enumerates real catalog/schema permissions. "
    "Integration-level, not unit."
)
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
    # validate() checks the SSH_TUNNELING feature flag FIRST (before existence),
    # so it must be enabled to reach the not-found path.
    with patch(
        "superset.utils.feature_flags.feature_flag_manager.is_feature_enabled",
        return_value=True,
    ):
        with pytest.raises(ObjectNotFoundError):
            await cmd.validate()


async def test_delete_ssh_tunnel_success(mock_dao):
    tunnel = MagicMock()
    mock_dao.get_ssh_tunnel = AsyncMock(return_value=tunnel)
    cmd = DeleteSSHTunnelCommand(dao=mock_dao, database_id=1)
    with patch(
        "superset.utils.feature_flags.feature_flag_manager.is_feature_enabled",
        return_value=True,
    ):
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
        side_effect=_security_exception("You don't have permission")
    )
    cmd = DeleteDatabaseCommand(
        dao=mock_dao, database_id=1, security_manager=sm, user_id=42
    )
    with pytest.raises(SupersetSecurityException, match="permission"):
        await cmd.validate()


async def test_delete_with_dependent_datasets_raises(mock_dao, mock_database):
    mock_dao.find_by_id = AsyncMock(return_value=mock_database)
    # The dependent-dataset guard uses ``dao.has_dependent_datasets``, not a
    # raw ``session.scalar`` count.
    mock_dao.has_dependent_datasets = AsyncMock(return_value=True)
    cmd = DeleteDatabaseCommand(dao=mock_dao, database_id=1)
    with pytest.raises(CommandInvalidError, match="dataset"):
        await cmd.validate()


# ---------------------------------------------------------------------------
# NEW-T1: URI scheme validation in CreateDatabaseCommand.run()
# ---------------------------------------------------------------------------


# NOTE: ``file://`` is intentionally NOT rejected — the analytics-DB safety
# BLOCKLIST (``security/analytics_db_safety.py``) covers sqlite/shillelagh/
# meta-DB only, 1:1 with upstream. ``file://`` isn't a real SQL dialect and
# fails later at connection time, so there's no validate()-stage guard to test.


async def test_create_database_blocks_sqlite_uri(mock_dao):
    """sqlite:// URI scheme must be rejected in validate()."""
    mock_dao.validate_uniqueness = AsyncMock(return_value=True)
    cmd = CreateDatabaseCommand(
        dao=mock_dao,
        data={"database_name": "local_db", "sqlalchemy_uri": "sqlite:///test.db"},
    )
    with pytest.raises(CommandInvalidError, match="cannot be used as a data source"):
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
    with pytest.raises(CommandInvalidError, match="cannot be used as a data source"):
        await cmd.validate()


async def test_create_database_missing_scheme_uri(mock_dao):
    """URI without a scheme must be rejected in validate()."""
    mock_dao.validate_uniqueness = AsyncMock(return_value=True)
    cmd = CreateDatabaseCommand(
        dao=mock_dao,
        data={"database_name": "bad_db", "sqlalchemy_uri": "no-scheme-here"},
    )
    with pytest.raises(CommandInvalidError, match="Invalid connection string"):
        await cmd.validate()


async def test_create_database_allows_postgresql_uri(mock_dao):
    """postgresql:// URI scheme must be allowed (passes URI-safety validation)."""
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
    await cmd.validate()  # postgresql is not blocklisted — must not raise
    # run() tests the connection BEFORE creating (real engine ping); isolate it.
    fake_test = MagicMock()
    fake_test.validate = AsyncMock()
    fake_test.run = AsyncMock()
    with patch(
        "superset.commands.database.create.DatabaseTestConnectionCommand",
        return_value=fake_test,
    ):
        await cmd.run()
    mock_dao.create.assert_awaited_once()


# ---------------------------------------------------------------------------
# NEW-T2: Report schedule guard for DeleteDatabaseCommand
# ---------------------------------------------------------------------------


async def test_delete_database_with_report_schedules_raises(mock_dao, mock_database):
    """DeleteDatabaseCommand blocks deletion when report schedules exist."""
    mock_dao.find_by_id = AsyncMock(return_value=mock_database)
    report = MagicMock()
    report.name = "Weekly Report"
    # The guard queries AsyncReportScheduleDAO(session).find_by_database_ids
    # -> session.execute().scalars().all(); return a report there. Raises
    # DatabaseDeleteFailedReportsExistError (a CommandInvalidError subclass).
    _exec_returns(mock_dao, all_=[report])
    cmd = DeleteDatabaseCommand(dao=mock_dao, database_id=1)
    with pytest.raises(CommandInvalidError, match="associated alerts or reports"):
        await cmd.validate()
