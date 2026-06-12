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
# _validate_extra — the ``extra`` JSON field must be an object
#
# Regression: a valid-but-non-object ``extra`` (``[1,2]`` / ``5`` / ``"s"``)
# made ``extra_.get("metadata_params", …)`` raise AttributeError → HTTP 500
# (live-probed via PUT /database/1). Must be a clean 4xx CommandInvalidError.
# ---------------------------------------------------------------------------


def test_validate_extra_rejects_non_object() -> None:
    from superset.commands.database.utils import _validate_extra

    for bad in ("[1, 2, 3]", "5", '"a string"', "true"):
        with pytest.raises(CommandInvalidError, match="must be a JSON object"):
            _validate_extra(bad)


def test_validate_extra_accepts_object_and_empty() -> None:
    from superset.commands.database.utils import _validate_extra

    # None/empty/null and a plain object all pass without raising.
    _validate_extra(None)
    _validate_extra("")
    _validate_extra("null")
    _validate_extra("{}")
    _validate_extra('{"metadata_params": {}}')


def test_validate_extra_bad_metadata_params_key() -> None:
    from superset.commands.database.utils import _validate_extra

    with pytest.raises(CommandInvalidError, match="metadata_params"):
        _validate_extra('{"metadata_params": {"not_a_real_kwarg": 1}}')


# ---------------------------------------------------------------------------
# CreateDatabaseCommand
# ---------------------------------------------------------------------------


async def test_create_database_validates_name_required(mock_dao):
    cmd = CreateDatabaseCommand(dao=mock_dao, data={})
    with pytest.raises(CommandInvalidError, match="database_name"):
        await cmd.validate()


async def test_create_database_validates_uniqueness(mock_dao):
    """Name conflict is the field-keyed 422 upstream emits:
    ``DatabaseInvalidError(exceptions=[DatabaseExistsValidationError()])`` →
    ``{"database_name": ["A database with the same name already exists."]}``
    (superset_old/commands/database/exceptions.py:35-43)."""
    from superset.commands.database.exceptions import DatabaseInvalidError

    mock_dao.validate_uniqueness = AsyncMock(return_value=False)
    cmd = CreateDatabaseCommand(
        dao=mock_dao,
        data={
            "database_name": "existing_db",
            "sqlalchemy_uri": "postgresql://localhost/db",
        },
    )
    with pytest.raises(DatabaseInvalidError) as exc_info:
        await cmd.validate()
    assert exc_info.value.normalized_messages() == {
        "database_name": ["A database with the same name already exists."]
    }


async def test_create_database_validates_success(mock_dao):
    mock_dao.validate_uniqueness = AsyncMock(return_value=True)
    cmd = CreateDatabaseCommand(
        dao=mock_dao,
        data={"database_name": "new_db", "sqlalchemy_uri": "postgresql://localhost/db"},
        user_id=1,
    )
    await cmd.validate()  # Should not raise


async def test_create_database_uniqueness_failure_emits_telemetry(mock_dao):
    """A failed name-uniqueness validation emits the upstream analytics event
    ``db_connection_failed.<ExcCls>.<leaf-classnames>``
    (superset_old/commands/database/create.py:143-152)."""
    from superset.commands.database.exceptions import DatabaseInvalidError

    mock_dao.validate_uniqueness = AsyncMock(return_value=False)
    cmd = CreateDatabaseCommand(
        dao=mock_dao,
        data={
            "database_name": "existing_db",
            "sqlalchemy_uri": "postgresql://localhost/db",
        },
    )
    with patch("superset.events.event_logger.log_with_context") as ev:
        with pytest.raises(DatabaseInvalidError):
            await cmd.validate()
    ev.assert_called_once_with(
        action="db_connection_failed.DatabaseInvalidError.DatabaseExistsValidationError"
    )


