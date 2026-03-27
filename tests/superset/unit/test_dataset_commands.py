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

from superset.commands.dataset import (
    BulkDeleteDatasetsCommand,
    CreateDatasetCommand,
    DeleteDatasetColumnCommand,
    DeleteDatasetCommand,
    DeleteDatasetMetricCommand,
    DuplicateDatasetCommand,
    ExportDatasetsCommand,
    GetOrCreateDatasetCommand,
    RefreshDatasetCommand,
    UpdateDatasetCommand,
    WarmUpDatasetCacheCommand,
)
from superset.exceptions import (
    CommandInvalidError,
    SupersetSecurityException,
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


async def test_create_dataset_validates_table_name(mock_dao):
    cmd = CreateDatasetCommand(dao=mock_dao, data={"database": 1})
    with pytest.raises(CommandInvalidError, match="table_name"):
        await cmd.validate()


async def test_create_dataset_validates_database(mock_dao):
    cmd = CreateDatasetCommand(dao=mock_dao, data={"table_name": "t"})
    with pytest.raises(CommandInvalidError, match="database"):
        await cmd.validate()


async def test_create_dataset_validates_database_not_found(mock_dao):
    mock_dao.get_database_by_id = AsyncMock(return_value=None)
    cmd = CreateDatasetCommand(dao=mock_dao, data={"table_name": "t", "database": 999})
    with pytest.raises(CommandInvalidError, match="Database not found"):
        await cmd.validate()


async def test_create_dataset_validates_uniqueness(mock_dao):
    mock_dao.get_database_by_id = AsyncMock(return_value=MagicMock())
    mock_dao.validate_uniqueness = AsyncMock(return_value=False)
    cmd = CreateDatasetCommand(dao=mock_dao, data={"table_name": "t", "database": 1})
    with pytest.raises(CommandInvalidError, match="already exists"):
        await cmd.validate()


async def test_create_dataset_validation_success(mock_dao):
    mock_dao.get_database_by_id = AsyncMock(return_value=MagicMock())
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
    with pytest.raises(CommandInvalidError, match="already exists"):
        await cmd.validate()


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
    mock_dao.session.flush.assert_awaited_once()


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
    with pytest.raises(CommandInvalidError, match="table_name"):
        await cmd.validate()


async def test_duplicate_source_not_found(mock_dao):
    mock_dao.find_by_id = AsyncMock(return_value=None)
    cmd = DuplicateDatasetCommand(dao=mock_dao, base_model_id=999, table_name="dup")
    with pytest.raises(ObjectNotFoundError):
        await cmd.validate()


async def test_duplicate_success(mock_dao, mock_dataset):
    mock_dataset.kind = "virtual"  # Only virtual datasets can be duplicated
    mock_dataset.sql = "SELECT 1"
    mock_dao.find_by_id = AsyncMock(return_value=mock_dataset)
    cmd = DuplicateDatasetCommand(dao=mock_dao, base_model_id=1, table_name="dup_table")
    await cmd.validate()
    mock_sqla_table = MagicMock()
    mock_module = MagicMock()
    mock_module.SqlaTable = mock_sqla_table
    with patch.dict(sys.modules, {"superset.connectors.sqla.models": mock_module}):
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
    with pytest.raises(CommandInvalidError, match="table_name"):
        await cmd.validate()


async def test_get_or_create_validates_database(mock_dao):
    cmd = GetOrCreateDatasetCommand(dao=mock_dao, data={"table_name": "t"})
    with pytest.raises(CommandInvalidError, match="database"):
        await cmd.validate()


async def test_get_or_create_returns_existing(mock_dao, mock_dataset):
    mock_dao.find_one_or_none = AsyncMock(return_value=mock_dataset)
    cmd = GetOrCreateDatasetCommand(
        dao=mock_dao,
        data={"table_name": "test_table", "database": 10},
    )
    await cmd.validate()
    mock_module = MagicMock()
    with patch.dict(sys.modules, {"superset.connectors.sqla.models": mock_module}):
        result = await cmd.run()
    assert result is mock_dataset
    mock_dao.session.add.assert_not_called()


async def test_get_or_create_creates_new(mock_dao):
    mock_dao.find_one_or_none = AsyncMock(return_value=None)
    cmd = GetOrCreateDatasetCommand(
        dao=mock_dao,
        data={"table_name": "new_table", "database": 10},
        user_id=1,
    )
    await cmd.validate()
    mock_sqla_table = MagicMock()
    mock_module = MagicMock()
    mock_module.SqlaTable = mock_sqla_table
    with patch.dict(sys.modules, {"superset.connectors.sqla.models": mock_module}):
        await cmd.run()
    mock_dao.session.add.assert_called_once()
    mock_dao.session.flush.assert_awaited_once()


# ---- ExportDatasetsCommand ----


async def test_export_produces_zip(mock_dao, mock_dataset):
    mock_dao.find_by_id = AsyncMock(return_value=mock_dataset)
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
    mock_dao.find_by_id = AsyncMock(return_value=None)
    cmd = ExportDatasetsCommand(model_ids=[999], dao=mock_dao)
    with pytest.raises(ObjectNotFoundError):
        await cmd.execute()


# ---- WarmUpDatasetCacheCommand ----


async def test_warm_up_validates_db_name(mock_dao):
    cmd = WarmUpDatasetCacheCommand(dao=mock_dao, db_name="", table_name="t")
    with pytest.raises(CommandInvalidError, match="db_name"):
        await cmd.validate()


async def test_warm_up_validates_table_name(mock_dao):
    cmd = WarmUpDatasetCacheCommand(dao=mock_dao, db_name="db", table_name="")
    with pytest.raises(CommandInvalidError, match="table_name"):
        await cmd.validate()


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
        side_effect=SupersetSecurityException(message="You don't have permission")
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
        side_effect=SupersetSecurityException(message="You don't have permission")
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
        side_effect=SupersetSecurityException(message="You don't have permission")
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
    mock_dao.find_by_id = AsyncMock(return_value=mock_dataset)
    cmd = DuplicateDatasetCommand(dao=mock_dao, base_model_id=1, table_name="dup")
    with pytest.raises(CommandInvalidError, match="Only virtual datasets"):
        await cmd.validate()


async def test_duplicate_dataset_no_kind_attribute_rejected(mock_dao):
    """Dataset without sql attribute (None) is rejected as non-virtual."""
    source = MagicMock()
    source.id = 1
    source.kind = None
    source.sql = None
    mock_dao.find_by_id = AsyncMock(return_value=source)
    cmd = DuplicateDatasetCommand(dao=mock_dao, base_model_id=1, table_name="dup")
    with pytest.raises(CommandInvalidError, match="Only virtual datasets"):
        await cmd.validate()


# ---------------------------------------------------------------------------
# NEW-T6: BulkDelete — "some IDs not found" branch
# ---------------------------------------------------------------------------


async def test_bulk_delete_some_ids_not_found(mock_dao, mock_dataset):
    """BulkDeleteDatasetsCommand raises ObjectNotFoundError when some IDs are missing."""
    mock_dao.find_by_ids = AsyncMock(return_value=[mock_dataset])  # only id=1 found
    cmd = BulkDeleteDatasetsCommand(dao=mock_dao, dataset_ids=[1, 2, 3])
    with pytest.raises(ObjectNotFoundError):
        await cmd.validate()
