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
import sys
import zipfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from superset.commands.dataset.columns.delete import DeleteDatasetColumnCommand
from superset.commands.dataset.create import (
    CreateDatasetCommand,
    GetOrCreateDatasetCommand,
)
from superset.commands.dataset.delete import (
    BulkDeleteDatasetsCommand,
    DeleteDatasetCommand,
)
from superset.commands.dataset.duplicate import DuplicateDatasetCommand
from superset.commands.dataset.exceptions import DatasetInvalidError
from superset.commands.dataset.export import ExportDatasetsCommand
from superset.commands.dataset.metrics.delete import DeleteDatasetMetricCommand
from superset.commands.dataset.refresh import RefreshDatasetCommand
from superset.commands.dataset.update import UpdateDatasetCommand
from superset.commands.dataset.warm_up_cache import WarmUpDatasetCacheCommand
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
    # Tag-sync side effects go through
    # ``(await session.execute()).scalars().{unique().one_or_none(),all()}`` —
    # SYNC chains on the awaited result. Configure concrete (empty) results so
    # ``.scalars()`` isn't a coroutine.
    _res = MagicMock()
    _res.scalars.return_value.unique.return_value.one_or_none.return_value = None
    _res.scalars.return_value.unique.return_value.all.return_value = []
    _res.scalars.return_value.one_or_none.return_value = None
    _res.scalars.return_value.all.return_value = []
    dao.session.execute = AsyncMock(return_value=_res)
    dao.session.begin_nested = MagicMock(return_value=AsyncMock())
    return dao


@pytest.fixture
def mock_column_dao():
    dao = AsyncMock()
    dao.session = AsyncMock()
    dao.session.delete = AsyncMock()
    dao.session.flush = AsyncMock()
    return dao


@pytest.fixture
def mock_metric_dao():
    dao = AsyncMock()
    dao.session = AsyncMock()
    dao.session.delete = AsyncMock()
    dao.session.flush = AsyncMock()
    return dao


@pytest.fixture
def mock_dataset():
    ds = MagicMock()
    ds.id = 1
    ds.table_name = "test_table"
    ds.database_id = 10
    ds.schema = "public"
    ds.sql = None
    ds.description = "A test dataset"
    ds.cache_timeout = None
    ds.uuid = None
    ds.database = MagicMock()
    ds.database.uuid = None
    ds.database.database_name = "test_db"
    ds.database.sqlalchemy_uri = "sqlite:///test.db"
    return ds


# ---- CreateDatasetCommand ----


def _physical_database() -> MagicMock:
    """A mock Database whose ``get_default_catalog`` returns a real string so
    that ``Table(...).__str__`` (used in error messages) never sees a
    MagicMock, and whose ``has_table`` reports the physical table exists."""
    db = MagicMock()
    db.get_default_catalog.return_value = None
    db.has_table.return_value = True
    return db


async def test_create_dataset_validates_table_name(mock_dao):
    mock_dao.get_database_by_id = AsyncMock(return_value=_physical_database())
    mock_dao.validate_uniqueness = AsyncMock(return_value=True)
    cmd = CreateDatasetCommand(dao=mock_dao, data={"database": 1})
    with pytest.raises(DatasetInvalidError) as exc_info:
        await cmd.validate()
    assert "table_name" in exc_info.value.normalized_messages()


async def test_create_dataset_validates_database(mock_dao):
    cmd = CreateDatasetCommand(dao=mock_dao, data={"table_name": "t"})
    with pytest.raises(DatasetInvalidError) as exc_info:
        await cmd.validate()
    assert "database" in exc_info.value.normalized_messages()


async def test_create_dataset_validates_database_not_found(mock_dao):
    mock_dao.get_database_by_id = AsyncMock(return_value=None)
    cmd = CreateDatasetCommand(dao=mock_dao, data={"table_name": "t", "database": 999})
    with pytest.raises(DatasetInvalidError) as exc_info:
        await cmd.validate()
    messages = exc_info.value.normalized_messages()
    assert messages["database"] == ["Database does not exist"]


async def test_create_dataset_validates_uniqueness(mock_dao):
    mock_dao.get_database_by_id = AsyncMock(return_value=_physical_database())
    mock_dao.validate_uniqueness = AsyncMock(return_value=False)
    cmd = CreateDatasetCommand(dao=mock_dao, data={"table_name": "t", "database": 1})
    with pytest.raises(DatasetInvalidError) as exc_info:
        await cmd.validate()
    messages = exc_info.value.normalized_messages()
    assert "table" in messages
    assert "already exists" in messages["table"][0]