async def test_create_database_connection_failure_emits_telemetry(mock_dao):
    """A failed pre-creation connection test emits
    ``db_creation_failed.<ExcCls>`` with the URI scheme as ``engine``
    (superset_old/commands/database/create.py:81-86) before being wrapped in
    ``DatabaseConnectionFailedError``."""
    from superset.exceptions import DatabaseConnectionFailedError

    mock_dao.validate_uniqueness = AsyncMock(return_value=True)
    cmd = CreateDatabaseCommand(
        dao=mock_dao,
        data={
            "database_name": "new_db",
            "sqlalchemy_uri": "postgresql://localhost/db",
        },
    )
    await cmd.validate()
    fake_test_cmd = MagicMock()
    fake_test_cmd.validate = AsyncMock()
    fake_test_cmd.run = AsyncMock(side_effect=RuntimeError("connection refused"))
    with (
        patch(
            "superset.commands.database.create.DatabaseTestConnectionCommand",
            return_value=fake_test_cmd,
        ),
        patch("superset.events.event_logger.log_with_context") as ev,
    ):
        with pytest.raises(DatabaseConnectionFailedError):
            await cmd.run()
    ev.assert_called_once_with(
        action="db_creation_failed.RuntimeError", engine="postgresql"
    )


async def test_create_database_reraised_failure_emits_telemetry(mock_dao):
    """SIP-40 / SSH errors are re-raised unchanged but still emit the
    ``db_creation_failed.<ExcCls>`` event (upstream create.py:70-80)."""
    from superset.exceptions import SupersetErrorsException

    mock_dao.validate_uniqueness = AsyncMock(return_value=True)
    cmd = CreateDatabaseCommand(
        dao=mock_dao,
        data={
            "database_name": "new_db",
            "sqlalchemy_uri": "postgresql://localhost/db",
        },
    )
    await cmd.validate()
    fake_test_cmd = MagicMock()
    fake_test_cmd.validate = AsyncMock()
    fake_test_cmd.run = AsyncMock(
        side_effect=SupersetErrorsException(
            [
                SupersetError(
                    error_type=SupersetErrorType.CONNECTION_INVALID_HOSTNAME_ERROR,
                    message="bad host",
                    level=ErrorLevel.ERROR,
                )
            ]
        )
    )
    with (
        patch(
            "superset.commands.database.create.DatabaseTestConnectionCommand",
            return_value=fake_test_cmd,
        ),
        patch("superset.events.event_logger.log_with_context") as ev,
    ):
        with pytest.raises(SupersetErrorsException):
            await cmd.run()
    ev.assert_called_once_with(
        action="db_creation_failed.SupersetErrorsException", engine="postgresql"
    )


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
    """PUT name conflict — same field-keyed shape and exact upstream text
    (the port used to emit a different flat string)."""
    from superset.commands.database.exceptions import DatabaseInvalidError

    mock_dao.find_by_id = AsyncMock(return_value=mock_database)
    mock_dao.validate_update_uniqueness = AsyncMock(return_value=False)
    cmd = UpdateDatabaseCommand(
        dao=mock_dao,
        database_id=1,
        data={"database_name": "duplicate"},
    )
    with pytest.raises(DatabaseInvalidError) as exc_info:
        await cmd.validate()
    assert exc_info.value.normalized_messages() == {
        "database_name": ["A database with the same name already exists."]
    }


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


async def test_update_database_broken_connection_forces_catalog_update(
    mock_dao, mock_database
):
    """When the CURRENT connection is broken, ``get_default_catalog`` on the
    pre-update model raises — upstream catches it, sets ``force_update=True``
    and still propagates the (fixed) catalog to dependent assets
    (superset_old/commands/database/update.py:83-110).  Without the catch the
    PUT 500s and the user can never repair a broken connection."""
    mock_dao.find_by_id = AsyncMock(return_value=mock_database)
    mock_dao.validate_update_uniqueness = AsyncMock(return_value=True)
    mock_database.get_default_catalog = MagicMock(
        side_effect=[Exception("connection broken"), "fixed_catalog"]
    )
    cmd = UpdateDatabaseCommand(
        dao=mock_dao,
        database_id=1,
        data={"sqlalchemy_uri": "postgresql://fixed-host/db"},
    )
    await cmd.validate()
    with (
        patch.object(
            UpdateDatabaseCommand, "_sync_permissions", new_callable=AsyncMock
        ),
        patch.object(
            UpdateDatabaseCommand, "_update_catalog_attribute", new_callable=AsyncMock
        ) as upd_cat,
    ):
        result = await cmd.run()
    assert result is mock_database
    # force_update short-circuits the multi-catalog guard — 1:1 upstream.
    upd_cat.assert_awaited_once_with(1, "fixed_catalog")


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
    # validate() calls dao.count to verify the user can access the requested IDs.
    mock_dao.count = AsyncMock(return_value=1)
    cmd = ExportDatabasesCommand(model_ids=[1], dao=mock_dao)
    with patch(
        "superset.db.filters.database_access_filters",
        AsyncMock(return_value=[]),
    ):
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
    # count=0 makes validate() raise ObjectNotFoundError (inaccessible ID).
    mock_dao.count = AsyncMock(return_value=0)
    cmd = ExportDatabasesCommand(
        model_ids=[999], dao=mock_dao, security_manager=MagicMock()
    )
    with patch(
        "superset.db.filters.database_access_filters",
        AsyncMock(return_value=[]),
    ):
        with pytest.raises(ObjectNotFoundError):
            await cmd.execute()


