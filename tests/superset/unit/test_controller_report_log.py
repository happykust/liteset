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
"""Tests for ReportExecutionLogController."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from superset.controllers.report_log import ReportExecutionLogController
from superset.exceptions import ObjectNotFoundError

# ---------------------------------------------------------------------------
# Helpers -- Litestar decorators wrap methods; access the raw fn for unit tests.
# ---------------------------------------------------------------------------


def _get_raw_method(controller_cls: type, method_name: str):  # type: ignore[type-arg]
    """Return the underlying async function from a Litestar-decorated controller
    method.
    """
    handler = getattr(controller_cls, method_name)
    if hasattr(handler, "fn"):
        return handler.fn
    return handler


_get_list = _get_raw_method(ReportExecutionLogController, "get_list")
_get_single = _get_raw_method(ReportExecutionLogController, "get_single")

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_dao() -> AsyncMock:
    dao = AsyncMock()
    # model_cls.report_schedule_id needs to support == for SQLAlchemy-style filters
    mock_model = MagicMock()
    dao.model_cls = mock_model
    return dao


@pytest.fixture
def controller() -> ReportExecutionLogController:
    return ReportExecutionLogController(owner=MagicMock())


# ---------------------------------------------------------------------------
# get_list tests
# ---------------------------------------------------------------------------


async def test_get_list(
    controller: ReportExecutionLogController, mock_dao: AsyncMock
) -> None:
    mock_dao.find_all.return_value = [MagicMock(), MagicMock()]
    mock_dao.count.return_value = 2
    result = await _get_list(controller, pk=1, dao=mock_dao, rison_params=None)
    assert result["count"] == 2
    assert len(result["result"]) == 2
    mock_dao.find_all.assert_awaited_once()
    mock_dao.count.assert_awaited_once()


async def test_get_list_with_pagination(
    controller: ReportExecutionLogController, mock_dao: AsyncMock
) -> None:
    mock_dao.find_all.return_value = [MagicMock()]
    mock_dao.count.return_value = 50
    result = await _get_list(
        controller,
        pk=1,
        dao=mock_dao,
        rison_params={"page": 2, "page_size": 10},
    )
    assert result["count"] == 50
    call_kwargs = mock_dao.find_all.call_args
    assert call_kwargs.kwargs["page"] == 2
    assert call_kwargs.kwargs["page_size"] == 10


async def test_get_list_empty(
    controller: ReportExecutionLogController, mock_dao: AsyncMock
) -> None:
    mock_dao.find_all.return_value = []
    mock_dao.count.return_value = 0
    result = await _get_list(controller, pk=99, dao=mock_dao, rison_params=None)
    assert result["count"] == 0
    assert result["result"] == []


# ---------------------------------------------------------------------------
# get_single tests
# ---------------------------------------------------------------------------


async def test_get_single(
    controller: ReportExecutionLogController, mock_dao: AsyncMock
) -> None:
    item = MagicMock()
    item.id = 5
    item.report_schedule_id = 1
    mock_dao.find_by_id.return_value = item
    result = await _get_single(controller, pk=1, log_id=5, dao=mock_dao)
    assert result["result"] == item
    mock_dao.find_by_id.assert_awaited_once_with(5)


async def test_get_single_not_found(
    controller: ReportExecutionLogController, mock_dao: AsyncMock
) -> None:
    mock_dao.find_by_id.return_value = None
    with pytest.raises(ObjectNotFoundError):
        await _get_single(controller, pk=1, log_id=999, dao=mock_dao)


async def test_get_single_wrong_report(
    controller: ReportExecutionLogController, mock_dao: AsyncMock
) -> None:
    item = MagicMock()
    item.report_schedule_id = 2  # Different from report_pk=1
    mock_dao.find_by_id.return_value = item
    with pytest.raises(ObjectNotFoundError):
        await _get_single(controller, pk=1, log_id=5, dao=mock_dao)


# ---------------------------------------------------------------------------
# Controller metadata
# ---------------------------------------------------------------------------


def test_controller_path() -> None:
    assert ReportExecutionLogController.path == "/api/v1/report/{pk:int}/log"


def test_controller_tags() -> None:
    assert "Report Execution Log" in ReportExecutionLogController.tags