async def test_create_dataset_accumulates_multiple_field_errors(mock_dao):
    """Blank table_name AND a non-existent database id are reported TOGETHER
    in the same 422 — proving accumulation (no early-return)."""
    mock_dao.get_database_by_id = AsyncMock(return_value=None)
    cmd = CreateDatasetCommand(dao=mock_dao, data={"table_name": "", "database": 999})
    with pytest.raises(DatasetInvalidError) as exc_info:
        await cmd.validate()
    messages = exc_info.value.normalized_messages()
    assert set(messages) == {"table_name", "database"}
    assert messages["database"] == ["Database does not exist"]


async def test_create_dataset_validation_success(mock_dao):
    mock_dao.get_database_by_id = AsyncMock(return_value=_physical_database())
    mock_dao.validate_uniqueness = AsyncMock(return_value=True)
    cmd = CreateDatasetCommand(
        dao=mock_dao,
        data={"table_name": "t", "database": 1},
        user_id=1,
    )
    await cmd.validate()  # Should not raise


# ---- UpdateDatasetCommand ----


async def test_update_dataset_not_found(mock_dao):
    mock_dao.find_by_id = AsyncMock(return_value=None)
    cmd = UpdateDatasetCommand(dao=mock_dao, dataset_id=999, data={"table_name": "x"})
    with pytest.raises(ObjectNotFoundError):
        await cmd.validate()


async def test_update_dataset_uniqueness_check(mock_dao, mock_dataset):
    mock_dao.find_by_id = AsyncMock(return_value=mock_dataset)
    mock_dao.validate_uniqueness = AsyncMock(return_value=False)
    cmd = UpdateDatasetCommand(
        dao=mock_dao,
        dataset_id=1,
        data={"table_name": "new_name"},
    )
    with pytest.raises(DatasetInvalidError) as exc_info:
        await cmd.validate()
    messages = exc_info.value.normalized_messages()
    assert "table" in messages
    assert "already exists" in messages["table"][0]


async def test_update_dataset_success(mock_dao, mock_dataset):
    mock_dao.find_by_id = AsyncMock(return_value=mock_dataset)
    mock_dao.validate_uniqueness = AsyncMock(return_value=True)
    mock_dao.update = AsyncMock(return_value=mock_dataset)
    cmd = UpdateDatasetCommand(
        dao=mock_dao,
        dataset_id=1,
        data={"description": "Updated desc"},
    )
    await cmd.validate()
    result = await cmd.run()
    assert result is mock_dataset
    # run() flushes for the update and again inside the owner-tag sync.
    mock_dao.session.flush.assert_awaited()


# ---- DeleteDatasetCommand ----


async def test_delete_dataset_not_found(mock_dao):
    mock_dao.find_by_id = AsyncMock(return_value=None)
    cmd = DeleteDatasetCommand(dao=mock_dao, dataset_id=999)
    with pytest.raises(ObjectNotFoundError):
        await cmd.validate()


async def test_delete_dataset_success(mock_dao, mock_dataset):
    mock_dao.find_by_id = AsyncMock(return_value=mock_dataset)
    cmd = DeleteDatasetCommand(dao=mock_dao, dataset_id=1)
    await cmd.validate()
    await cmd.run()
    mock_dao.session.delete.assert_awaited_once_with(mock_dataset)


# ---- BulkDeleteDatasetsCommand ----


async def test_bulk_delete_empty_ids(mock_dao):
    cmd = BulkDeleteDatasetsCommand(dao=mock_dao, dataset_ids=[])
    with pytest.raises(CommandInvalidError, match="No dataset IDs"):
        await cmd.validate()


async def test_bulk_delete_success(mock_dao, mock_dataset):
    mock_dao.find_by_ids = AsyncMock(return_value=[mock_dataset])
    cmd = BulkDeleteDatasetsCommand(dao=mock_dao, dataset_ids=[1])
    await cmd.validate()
    await cmd.run()
    mock_dao.session.delete.assert_awaited_once_with(mock_dataset)


# ---- DuplicateDatasetCommand ----


async def test_duplicate_validates_table_name(mock_dao):
    cmd = DuplicateDatasetCommand(dao=mock_dao, base_model_id=1, table_name="")
    with pytest.raises(DatasetInvalidError) as exc_info:
        await cmd.validate()
    assert "table_name" in exc_info.value.normalized_messages()


