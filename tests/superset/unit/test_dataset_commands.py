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
    ds.catalog = None
    ds.database = MagicMock()
    ds.database.uuid = None
    ds.database.database_name = "test_db"
    ds.database.sqlalchemy_uri = "sqlite:///test.db"
    # Real bool / catalog so the dataset-source validation in
    # ``UpdateDatasetCommand.validate`` (catalog coercion + ``Table.__str__``)
    # never operates on a bare MagicMock.
    ds.database.allow_multi_catalog = False
    ds.database.get_default_catalog.return_value = None
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


async def test_create_dataset_validates_owners(mock_dao):
    """A non-resolvable owner id is reported per-field under ``owners`` (1:1
    upstream — owner validation happens in validate(), not run())."""
    mock_dao.get_database_by_id = AsyncMock(return_value=_physical_database())
    mock_dao.validate_uniqueness = AsyncMock(return_value=True)
    sm = MagicMock()
    sm.find_user_by_id = AsyncMock(return_value=None)  # owner id 999 unresolved
    sm.is_admin = MagicMock(return_value=True)
    cmd = CreateDatasetCommand(
        dao=mock_dao,
        data={"table_name": "t", "database": 1, "owners": [999]},
        user_id=1,
        security_manager=sm,
    )
    with pytest.raises(DatasetInvalidError) as exc_info:
        await cmd.validate()
    assert exc_info.value.normalized_messages()["owners"] == ["Owners are invalid"]


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


async def test_update_dataset_validates_owners(mock_dao, mock_dataset):
    """A non-resolvable owner id on update is reported per-field under
    ``owners`` (1:1 upstream — validated in validate(), not run())."""
    mock_dataset.owners = []
    mock_dao.find_by_id = AsyncMock(return_value=mock_dataset)
    mock_dao.validate_uniqueness = AsyncMock(return_value=True)
    sm = MagicMock()
    sm.raise_for_ownership = AsyncMock(return_value=None)
    sm.find_user_by_id = AsyncMock(return_value=None)
    sm.is_admin = MagicMock(return_value=True)
    cmd = UpdateDatasetCommand(
        dao=mock_dao,
        dataset_id=1,
        data={"owners": [999]},
        security_manager=sm,
    )
    with pytest.raises(DatasetInvalidError) as exc_info:
        await cmd.validate()
    assert exc_info.value.normalized_messages()["owners"] == ["Owners are invalid"]


async def test_update_dataset_changed_database_not_found(mock_dao, mock_dataset):
    """A changed ``database_id`` that does not resolve is reported per-field
    under ``database`` (1:1 upstream ``_validate_dataset_source``)."""
    mock_dao.find_by_id = AsyncMock(return_value=mock_dataset)
    mock_dao.get_database_by_id = AsyncMock(return_value=None)
    mock_dao.validate_uniqueness = AsyncMock(return_value=True)
    cmd = UpdateDatasetCommand(
        dao=mock_dao,
        dataset_id=1,
        # database_id differs from mock_dataset.database_id (10).
        data={"table_name": "t", "database_id": 999},
    )
    with pytest.raises(DatasetInvalidError) as exc_info:
        await cmd.validate()
    messages = exc_info.value.normalized_messages()
    assert messages["database"] == ["Database does not exist"]


async def test_update_dataset_sql_access_denied(mock_dao, mock_dataset):
    """SQL-access denial surfaces as a per-field ``sql`` 422 from validate()
    (not a flat error in run()) — 1:1 upstream ``_validate_sql_access``."""
    mock_dataset.sql = "SELECT 1"
    mock_dao.find_by_id = AsyncMock(return_value=mock_dataset)
    mock_dao.validate_uniqueness = AsyncMock(return_value=True)
    sm = MagicMock()
    sm.raise_for_ownership = AsyncMock(return_value=None)
    sm.find_user_by_id = AsyncMock(return_value=None)
    sm.raise_for_access = AsyncMock(side_effect=_security_exception("no sql access"))
    cmd = UpdateDatasetCommand(
        dao=mock_dao,
        dataset_id=1,
        # changed sql triggers the access check
        data={"sql": "SELECT 2"},
        security_manager=sm,
    )
    with pytest.raises(DatasetInvalidError) as exc_info:
        await cmd.validate()
    messages = exc_info.value.normalized_messages()
    assert messages["sql"] == ["no sql access"]