async def test_export_databases_denies_inaccessible_id(mock_dao):
    """validate() raises ObjectNotFoundError when dao.count returns fewer rows
    than requested — i.e. the user is not permitted to see the database."""
    mock_dao.count = AsyncMock(return_value=0)
    cmd = ExportDatabasesCommand(
        model_ids=[42], dao=mock_dao, security_manager=MagicMock()
    )
    with patch(
        "superset.db.filters.database_access_filters",
        AsyncMock(return_value=[]),
    ):
        with pytest.raises(ObjectNotFoundError):
            await cmd.validate()


async def test_export_databases_no_dao():
    cmd = ExportDatabasesCommand(model_ids=[1], dao=None)
    with pytest.raises(CommandInvalidError, match="DAO not provided"):
        await cmd.execute()


async def test_export_databases_dataset_json_fields_are_raw_strings(
    mock_dao, mock_database
):
    """1:1 parity with original superset_old/commands/database/export.py:122-129.

    The original _export() sets only ``version`` and ``database_uuid`` on the
    dataset payload — it does NOT decode JSON string fields (params,
    template_params, extra, or per-metric/column extra).  Only
    ExportDatasetsCommand._file_content() does that decoding.

    Regression guard: the liteset _export_single previously called json.loads()
    on those fields, producing decoded dicts in the YAML where the original
    produces raw JSON-encoded strings.  This test asserts that after the fix
    the values remain as-is (raw strings).
    """
    raw_params = '{"time_grain_sqla": "P1D"}'
    raw_template_params = '{"foo": "bar"}'
    raw_extra = '{"certification": {"certified_by": "core team"}}'
    raw_metric_extra = '{"warning_markdown": ""}'

    mock_database.export_to_dict.return_value = {
        "database_name": "test_db",
        "sqlalchemy_uri": "sqlite:///test.db",
    }
    mock_database.uuid = "11111111-1111-1111-1111-111111111111"

    mock_dataset = MagicMock()
    mock_dataset.id = 10
    mock_dataset.table_name = "sales"
    mock_dataset.export_to_dict.return_value = {
        "table_name": "sales",
        "params": raw_params,
        "template_params": raw_template_params,
        "extra": raw_extra,
        "metrics": [
            {"metric_name": "count", "extra": raw_metric_extra},
        ],
        "columns": [
            {"column_name": "id", "extra": raw_metric_extra},
        ],
    }

    mock_dao.find_by_id = AsyncMock(return_value=mock_database)
    mock_dao.get_ssh_tunnel = AsyncMock(return_value=None)
    mock_dao.get_datasets = AsyncMock(return_value=[mock_dataset])
    mock_dao.count = AsyncMock(return_value=1)

    cmd = ExportDatabasesCommand(model_ids=[1], dao=mock_dao)
    with patch(
        "superset.db.filters.database_access_filters",
        AsyncMock(return_value=[]),
    ):
        buf = await cmd.execute()

    with zipfile.ZipFile(buf) as zf:
        ds_files = [n for n in zf.namelist() if n.startswith("datasets/")]
        assert ds_files, "expected at least one dataset YAML in the bundle"
        ds_content = yaml.safe_load(zf.read(ds_files[0]))

    # Original behaviour: JSON-string fields are written verbatim, not decoded.
    assert ds_content["params"] == raw_params, (
        "params must remain a raw JSON string (no json.loads decoding)"
    )
    assert ds_content["template_params"] == raw_template_params, (
        "template_params must remain a raw JSON string"
    )
    assert ds_content["extra"] == raw_extra, "extra must remain a raw JSON string"
    # Nested metric/column extra must also stay as raw strings.
    assert ds_content["metrics"][0]["extra"] == raw_metric_extra, (
        "metric.extra must remain a raw JSON string"
    )
    assert ds_content["columns"][0]["extra"] == raw_metric_extra, (
        "column.extra must remain a raw JSON string"
    )
    # Confirm the values are NOT dicts/lists (i.e., no json.loads was applied).
    assert isinstance(ds_content["params"], str)
    assert isinstance(ds_content["extra"], str)
    assert isinstance(ds_content["metrics"][0]["extra"], str)

    # Mandatory version + database_uuid stamps are still present.
    assert "version" in ds_content
    assert ds_content["database_uuid"] == str(mock_database.uuid)


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


