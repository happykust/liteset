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
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from superset.commands.chart import (
    BulkDeleteChartsCommand,
    CreateChartCommand,
    DeleteChartCommand,
    ExportChartsCommand,
    UpdateChartCommand,
    WarmUpChartCacheCommand,
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
def mock_chart():
    chart = MagicMock()
    chart.id = 1
    chart.slice_name = "Test Chart"
    chart.viz_type = "table"
    chart.params = "{}"
    chart.query_context = None
    chart.cache_timeout = None
    chart.uuid = None
    chart.datasource_id = 1
    chart.datasource_type = "table"
    chart.datasource = None  # No datasource object for export bundling
    return chart


async def test_create_chart_validates_slice_name(mock_dao):
    cmd = CreateChartCommand(dao=mock_dao, data={"viz_type": "table"})
    with pytest.raises(CommandInvalidError, match="slice_name"):
        await cmd.validate()


async def test_create_chart_validates_viz_type(mock_dao):
    cmd = CreateChartCommand(dao=mock_dao, data={"slice_name": "Test"})
    with pytest.raises(CommandInvalidError, match="viz_type"):
        await cmd.validate()


async def test_create_chart_validates_success(mock_dao):
    cmd = CreateChartCommand(
        dao=mock_dao,
        data={"slice_name": "Test", "viz_type": "table"},
        user_id=1,
    )
    await cmd.validate()  # Should not raise


async def test_update_chart_not_found(mock_dao):
    mock_dao.find_by_id = AsyncMock(return_value=None)
    cmd = UpdateChartCommand(dao=mock_dao, chart_id=999, data={"slice_name": "X"})
    with pytest.raises(ObjectNotFoundError):
        await cmd.validate()


async def test_update_chart_success(mock_dao, mock_chart):
    mock_dao.find_by_id = AsyncMock(return_value=mock_chart)
    cmd = UpdateChartCommand(
        dao=mock_dao,
        chart_id=1,
        data={"slice_name": "Updated"},
    )
    await cmd.validate()
    result = await cmd.run()
    assert result.slice_name == "Updated"
    mock_dao.session.flush.assert_awaited_once()


async def test_delete_chart_not_found(mock_dao):
    mock_dao.find_by_id = AsyncMock(return_value=None)
    cmd = DeleteChartCommand(dao=mock_dao, chart_id=999)
    with pytest.raises(ObjectNotFoundError):
        await cmd.validate()


async def test_delete_chart_success(mock_dao, mock_chart):
    mock_dao.find_by_id = AsyncMock(return_value=mock_chart)
    mock_dao.find_report_schedules_by_chart_id = AsyncMock(return_value=[])
    cmd = DeleteChartCommand(dao=mock_dao, chart_id=1)
    await cmd.validate()
    await cmd.run()
    mock_dao.delete.assert_awaited_once_with([mock_chart])


async def test_bulk_delete_empty_ids(mock_dao):
    cmd = BulkDeleteChartsCommand(dao=mock_dao, chart_ids=[])
    with pytest.raises(CommandInvalidError, match="No chart IDs"):
        await cmd.validate()


async def test_bulk_delete_success(mock_dao, mock_chart):
    mock_dao.find_by_ids = AsyncMock(return_value=[mock_chart])
    cmd = BulkDeleteChartsCommand(dao=mock_dao, chart_ids=[1])
    await cmd.validate()
    await cmd.run()
    mock_dao.delete.assert_awaited_once_with([mock_chart])


async def test_export_charts_produces_zip(mock_dao, mock_chart):
    mock_dao.find_by_id = AsyncMock(return_value=mock_chart)
    cmd = ExportChartsCommand(model_ids=[1], dao=mock_dao)
    buf = await cmd.execute()
    assert isinstance(buf, io.BytesIO)
    with zipfile.ZipFile(buf) as zf:
        names = zf.namelist()
        assert any("charts/" in n for n in names)
        assert "metadata.yaml" in names
        # Verify YAML content contains known fields
        chart_files = [n for n in names if n.startswith("charts/")]
        content = yaml.safe_load(zf.read(chart_files[0]))
        assert content["slice_name"] == "Test Chart"
        assert content["viz_type"] == "table"


async def test_warm_up_cache(mock_dao, mock_chart):
    mock_dao.find_by_id = AsyncMock(return_value=mock_chart)
    cmd = WarmUpChartCacheCommand(dao=mock_dao, chart_id=1)
    result = await cmd.execute()
    assert result[0]["chart_id"] == 1
    assert result[0]["viz_status"] == "success"


# ---------------------------------------------------------------------------
# Ownership checks
# ---------------------------------------------------------------------------


async def test_delete_non_owner_raises_forbidden(mock_dao, mock_chart):
    mock_dao.find_by_id = AsyncMock(return_value=mock_chart)
    sm = AsyncMock()
    sm.raise_for_ownership = AsyncMock(
        side_effect=SupersetSecurityException(message="You don't have permission")
    )
    cmd = DeleteChartCommand(dao=mock_dao, chart_id=1, security_manager=sm, user_id=42)
    with pytest.raises(SupersetSecurityException, match="permission"):
        await cmd.validate()


async def test_update_non_owner_raises_forbidden(mock_dao, mock_chart):
    mock_dao.find_by_id = AsyncMock(return_value=mock_chart)
    sm = AsyncMock()
    sm.raise_for_ownership = AsyncMock(
        side_effect=SupersetSecurityException(message="You don't have permission")
    )
    cmd = UpdateChartCommand(
        dao=mock_dao,
        chart_id=1,
        data={"slice_name": "Updated"},
        user_id=42,
        security_manager=sm,
    )
    with pytest.raises(SupersetSecurityException, match="permission"):
        await cmd.validate()


async def test_bulk_delete_non_owner_raises_forbidden(mock_dao, mock_chart):
    mock_dao.find_by_ids = AsyncMock(return_value=[mock_chart])
    sm = AsyncMock()
    sm.raise_for_ownership = AsyncMock(
        side_effect=SupersetSecurityException(message="You don't have permission")
    )
    cmd = BulkDeleteChartsCommand(
        dao=mock_dao, chart_ids=[1], security_manager=sm, user_id=42
    )
    with pytest.raises(SupersetSecurityException, match="permission"):
        await cmd.validate()


# ---------------------------------------------------------------------------
# NEW-T2: Report schedule guard for DeleteChartCommand
# ---------------------------------------------------------------------------


async def test_delete_chart_with_report_schedules_raises(mock_dao, mock_chart):
    """DeleteChartCommand blocks deletion when report schedules exist."""
    mock_dao.find_by_id = AsyncMock(return_value=mock_chart)
    report = MagicMock()
    report.name = "Weekly Report"
    mock_dao.find_report_schedules_by_chart_id = AsyncMock(return_value=[report])
    cmd = DeleteChartCommand(dao=mock_dao, chart_id=1)
    with pytest.raises(CommandInvalidError, match="report schedules"):
        await cmd.validate()


# ---------------------------------------------------------------------------
# NEW-T3: CreateChartCommand.run() owner resolution branches
# ---------------------------------------------------------------------------


async def test_create_chart_run_explicit_owners(mock_dao):
    """run() resolves explicit owner IDs via security_manager."""
    sm = AsyncMock()
    owner_obj = MagicMock()
    owner_obj.id = 10
    sm.find_user_by_id = AsyncMock(return_value=owner_obj)

    cmd = CreateChartCommand(
        dao=mock_dao,
        data={"slice_name": "Chart A", "viz_type": "table", "owners": [10]},
        user_id=1,
        security_manager=sm,
    )
    await cmd.validate()

    mock_slice_cls = MagicMock()
    instance = MagicMock()
    instance.owners = []
    mock_slice_cls.return_value = instance
    import sys
    from unittest.mock import patch

    with patch.dict(
        sys.modules, {"superset.models.slice": MagicMock(Slice=mock_slice_cls)}
    ):
        result = await cmd.run()

    sm.find_user_by_id.assert_awaited_with(10)
    assert result.owners == [owner_obj]


async def test_create_chart_run_auto_assign_current_user(mock_dao):
    """run() auto-assigns current user as owner when no explicit owners."""
    sm = AsyncMock()
    user_obj = MagicMock()
    user_obj.id = 5
    sm.find_user_by_id = AsyncMock(return_value=user_obj)

    cmd = CreateChartCommand(
        dao=mock_dao,
        data={"slice_name": "Chart B", "viz_type": "bar"},
        user_id=5,
        security_manager=sm,
    )
    await cmd.validate()

    mock_slice_cls = MagicMock()
    instance = MagicMock()
    instance.owners = []
    mock_slice_cls.return_value = instance
    import sys
    from unittest.mock import patch

    with patch.dict(
        sys.modules, {"superset.models.slice": MagicMock(Slice=mock_slice_cls)}
    ):
        result = await cmd.run()

    sm.find_user_by_id.assert_awaited_with(5)
    assert result.owners == [user_obj]


async def test_create_chart_run_user_not_found(mock_dao):
    """run() handles user not found during auto-assign (owners stays empty)."""
    sm = AsyncMock()
    sm.find_user_by_id = AsyncMock(return_value=None)

    cmd = CreateChartCommand(
        dao=mock_dao,
        data={"slice_name": "Chart C", "viz_type": "line"},
        user_id=99,
        security_manager=sm,
    )
    await cmd.validate()

    mock_slice_cls = MagicMock()
    instance = MagicMock()
    instance.owners = []
    mock_slice_cls.return_value = instance
    import sys
    from unittest.mock import patch

    with patch.dict(
        sys.modules, {"superset.models.slice": MagicMock(Slice=mock_slice_cls)}
    ):
        result = await cmd.run()

    sm.find_user_by_id.assert_awaited_with(99)
    # owners should not have been set (no user found)
    assert result.owners == []