async def test_update_dataset_invalid_sql_parse_error(mock_dao, mock_dataset):
    """A ``SupersetParseError`` during SQL-access validation is reported as an
    ``Invalid SQL: ...`` per-field ``sql`` 422 — 1:1 upstream."""
    from superset.exceptions import SupersetParseError

    mock_dataset.sql = "SELECT 1"
    mock_dao.find_by_id = AsyncMock(return_value=mock_dataset)
    mock_dao.validate_uniqueness = AsyncMock(return_value=True)
    parse_err = SupersetParseError(
        sql="SELECT 2", engine="postgresql", message="bad syntax"
    )
    sm = MagicMock()
    sm.raise_for_ownership = AsyncMock(return_value=None)
    sm.find_user_by_id = AsyncMock(return_value=None)
    sm.raise_for_access = AsyncMock(side_effect=parse_err)
    cmd = UpdateDatasetCommand(
        dao=mock_dao,
        dataset_id=1,
        data={"sql": "SELECT 2"},
        security_manager=sm,
    )
    with pytest.raises(DatasetInvalidError) as exc_info:
        await cmd.validate()
    messages = exc_info.value.normalized_messages()
    assert messages["sql"] == ["Invalid SQL: bad syntax"]


async def test_update_dataset_multi_catalog_disabled(mock_dao, mock_dataset):
    """A non-default catalog while the connection disallows multi-catalog is
    reported per-field under ``catalog`` — 1:1 upstream
    ``_validate_dataset_source`` (MultiCatalogDisabledValidationError)."""
    mock_dataset.database.allow_multi_catalog = False
    mock_dataset.database.get_default_catalog.return_value = "default_cat"
    mock_dao.find_by_id = AsyncMock(return_value=mock_dataset)
    mock_dao.validate_uniqueness = AsyncMock(return_value=True)
    cmd = UpdateDatasetCommand(
        dao=mock_dao,
        dataset_id=1,
        data={"catalog": "other_cat"},
    )
    with pytest.raises(DatasetInvalidError) as exc_info:
        await cmd.validate()
    messages = exc_info.value.normalized_messages()
    assert messages["catalog"] == [
        "Only the default catalog is supported for this connection"
    ]


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
    # 1:1 with upstream: a missing base dataset accumulates into the
    # validation error set (422 DatasetInvalidError), not an early 404.
    from superset.commands.dataset.exceptions import DatasetInvalidError

    mock_dao.find_by_id = AsyncMock(return_value=None)
    mock_dao.find_one_or_none = AsyncMock(return_value=None)
    cmd = DuplicateDatasetCommand(dao=mock_dao, base_model_id=999, table_name="dup")
    with pytest.raises(DatasetInvalidError):
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
    """Creation routes through ``CreateDatasetCommand`` (1:1 upstream
    ``get_or_create`` → ``CreateDatasetCommand(body).run()``) so the new dataset
    gets ``fetch_metadata()`` (column/metric introspection) and owners — not the
    old bare ``SqlaTable`` + add/flush."""
    mock_dao.find_one_or_none = AsyncMock(return_value=None)
    mock_dao.get_database_by_id = AsyncMock(return_value=_physical_database())
    mock_dao.validate_uniqueness = AsyncMock(return_value=True)
    mock_dao.fetch_metadata = AsyncMock(return_value=None)
    sm = MagicMock()
    # ``populate_owner_list`` defaults to the current user; return a non-empty
    # owners list to assert the default-to-user side effect.
    owner = MagicMock()
    owner.id = 1
    cmd = GetOrCreateDatasetCommand(
        dao=mock_dao,
        data={"table_name": "new_table", "database_id": 10},
        user_id=1,
        security_manager=sm,
    )
    await cmd.validate()
    with patch(
        "superset.commands.utils.populate_owner_list",
        AsyncMock(return_value=[owner]),
    ):
        dataset = await cmd.run()
    # add() is called for the dataset (and again for implicit tags) — at least
    # the dataset itself is persisted.
    mock_dao.session.add.assert_called()
    mock_dao.session.flush.assert_awaited()
    # fetch_metadata is invoked by the delegated CreateDatasetCommand.run().
    mock_dao.fetch_metadata.assert_awaited_once()
    # Owners default to the current user (non-empty list assigned).
    assert dataset.owners == [owner]


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
    # validate() now checks per-id access; return count == len(model_ids) so
    # the check passes for the single requested id.
    mock_dao.count = AsyncMock(return_value=1)
    cmd = ExportDatasetsCommand(model_ids=[1], dao=mock_dao)
    with patch(
        "superset.db.filters.dataset_access_filters", AsyncMock(return_value=[])
    ):
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
    # count=0 → validate() raises ObjectNotFoundError before _export_single runs.
    mock_dao.find_by_id_with_options = AsyncMock(return_value=None)
    mock_dao.count = AsyncMock(return_value=0)
    cmd = ExportDatasetsCommand(model_ids=[999], dao=mock_dao)
    with patch(
        "superset.db.filters.dataset_access_filters", AsyncMock(return_value=[])
    ):
        with pytest.raises(ObjectNotFoundError):
            await cmd.execute()