async def test_duplicate_source_not_found(mock_dao):
    mock_dao.find_by_id = AsyncMock(return_value=None)
    cmd = DuplicateDatasetCommand(dao=mock_dao, base_model_id=999, table_name="dup")
    with pytest.raises(ObjectNotFoundError):
        await cmd.validate()


async def test_duplicate_success(mock_dao, mock_dataset):
    mock_dataset.kind = "virtual"  # Only virtual datasets can be duplicated
    mock_dataset.sql = "SELECT 1"
    # Source collections are iterated in run() to copy columns/metrics.
    mock_dataset.columns = []
    mock_dataset.metrics = []
    mock_dao.find_by_id = AsyncMock(return_value=mock_dataset)
    # validate() rejects a duplicate name via find_one_or_none — must be free.
    mock_dao.find_one_or_none = AsyncMock(return_value=None)
    cmd = DuplicateDatasetCommand(dao=mock_dao, base_model_id=1, table_name="dup_table")
    await cmd.validate()
    # run() imports SqlaTable/SqlMetric/TableColumn from superset.models.connectors.
    mock_module = MagicMock()
    with patch.dict(sys.modules, {"superset.models.connectors": mock_module}):
        await cmd.run()
    mock_dao.session.add.assert_called_once()
    assert mock_dao.session.flush.await_count >= 1


# ---- RefreshDatasetCommand ----


async def test_refresh_not_found(mock_dao):
    mock_dao.find_by_id = AsyncMock(return_value=None)
    cmd = RefreshDatasetCommand(dao=mock_dao, dataset_id=999)
    with pytest.raises(ObjectNotFoundError):
        await cmd.validate()


async def test_refresh_success(mock_dao, mock_dataset):
    mock_dao.find_by_id = AsyncMock(return_value=mock_dataset)
    cmd = RefreshDatasetCommand(dao=mock_dao, dataset_id=1)
    result = await cmd.execute()
    assert result is mock_dataset


# ---- GetOrCreateDatasetCommand ----


async def test_get_or_create_validates_table_name(mock_dao):
    cmd = GetOrCreateDatasetCommand(dao=mock_dao, data={"database": 1})
    with pytest.raises(DatasetInvalidError) as exc_info:
        await cmd.validate()
    assert "table_name" in exc_info.value.normalized_messages()


async def test_get_or_create_validates_database(mock_dao):
    cmd = GetOrCreateDatasetCommand(dao=mock_dao, data={"table_name": "t"})
    with pytest.raises(DatasetInvalidError) as exc_info:
        await cmd.validate()
    assert "database_id" in exc_info.value.normalized_messages()


async def test_get_or_create_returns_existing(mock_dao, mock_dataset):
    mock_dao.find_one_or_none = AsyncMock(return_value=mock_dataset)
    # GetOrCreate uses the ``database_id`` key (not ``database``).
    cmd = GetOrCreateDatasetCommand(
        dao=mock_dao,
        data={"table_name": "test_table", "database_id": 10},
    )
    await cmd.validate()
    mock_module = MagicMock()
    with patch.dict(sys.modules, {"superset.models.connectors": mock_module}):
        result = await cmd.run()
    assert result is mock_dataset
    mock_dao.session.add.assert_not_called()


async def test_get_or_create_creates_new(mock_dao):
    mock_dao.find_one_or_none = AsyncMock(return_value=None)
    cmd = GetOrCreateDatasetCommand(
        dao=mock_dao,
        data={"table_name": "new_table", "database_id": 10},
        user_id=1,
    )
    await cmd.validate()
    # run() imports SqlaTable from superset.models.connectors.
    mock_module = MagicMock()
    with patch.dict(sys.modules, {"superset.models.connectors": mock_module}):
        await cmd.run()
    mock_dao.session.add.assert_called_once()
    mock_dao.session.flush.assert_awaited_once()


# ---- ExportDatasetsCommand ----


async def test_export_produces_zip(mock_dao, mock_dataset):
    # Export loads via find_by_id_with_options and builds YAML from
    # export_to_dict. Drop the database relationship so the secondary
    # database-YAML bundling (which would serialize a MagicMock) is skipped.
    mock_dataset.database = None
    mock_dataset.export_to_dict.return_value = {
        "table_name": "test_table",
        "schema": "public",
    }
    mock_dao.find_by_id_with_options = AsyncMock(return_value=mock_dataset)
    cmd = ExportDatasetsCommand(model_ids=[1], dao=mock_dao)
    buf = await cmd.execute()
    assert isinstance(buf, io.BytesIO)
    with zipfile.ZipFile(buf) as zf:
        names = zf.namelist()
        assert any("datasets/" in n for n in names)
        assert "metadata.yaml" in names
        # Verify YAML content contains known fields
        ds_files = [n for n in names if n.startswith("datasets/")]
        content = yaml.safe_load(zf.read(ds_files[0]))
        assert content["table_name"] == "test_table"
        assert content["schema"] == "public"


