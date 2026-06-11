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
"""R13-04: report mutations must 404 (not 403) on reports the user can't see.

Upstream ``ReportScheduleDAO.find_by_id(s)`` applies ``ReportScheduleFilter``
(owners-scope unless ``can_access_all_datasources``), so a non-owner without
global datasource access gets ``ReportScheduleNotFoundError`` (404) before the
ownership check ever runs; the 403 is reachable only for users who can see
every report.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from superset.commands.report import (
    BulkDeleteReportScheduleCommand,
    DeleteReportScheduleCommand,
)
from superset.commands.report_exceptions import ReportScheduleForbiddenError
from superset.exceptions import ObjectNotFoundError, SupersetSecurityException


def _denying_sm(can_access_all: bool) -> MagicMock:
    from superset.errors import ErrorLevel, SupersetError, SupersetErrorType

    sm = MagicMock()
    sm.raise_for_ownership = AsyncMock(
        side_effect=SupersetSecurityException(
            SupersetError(
                error_type=SupersetErrorType.MISSING_OWNERSHIP_ERROR,
                message="not an owner",
                level=ErrorLevel.ERROR,
            )
        )
    )
    sm.find_user_by_id = AsyncMock(return_value=MagicMock())
    sm.can_access_all_datasources = AsyncMock(return_value=can_access_all)
    return sm


def _dao_with_report(report: MagicMock) -> MagicMock:
    dao = MagicMock()
    dao.find_by_id = AsyncMock(return_value=report)
    dao.find_by_ids = AsyncMock(return_value=[report])
    return dao


async def test_delete_invisible_report_is_404():
    report = MagicMock(id=10)
    cmd = DeleteReportScheduleCommand(
        dao=_dao_with_report(report),
        pk=10,
        user_id=42,
        security_manager=_denying_sm(can_access_all=False),
    )
    with pytest.raises(ObjectNotFoundError):
        await cmd.validate()


async def test_delete_visible_not_owned_report_is_403():
    report = MagicMock(id=10)
    cmd = DeleteReportScheduleCommand(
        dao=_dao_with_report(report),
        pk=10,
        user_id=42,
        security_manager=_denying_sm(can_access_all=True),
    )
    with pytest.raises(ReportScheduleForbiddenError):
        await cmd.validate()


async def test_bulk_delete_invisible_report_is_404():
    report = MagicMock(id=10)
    cmd = BulkDeleteReportScheduleCommand(
        dao=_dao_with_report(report),
        ids=[10],
        user_id=42,
        security_manager=_denying_sm(can_access_all=False),
    )
    with pytest.raises(ObjectNotFoundError):
        await cmd.validate()


async def test_delete_anonymous_user_is_404():
    report = MagicMock(id=10)
    sm = _denying_sm(can_access_all=True)
    cmd = DeleteReportScheduleCommand(
        dao=_dao_with_report(report),
        pk=10,
        user_id=None,
        security_manager=sm,
    )
    # No user → upstream filter matches nothing → 404 regardless of perms.
    with pytest.raises(ObjectNotFoundError):
        await cmd.validate()
    sm.find_user_by_id.assert_not_awaited()