async def test_export_datasets_denies_inaccessible_id(mock_dao):
    """validate() raises ObjectNotFoundError when the DAO reports fewer
    accessible rows than the number of requested IDs (IDOR prevention)."""
    mock_dao.count = AsyncMock(return_value=0)
    cmd = ExportDatasetsCommand(
        model_ids=[42], dao=mock_dao, security_manager=MagicMock()
    )
    with patch(
        "superset.db.filters.dataset_access_filters", AsyncMock(return_value=[])
    ):
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


async def test_warm_up_runs_chart_command_via_execute(mock_dao, monkeypatch):
    """Regression: the per-chart command MUST go through execute() AND be
    handed the already-loaded Slice (``chart=``) — 1:1 with upstream where
    ``ChartWarmUpCacheCommand.validate()`` short-circuits for a Slice
    instance instead of re-fetching it per chart (the N-query regression).
    The stub mirrors that contract exactly.
    """
    from superset.commands.dataset import warm_up_cache as wuc_module

    calls: list[int] = []

    class StubChartCmd:
        def __init__(
            self,
            dao,
            chart_id=None,
            dashboard_id=None,
            extra_filters=None,
            chart=None,
            security_manager=None,
            current_user=None,
        ):
            self._chart = chart
            self._chart_id = (
                chart_id if chart_id is not None else getattr(chart, "id", None)
            )

        async def validate(self):
            # Pre-loaded chart → no DB round-trip (the contract under test).
            if self._chart is None:
                raise AssertionError(
                    "dataset warm-up must pass the loaded chart, not chart_id"
                )

        async def run(self):
            assert self._chart is not None
            calls.append(self._chart_id)
            return {"chart_id": self._chart_id, "viz_status": "success"}

        async def execute(self):
            await self.validate()
            return await self.run()

    monkeypatch.setattr(wuc_module, "WarmUpChartCacheCommand", StubChartCmd)
    monkeypatch.setattr(wuc_module, "AsyncChartDAO", lambda session: MagicMock())

    cmd = WarmUpDatasetCacheCommand(dao=mock_dao, db_name="mydb", table_name="mytable")

    async def fake_validate():
        cmd._charts = [MagicMock(id=11), MagicMock(id=22)]

    monkeypatch.setattr(cmd, "validate", fake_validate)

    results = await cmd.run()
    assert calls == [11, 22]
    assert [r["chart_id"] for r in results] == [11, 22]


# ---- DeleteDatasetColumnCommand ----


async def test_delete_column_dataset_not_found(mock_dao, mock_column_dao):
    """A missing dataset surfaces as a missing COLUMN (the lookup is scoped
    by ``(dataset_id, column_id)``) — 1:1 with upstream
    ``DatasetDAO.find_dataset_column`` → ``DatasetColumnNotFoundError``."""
    mock_column_dao.find_by_dataset_and_id = AsyncMock(return_value=None)
    cmd = DeleteDatasetColumnCommand(
        dataset_dao=mock_dao,
        column_dao=mock_column_dao,
        dataset_id=999,
        column_id=1,
    )
    with pytest.raises(ObjectNotFoundError, match="DatasetColumn"):
        await cmd.validate()


async def test_delete_column_ownership_checked_on_column(mock_dao, mock_column_dao):
    """Ownership is checked on the COLUMN itself — 1:1 with upstream
    ``raise_for_ownership(self._model)`` where ``_model`` is the TableColumn
    (superset_old/commands/dataset/columns/delete.py:30-36).  TableColumn has
    no ``owners``, so non-admins are always denied (effectively admin-only);
    checking the parent DATASET instead silently widened access to dataset
    owners (R14-07)."""
    mock_column = MagicMock()
    mock_column_dao.find_by_dataset_and_id = AsyncMock(return_value=mock_column)
    sm = AsyncMock()
    cmd = DeleteDatasetColumnCommand(
        dataset_dao=mock_dao,
        column_dao=mock_column_dao,
        dataset_id=1,
        column_id=5,
        security_manager=sm,
        user_id=42,
    )
    await cmd.validate()
    sm.raise_for_ownership.assert_awaited_once_with(mock_column, 42)