async def test_export_not_found(mock_dao):
    mock_dao.find_by_id_with_options = AsyncMock(return_value=None)
    cmd = ExportDatasetsCommand(model_ids=[999], dao=mock_dao)
    with pytest.raises(ObjectNotFoundError):
        await cmd.execute()


# ---- WarmUpDatasetCacheCommand ----


async def test_warm_up_unknown_db_name_raises(mock_dao):
    # validate() resolves (db_name, table_name) via a join query; there's no
    # empty-string guard — an unresolved table raises WarmUpCacheTableNotFoundError.
    from superset.commands.dataset.exceptions import WarmUpCacheTableNotFoundError

    cmd = WarmUpDatasetCacheCommand(dao=mock_dao, db_name="", table_name="t")
    with pytest.raises(WarmUpCacheTableNotFoundError):
        await cmd.validate()


async def test_warm_up_unknown_table_name_raises(mock_dao):
    from superset.commands.dataset.exceptions import WarmUpCacheTableNotFoundError

    cmd = WarmUpDatasetCacheCommand(dao=mock_dao, db_name="db", table_name="")
    with pytest.raises(WarmUpCacheTableNotFoundError):
        await cmd.validate()


@pytest.mark.skip(
    reason="run() resolves a real table + its charts via join queries, then warms "
    "each chart's cache through WarmUpChartCacheCommand (viz/query-context "
    "execution); integration-level, not unit. run() also returns chart "
    "warm-up dicts, not {db_name,table_name,status}."
)
async def test_warm_up_success(mock_dao):
    cmd = WarmUpDatasetCacheCommand(dao=mock_dao, db_name="mydb", table_name="mytable")
    result = await cmd.execute()
    assert result[0]["db_name"] == "mydb"
    assert result[0]["table_name"] == "mytable"
    assert result[0]["status"] == "success"


# ---- DeleteDatasetColumnCommand ----


async def test_delete_column_dataset_not_found(mock_dao, mock_column_dao):
    mock_dao.find_by_id = AsyncMock(return_value=None)
    cmd = DeleteDatasetColumnCommand(
        dataset_dao=mock_dao,
        column_dao=mock_column_dao,
        dataset_id=999,
        column_id=1,
    )
    with pytest.raises(ObjectNotFoundError, match="Dataset"):
        await cmd.validate()


async def test_delete_column_not_found(mock_dao, mock_column_dao, mock_dataset):
    mock_dao.find_by_id = AsyncMock(return_value=mock_dataset)
    mock_column_dao.find_by_dataset_and_id = AsyncMock(return_value=None)
    cmd = DeleteDatasetColumnCommand(
        dataset_dao=mock_dao,
        column_dao=mock_column_dao,
        dataset_id=1,
        column_id=999,
    )
    with pytest.raises(ObjectNotFoundError, match="DatasetColumn"):
        await cmd.validate()


async def test_delete_column_success(mock_dao, mock_column_dao, mock_dataset):
    mock_column = MagicMock()
    mock_dao.find_by_id = AsyncMock(return_value=mock_dataset)
    mock_column_dao.find_by_dataset_and_id = AsyncMock(return_value=mock_column)
    cmd = DeleteDatasetColumnCommand(
        dataset_dao=mock_dao,
        column_dao=mock_column_dao,
        dataset_id=1,
        column_id=5,
    )
    await cmd.validate()
    await cmd.run()
    mock_column_dao.session.delete.assert_awaited_once_with(mock_column)


# ---- DeleteDatasetMetricCommand ----


async def test_delete_metric_dataset_not_found(mock_dao, mock_metric_dao):
    mock_dao.find_by_id = AsyncMock(return_value=None)
    cmd = DeleteDatasetMetricCommand(
        dataset_dao=mock_dao,
        metric_dao=mock_metric_dao,
        dataset_id=999,
        metric_id=1,
    )
    with pytest.raises(ObjectNotFoundError, match="Dataset"):
        await cmd.validate()