async def test_import_databases_validate_skips_non_dict_config(mock_dao):
    """Regression: a bundled YAML file parsing to a list/scalar must be skipped
    in _validate (was ``config.get`` on a list → AttributeError → HTTP 500)."""
    cmd = ImportDatabasesCommand(contents=io.BytesIO(b""), dao=mock_dao)
    # A non-dict ``databases/`` config must not raise.
    await cmd._validate({"databases/x.yaml": [1, 2, 3], "metadata.yaml": {}})
    await cmd._validate({"databases/x.yaml": "a string"})
    # A real dict still validated: missing database_name → CommandInvalidError.
    with pytest.raises(CommandInvalidError, match="Missing database_name"):
        await cmd._validate({"databases/y.yaml": {"sqlalchemy_uri": "sqlite://"}})


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
# add_permissions — catalog/schema DAR PVMs on database CREATE
#
# Regression: a newly-created database had no schema/catalog access
# permission-view-menus, so per-schema RBAC grants could not be made until a
# later re-sync.  ``add_permissions`` (called from the create flow) must
# enumerate the connection's catalogs/schemas and create the
# ``catalog_access`` / ``schema_access`` PVMs via the security manager.
# Mirrors superset_old/commands/database/utils.py::add_permissions.
# ---------------------------------------------------------------------------


def _perm_security_manager():
    """SM mock whose perm-string helpers + async PVM creator are observable."""
    sm = MagicMock()
    sm.get_catalog_perm = MagicMock(
        side_effect=lambda name, catalog: f"[{name}].[{catalog}]"
    )
    sm.get_schema_perm = MagicMock(
        side_effect=lambda name, schema, catalog=None: (
            f"[{name}].[{catalog}].[{schema}]" if catalog else f"[{name}].[{schema}]"
        )
    )
    sm.add_permission_view_menu = AsyncMock(return_value=MagicMock())
    return sm


def _non_catalog_database():
    db = MagicMock()
    db.database_name = "test_db"
    db.allow_multi_catalog = False
    db.db_engine_spec.supports_catalog = False
    db.db_engine_spec.get_schema_names.return_value = {"public", "secret"}
    # get_inspector() is a context manager
    db.get_inspector.return_value.__enter__.return_value = MagicMock()
    db.get_inspector.return_value.__exit__.return_value = False
    return db


async def test_add_permissions_creates_schema_pvms_for_non_catalog_db():
    """Non-catalog engine (e.g. Postgres-like): one schema_access PVM per schema,
    no catalog_access PVMs."""
    from superset.commands.database.utils import add_permissions

    db = _non_catalog_database()
    sm = _perm_security_manager()

    await add_permissions(db, sm)

    calls = sm.add_permission_view_menu.await_args_list
    perm_types = {c.args[0] for c in calls}
    assert perm_types == {"schema_access"}
    created = {c.args[1] for c in calls}
    assert created == {"[test_db].[public]", "[test_db].[secret]"}