async def test_delete_column_missing_is_404_before_ownership(
    mock_dao, mock_column_dao
):
    """Column existence is validated BEFORE ownership — a non-owner probing a
    nonexistent column gets 404, not 403 (upstream order: find → ownership)."""
    mock_column_dao.find_by_dataset_and_id = AsyncMock(return_value=None)
    sm = AsyncMock()
    sm.raise_for_ownership = AsyncMock(side_effect=_security_exception())
    cmd = DeleteDatasetColumnCommand(
        dataset_dao=mock_dao,
        column_dao=mock_column_dao,
        dataset_id=1,
        column_id=999,
        security_manager=sm,
        user_id=42,
    )
    with pytest.raises(ObjectNotFoundError, match="DatasetColumn"):
        await cmd.validate()
    sm.raise_for_ownership.assert_not_awaited()


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
    """Missing dataset surfaces as a missing METRIC (scoped lookup) — 1:1
    upstream ``find_dataset_metric`` → ``DatasetMetricNotFoundError``."""
    mock_metric_dao.find_by_dataset_and_id = AsyncMock(return_value=None)
    cmd = DeleteDatasetMetricCommand(
        dataset_dao=mock_dao,
        metric_dao=mock_metric_dao,
        dataset_id=999,
        metric_id=1,
    )
    with pytest.raises(ObjectNotFoundError, match="DatasetMetric"):
        await cmd.validate()


async def test_delete_metric_ownership_checked_on_metric(mock_dao, mock_metric_dao):
    """Ownership is checked on the METRIC itself (1:1 upstream, R14-07)."""
    mock_metric = MagicMock()
    mock_metric_dao.find_by_dataset_and_id = AsyncMock(return_value=mock_metric)
    sm = AsyncMock()
    cmd = DeleteDatasetMetricCommand(
        dataset_dao=mock_dao,
        metric_dao=mock_metric_dao,
        dataset_id=1,
        metric_id=7,
        security_manager=sm,
        user_id=42,
    )
    await cmd.validate()
    sm.raise_for_ownership.assert_awaited_once_with(mock_metric, 42)


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


async def test_update_dataset_override_columns_reaches_dao(mock_dao, mock_dataset):
    """``override_columns`` + body columns must flow into ``dao.update``.

    1:1 with superset_old: the command stores the flag in its properties
    (superset_old/commands/dataset/update.py:68) and the DAO picks the
    delete-all-and-reinsert path via ``attributes.get("override_columns")``
    (superset_old/daos/dataset.py:188-193). The body ``columns`` must NOT be
    dropped — the original applies them first and lets RefreshDatasetCommand
    only update types afterwards.
    """
    mock_dao.find_by_id = AsyncMock(return_value=mock_dataset)
    mock_dao.validate_uniqueness = AsyncMock(return_value=True)
    mock_dao.validate_columns_exist = AsyncMock(return_value=True)
    mock_dao.validate_columns_uniqueness = AsyncMock(return_value=True)
    mock_dao.update = AsyncMock(return_value=mock_dataset)

    body_columns = [{"column_name": "virtual_col", "expression": "a + b"}]
    cmd = UpdateDatasetCommand(
        dao=mock_dao,
        dataset_id=1,
        data={"columns": body_columns},
        override_columns=True,
    )
    await cmd.validate()
    await cmd.run()

    mock_dao.update.assert_awaited_once()
    _, attributes = mock_dao.update.await_args.args
    assert attributes["override_columns"] is True
    assert attributes["columns"] == body_columns


@pytest.mark.asyncio
async def test_dao_update_columns_dispatches_override_path():
    """``update_columns(override_columns=True)`` takes the override branch."""
    from superset.db.daos.dataset import AsyncDatasetDAO

    session = MagicMock()
    session.refresh = AsyncMock()
    dao = AsyncDatasetDAO(session=session)
    model = MagicMock()
    model.id = 1

    with patch.object(
        AsyncDatasetDAO, "_apply_columns_override", new_callable=AsyncMock
    ) as override:
        await dao.update_columns(model, [{"column_name": "c1"}], override_columns=True)
    override.assert_awaited_once_with(model, [{"column_name": "c1"}])