async def test_delete_metric_not_found(mock_dao, mock_metric_dao, mock_dataset):
    mock_dao.find_by_id = AsyncMock(return_value=mock_dataset)
    mock_metric_dao.find_by_dataset_and_id = AsyncMock(return_value=None)
    cmd = DeleteDatasetMetricCommand(
        dataset_dao=mock_dao,
        metric_dao=mock_metric_dao,
        dataset_id=1,
        metric_id=999,
    )
    with pytest.raises(ObjectNotFoundError, match="DatasetMetric"):
        await cmd.validate()


async def test_delete_metric_success(mock_dao, mock_metric_dao, mock_dataset):
    mock_metric = MagicMock()
    mock_dao.find_by_id = AsyncMock(return_value=mock_dataset)
    mock_metric_dao.find_by_dataset_and_id = AsyncMock(return_value=mock_metric)
    cmd = DeleteDatasetMetricCommand(
        dataset_dao=mock_dao,
        metric_dao=mock_metric_dao,
        dataset_id=1,
        metric_id=7,
    )
    await cmd.validate()
    await cmd.run()
    mock_metric_dao.session.delete.assert_awaited_once_with(mock_metric)


# ---------------------------------------------------------------------------
# Ownership checks
# ---------------------------------------------------------------------------


async def test_delete_non_owner_raises_forbidden(mock_dao, mock_dataset):
    mock_dao.find_by_id = AsyncMock(return_value=mock_dataset)
    sm = AsyncMock()
    sm.raise_for_ownership = AsyncMock(
        side_effect=_security_exception("You don't have permission")
    )
    cmd = DeleteDatasetCommand(
        dao=mock_dao, dataset_id=1, security_manager=sm, user_id=42
    )
    with pytest.raises(SupersetSecurityException, match="permission"):
        await cmd.validate()


async def test_update_non_owner_raises_forbidden(mock_dao, mock_dataset):
    mock_dao.find_by_id = AsyncMock(return_value=mock_dataset)
    sm = AsyncMock()
    sm.raise_for_ownership = AsyncMock(
        side_effect=_security_exception("You don't have permission")
    )
    cmd = UpdateDatasetCommand(
        dao=mock_dao, dataset_id=1, data={}, user_id=42, security_manager=sm
    )
    with pytest.raises(SupersetSecurityException, match="permission"):
        await cmd.validate()


async def test_refresh_non_owner_raises_forbidden(mock_dao, mock_dataset):
    mock_dao.find_by_id = AsyncMock(return_value=mock_dataset)
    sm = AsyncMock()
    sm.raise_for_ownership = AsyncMock(
        side_effect=_security_exception("You don't have permission")
    )
    cmd = RefreshDatasetCommand(
        dao=mock_dao, dataset_id=1, security_manager=sm, user_id=42
    )
    with pytest.raises(SupersetSecurityException, match="permission"):
        await cmd.validate()


# ---------------------------------------------------------------------------
# NEW-T5: Physical dataset rejection in DuplicateDatasetCommand
# ---------------------------------------------------------------------------


async def test_duplicate_physical_dataset_rejected(mock_dao, mock_dataset):
    """Duplicating a physical dataset (kind != 'virtual') raises error."""
    mock_dataset.kind = "physical"
    mock_dataset.sql = None
    mock_dao.find_by_id = AsyncMock(return_value=mock_dataset)
    mock_dao.find_one_or_none = AsyncMock(return_value=None)
    cmd = DuplicateDatasetCommand(dao=mock_dao, base_model_id=1, table_name="dup")
    with pytest.raises(DatasetInvalidError) as exc_info:
        await cmd.validate()
    messages = exc_info.value.normalized_messages()
    assert messages["datasource_type"] == ["Datasource type is invalid"]


async def test_duplicate_dataset_no_kind_attribute_rejected(mock_dao):
    """Dataset without sql attribute (None) is rejected as non-virtual."""
    source = MagicMock()
    source.id = 1
    source.kind = None
    source.sql = None
    mock_dao.find_by_id = AsyncMock(return_value=source)
    mock_dao.find_one_or_none = AsyncMock(return_value=None)
    cmd = DuplicateDatasetCommand(dao=mock_dao, base_model_id=1, table_name="dup")
    with pytest.raises(DatasetInvalidError) as exc_info:
        await cmd.validate()
    assert "datasource_type" in exc_info.value.normalized_messages()


# ---------------------------------------------------------------------------
# NEW-T6: BulkDelete — "some IDs not found" branch
# ---------------------------------------------------------------------------