async def test_add_permissions_creates_catalog_and_schema_pvms():
    """Catalog-aware engine with multi-catalog: a catalog_access PVM per catalog
    plus schema_access PVMs scoped to that catalog."""
    from superset.commands.database.utils import add_permissions

    db = MagicMock()
    db.database_name = "test_db"
    db.allow_multi_catalog = True
    db.db_engine_spec.supports_catalog = True
    db.db_engine_spec.supports_cross_catalog_queries = True
    db.db_engine_spec.get_catalog_names.return_value = {"cat1"}
    db.db_engine_spec.get_schema_names.return_value = {"public"}
    db.get_inspector.return_value.__enter__.return_value = MagicMock()
    db.get_inspector.return_value.__exit__.return_value = False
    sm = _perm_security_manager()

    await add_permissions(db, sm)

    calls = sm.add_permission_view_menu.await_args_list
    by_type = {}
    for c in calls:
        by_type.setdefault(c.args[0], set()).add(c.args[1])
    assert by_type["catalog_access"] == {"[test_db].[cat1]"}
    assert by_type["schema_access"] == {"[test_db].[cat1].[public]"}


async def test_add_permissions_swallows_introspection_errors():
    """A failure enumerating one catalog's schemas must NOT abort: it logs and
    continues (mirrors the original's per-catalog GenericDBException swallow)."""
    from superset.commands.database.utils import add_permissions

    db = _non_catalog_database()
    db.get_inspector.side_effect = RuntimeError("connection refused")
    sm = _perm_security_manager()

    # Must not raise.
    await add_permissions(db, sm)
    # No schema PVMs could be created, but the call did not blow up.
    sm.add_permission_view_menu.assert_not_awaited()


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


async def test_create_database_dynamic_form_passes_masked_encrypted_extra(mock_dao):
    """Credentials must reach ``build_sqlalchemy_uri`` via
    ``masked_encrypted_extra``.

    1:1 with the original pre_load flow: validation reads
    ``data.get("masked_encrypted_extra")`` (superset_old/databases/
    schemas.py:352) — the rename to ``encrypted_extra`` only happens at
    persistence time inside ``_create_database`` (superset_old/commands/
    database/create.py:157-160). An early controller-side rename starved
    BigQuery-style specs of credentials ("Missing service credentials").
    """
    import json as _json

    mock_dao.validate_uniqueness = AsyncMock(return_value=True)
    creds = {"credentials_info": {"project_id": "p1"}}

    captured: dict = {}

    class _Spec:
        parameters_schema = object()

        @staticmethod
        def build_sqlalchemy_uri(parameters, encrypted_extra):
            captured["parameters"] = parameters
            captured["encrypted_extra"] = encrypted_extra
            return "bigquery://p1"

    cmd = CreateDatabaseCommand(
        dao=mock_dao,
        data={
            "database_name": "bq",
            "configuration_method": "dynamic_form",
            "engine": "bigquery",
            "parameters": {"project_id": "p1"},
            "masked_encrypted_extra": _json.dumps(creds),
        },
    )
    with patch("superset.db_engine_specs.get_engine_spec", return_value=_Spec):
        await cmd.validate()

    assert captured["encrypted_extra"] == creds
    assert cmd._data["sqlalchemy_uri"] == "bigquery://p1"


async def test_test_connection_builds_uri_from_dynamic_form_parameters(mock_dao):
    """Regression: dynamic_form requests carry ``parameters``, not a URI.

    Upstream built the URI in the Marshmallow ``@pre_load``
    (DatabaseParametersSchemaMixin.build_sqlalchemy_uri); without the
    equivalent step every dynamic-form "Test connection" died with
    "sqlalchemy_uri is required for connection test".
    """
    cmd = DatabaseTestConnectionCommand(
        dao=mock_dao,
        data={
            "configuration_method": "dynamic_form",
            "engine": "postgresql",
            "parameters": {
                "username": "scott",
                "password": "tiger",
                "host": "dbhost",
                "port": 5432,
                "database": "mydb",
            },
        },
    )
    await cmd.validate()
    built = cmd._properties["sqlalchemy_uri"]
    assert built.startswith("postgresql")
    assert "dbhost" in built
    assert "mydb" in built


async def test_test_connection_dynamic_form_requires_engine(mock_dao):
    cmd = DatabaseTestConnectionCommand(
        dao=mock_dao,
        data={
            "configuration_method": "dynamic_form",
            "parameters": {"host": "h", "database": "d"},
        },
    )
    with pytest.raises(CommandInvalidError, match="engine must be specified"):
        await cmd.validate()
