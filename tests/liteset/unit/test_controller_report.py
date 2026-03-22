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

from liteset.commands.report import (
    BulkDeleteReportScheduleCommand,
    CreateReportScheduleCommand,
    DeleteReportScheduleCommand,
    UpdateReportScheduleCommand,
)
from liteset.exceptions import CommandInvalidError, ObjectNotFoundError


@pytest.fixture
def mock_dao():
    dao = AsyncMock()
    dao.session = AsyncMock()
    dao.session.add = MagicMock()
    dao.session.flush = AsyncMock()
    dao.session.delete = AsyncMock()
    return dao


@pytest.fixture
def mock_report():
    report = MagicMock()
    report.id = 1
    report.name = "Test Report"
    report.type = "Report"
    report.crontab = "0 * * * *"
    report.timezone = "UTC"
    report.active = True
    report.description = ""
    report.chart_id = None
    report.dashboard_id = None
    report.database_id = None
    return report


# --- CreateReportScheduleCommand ---


async def test_create_report_validates_name_required(mock_dao):
    cmd = CreateReportScheduleCommand(
        dao=mock_dao, data={"type": "Report"}, user_id=1
    )
    with pytest.raises(CommandInvalidError, match="name is required"):
        await cmd.validate()


async def test_create_report_validates_empty_name(mock_dao):
    cmd = CreateReportScheduleCommand(
        dao=mock_dao, data={"name": "  ", "type": "Report"}, user_id=1
    )
    # Empty/whitespace name should fail — but our check is `not name.strip()`
    # which means whitespace-only also fails
    with pytest.raises(CommandInvalidError, match="name is required"):
        await cmd.validate()


async def test_create_report_validates_type_required(mock_dao):
    cmd = CreateReportScheduleCommand(
        dao=mock_dao, data={"name": "Test"}, user_id=1
    )
    with pytest.raises(CommandInvalidError, match="type is required"):
        await cmd.validate()


async def test_create_report_validates_crontab_invalid(mock_dao):
    """Invalid crontab expression should raise CommandInvalidError."""
    mock_dao.validate_update_uniqueness = AsyncMock(return_value=True)
    cmd = CreateReportScheduleCommand(
        dao=mock_dao,
        data={"name": "Test", "type": "Report", "crontab": "not a cron"},
        user_id=1,
    )
    # croniter may not be installed in test env; skip if not available
    try:
        from croniter import croniter  # noqa: F401
    except ImportError:
        pytest.skip("croniter not installed")
    with pytest.raises(CommandInvalidError, match="Invalid crontab"):
        await cmd.validate()


async def test_create_report_validates_uniqueness(mock_dao):
    """Duplicate name+type should raise CommandInvalidError."""
    mock_dao.validate_update_uniqueness = AsyncMock(return_value=False)
    cmd = CreateReportScheduleCommand(
        dao=mock_dao,
        data={"name": "Existing", "type": "Report", "crontab": "0 * * * *"},
        user_id=1,
    )
    with pytest.raises(CommandInvalidError, match="already exists"):
        await cmd.validate()


async def test_create_report_success(mock_dao, mock_report):
    """Valid data should pass validation and create the report."""
    mock_dao.validate_update_uniqueness = AsyncMock(return_value=True)
    mock_dao.create = AsyncMock(return_value=mock_report)
    cmd = CreateReportScheduleCommand(
        dao=mock_dao,
        data={
            "name": "Test Report",
            "type": "Report",
            "crontab": "0 * * * *",
        },
        user_id=1,
    )
    await cmd.validate()
    result = await cmd.run()
    assert result.id == 1
    assert result.name == "Test Report"
    mock_dao.create.assert_awaited_once()


# --- UpdateReportScheduleCommand ---


async def test_update_report_not_found(mock_dao):
    mock_dao.find_by_id = AsyncMock(return_value=None)
    cmd = UpdateReportScheduleCommand(dao=mock_dao, pk=999, data={"name": "X"})
    with pytest.raises(ObjectNotFoundError):
        await cmd.validate()


async def test_update_report_validates_uniqueness(mock_dao, mock_report):
    """Changing name should validate uniqueness."""
    mock_dao.find_by_id = AsyncMock(return_value=mock_report)
    mock_dao.validate_update_uniqueness = AsyncMock(return_value=False)
    cmd = UpdateReportScheduleCommand(
        dao=mock_dao, pk=1, data={"name": "Duplicate Name"}
    )
    with pytest.raises(CommandInvalidError, match="already exists"):
        await cmd.validate()


async def test_update_report_success(mock_dao, mock_report):
    mock_dao.find_by_id = AsyncMock(return_value=mock_report)
    mock_dao.validate_update_uniqueness = AsyncMock(return_value=True)
    mock_dao.update = AsyncMock(return_value=mock_report)
    cmd = UpdateReportScheduleCommand(
        dao=mock_dao, pk=1, data={"name": "Updated Name"}
    )
    await cmd.validate()
    result = await cmd.run()
    assert result.id == 1
    mock_dao.update.assert_awaited_once()
    mock_dao.session.flush.assert_awaited_once()


# --- DeleteReportScheduleCommand ---


async def test_delete_report_not_found(mock_dao):
    mock_dao.find_by_id = AsyncMock(return_value=None)
    cmd = DeleteReportScheduleCommand(dao=mock_dao, pk=999)
    with pytest.raises(ObjectNotFoundError):
        await cmd.validate()


async def test_delete_report_success(mock_dao, mock_report):
    mock_dao.find_by_id = AsyncMock(return_value=mock_report)
    mock_dao.delete = AsyncMock()
    cmd = DeleteReportScheduleCommand(dao=mock_dao, pk=1)
    await cmd.validate()
    await cmd.run()
    mock_dao.delete.assert_awaited_once_with([mock_report])
    mock_dao.session.flush.assert_awaited_once()


# --- BulkDeleteReportScheduleCommand ---


async def test_bulk_delete_empty_ids(mock_dao):
    cmd = BulkDeleteReportScheduleCommand(dao=mock_dao, ids=[])
    with pytest.raises(CommandInvalidError, match="No report schedule IDs"):
        await cmd.validate()


async def test_bulk_delete_missing_ids(mock_dao, mock_report):
    """IDs not found in the database should raise ObjectNotFoundError."""
    mock_dao.find_by_ids = AsyncMock(return_value=[mock_report])
    cmd = BulkDeleteReportScheduleCommand(dao=mock_dao, ids=[1, 2, 3])
    with pytest.raises(ObjectNotFoundError):
        await cmd.validate()


async def test_bulk_delete_success(mock_dao, mock_report):
    report2 = MagicMock()
    report2.id = 2
    mock_dao.find_by_ids = AsyncMock(return_value=[mock_report, report2])
    mock_dao.delete = AsyncMock()
    cmd = BulkDeleteReportScheduleCommand(dao=mock_dao, ids=[1, 2])
    await cmd.validate()
    await cmd.run()
    mock_dao.delete.assert_awaited_once_with([mock_report, report2])
    mock_dao.session.flush.assert_awaited_once()