async def test_bulk_delete_some_ids_not_found(mock_dao, mock_dataset):
    """BulkDeleteDatasetsCommand raises ObjectNotFoundError when some IDs are
    missing.
    """
    mock_dao.find_by_ids = AsyncMock(return_value=[mock_dataset])  # only id=1 found
    cmd = BulkDeleteDatasetsCommand(dao=mock_dao, dataset_ids=[1, 2, 3])
    with pytest.raises(ObjectNotFoundError):
        await cmd.validate()


# ---------------------------------------------------------------------------
# Per-field DatasetInvalidError / normalized_messages (upstream-1:1 422 shape)
# ---------------------------------------------------------------------------


def test_dataset_invalid_error_normalized_messages_merges_per_field():
    """``normalized_messages`` merges each child into ``{field: [messages]}``
    1:1 with upstream FAB ``response_422(message=ex.normalized_messages())``."""
    from superset.commands.dataset.exceptions import (
        DatabaseNotFoundValidationError,
        DatasetInvalidError,
        DatasetValidationError,
    )

    err = DatasetInvalidError(
        exceptions=[
            DatasetValidationError("table_name is required", field_name="table_name"),
            DatabaseNotFoundValidationError(),
        ]
    )
    assert err.normalized_messages() == {
        "table_name": ["table_name is required"],
        "database": ["Database does not exist"],
    }
    # append() extends the accumulator just like upstream CommandInvalidError.
    err.append(DatasetValidationError("Invalid SQL: boom", field_name="sql"))
    assert err.normalized_messages()["sql"] == ["Invalid SQL: boom"]
    assert err.status_code == 422


def test_dataset_validation_error_field_keys_match_upstream():
    """Field keys + message text are 1:1 with
    ``superset_old/commands/dataset/exceptions.py``."""
    from superset.commands.dataset.exceptions import (
        DatabaseNotFoundValidationError,
        DatasetColumnNotFoundValidationError,
        DatasetColumnsDuplicateValidationError,
        DatasetColumnsExistsValidationError,
        DatasetMetricsDuplicateValidationError,
        DatasetMetricsExistsValidationError,
        DatasetMetricsNotFoundValidationError,
        DatasourceTypeInvalidError,
        MultiCatalogDisabledValidationError,
        OwnersNotFoundValidationError,
    )

    assert DatabaseNotFoundValidationError().normalized_messages() == {
        "database": ["Database does not exist"]
    }
    assert DatasetColumnsDuplicateValidationError().normalized_messages() == {
        "columns": ["One or more columns are duplicated"]
    }
    assert DatasetColumnNotFoundValidationError().normalized_messages() == {
        "columns": ["One or more columns do not exist"]
    }
    assert DatasetColumnsExistsValidationError().normalized_messages() == {
        "columns": ["One or more columns already exist"]
    }
    assert DatasetMetricsDuplicateValidationError().normalized_messages() == {
        "metrics": ["One or more metrics are duplicated"]
    }
    assert DatasetMetricsNotFoundValidationError().normalized_messages() == {
        "metrics": ["One or more metrics do not exist"]
    }
    assert DatasetMetricsExistsValidationError().normalized_messages() == {
        "metrics": ["One or more metrics already exist"]
    }
    assert MultiCatalogDisabledValidationError().normalized_messages() == {
        "catalog": ["Only the default catalog is supported for this connection"]
    }
    assert OwnersNotFoundValidationError().normalized_messages() == {
        "owners": ["Owners are invalid"]
    }
    assert DatasourceTypeInvalidError().normalized_messages() == {
        "datasource_type": ["Datasource type is invalid"]
    }


def test_dataset_invalid_error_handler_emits_message_dict():
    """The Litestar handler emits ``{"message": {field: [messages]}}`` (no
    ``errors``/``detail`` keys) — 1:1 with upstream FAB ``response_422``."""
    from superset.commands.dataset.exceptions import (
        DatabaseNotFoundValidationError,
        dataset_invalid_error_handler,
        DatasetInvalidError,
        DatasetValidationError,
    )

    err = DatasetInvalidError(
        exceptions=[
            DatasetValidationError("table_name is required", field_name="table_name"),
            DatabaseNotFoundValidationError(),
        ]
    )
    response = dataset_invalid_error_handler(MagicMock(), err)
    assert response.status_code == 422
    assert response.content == {
        "message": {
            "table_name": ["table_name is required"],
            "database": ["Database does not exist"],
        }
    }
