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
"""Unit tests for chart data endpoints — verifies endpoints wire through
ChartDataCommand.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from superset.controllers.chart import ChartController
from superset.exceptions import ObjectNotFoundError

# ---------------------------------------------------------------------------
# Helpers — Litestar decorators wrap methods; access the raw fn for unit tests.
# ---------------------------------------------------------------------------


def _get_raw_method(controller_cls: type, method_name: str):
    """Return the underlying async function from a Litestar-decorated controller
    method.
    """
    handler = getattr(controller_cls, method_name)
    # Litestar stores the original function in .fn
    if hasattr(handler, "fn"):
        return handler.fn
    return handler


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_chart_dao():
    return AsyncMock()


@pytest.fixture
def mock_ds_dao():
    return AsyncMock()


@pytest.fixture
def mock_security_manager():
    return MagicMock()


@pytest.fixture
def mock_user():
    user = MagicMock()
    user.id = 1
    user.is_authenticated = True
    user.permissions = {("can_read", "Chart")}
    return user


@pytest.fixture
def mock_state():
    state = MagicMock()
    settings = MagicMock()
    settings.global_async_queries = False
    settings.feature_flags = {}
    state.settings = settings
    return state


@pytest.fixture
def controller():
    return ChartController(owner=MagicMock())


# ---------------------------------------------------------------------------
# get_chart_data tests
# ---------------------------------------------------------------------------

_get_chart_data = _get_raw_method(ChartController, "get_chart_data")
_data = _get_raw_method(ChartController, "data")


async def test_get_chart_data_chart_not_found(
    controller,
    mock_chart_dao,
    mock_ds_dao,
    mock_security_manager,
    mock_user,
    mock_state,
):
    """get_chart_data raises ObjectNotFoundError when chart is missing."""
    mock_chart_dao.find_by_id = AsyncMock(return_value=None)
    with pytest.raises(ObjectNotFoundError):
        await _get_chart_data(
            controller,
            request=MagicMock(),
            pk=999,
            dao=mock_chart_dao,
            ds_dao=mock_ds_dao,
            security_manager=mock_security_manager,
            current_user=mock_user,
            state=mock_state,
        )


async def test_get_chart_data_no_query_context(
    controller,
    mock_chart_dao,
    mock_ds_dao,
    mock_security_manager,
    mock_user,
    mock_state,
):
    """get_chart_data raises validation error when chart has no query_context."""
    from superset.exceptions import SupersetValidationException

    chart = MagicMock()
    chart.query_context = None
    mock_chart_dao.find_by_id = AsyncMock(return_value=chart)
    with pytest.raises(SupersetValidationException, match="no query context"):
        await _get_chart_data(
            controller,
            request=MagicMock(),
            pk=1,
            dao=mock_chart_dao,
            ds_dao=mock_ds_dao,
            security_manager=mock_security_manager,
            current_user=mock_user,
            state=mock_state,
        )


async def test_get_chart_data_invalid_json(
    controller,
    mock_chart_dao,
    mock_ds_dao,
    mock_security_manager,
    mock_user,
    mock_state,
):
    """get_chart_data raises validation error when query_context is invalid JSON."""
    from superset.exceptions import SupersetValidationException

    chart = MagicMock()
    chart.query_context = "not valid json {"
    mock_chart_dao.find_by_id = AsyncMock(return_value=chart)
    with pytest.raises(SupersetValidationException, match="invalid query context"):
        await _get_chart_data(
            controller,
            request=MagicMock(),
            pk=1,
            dao=mock_chart_dao,
            ds_dao=mock_ds_dao,
            security_manager=mock_security_manager,
            current_user=mock_user,
            state=mock_state,
        )


async def test_get_chart_data_datasource_not_found(
    controller,
    mock_chart_dao,
    mock_ds_dao,
    mock_security_manager,
    mock_user,
    mock_state,
):
    """get_chart_data raises ObjectNotFoundError when datasource is missing."""
    chart = MagicMock()
    chart.query_context = json.dumps(
        {
            "datasource": {"type": "table", "id": 42},
            "queries": [],
        }
    )
    mock_chart_dao.find_by_id = AsyncMock(return_value=chart)
    mock_ds_dao.get_datasource = AsyncMock(return_value=None)
    with pytest.raises(ObjectNotFoundError):
        await _get_chart_data(
            controller,
            request=MagicMock(),
            pk=1,
            dao=mock_chart_dao,
            ds_dao=mock_ds_dao,
            security_manager=mock_security_manager,
            current_user=mock_user,
            state=mock_state,
        )


@patch("superset.controllers.chart.ChartDataCommand")
async def test_get_chart_data_executes_command(
    mock_chart_data_command_cls,
    controller,
    mock_chart_dao,
    mock_ds_dao,
    mock_security_manager,
    mock_user,
    mock_state,
):
    """get_chart_data creates and executes a ChartDataCommand."""
    chart = MagicMock()
    chart.query_context = json.dumps(
        {
            "datasource": {"type": "table", "id": 1},
            "queries": [{"columns": ["col1"]}],
            "force": False,
        }
    )
    mock_chart_dao.find_by_id = AsyncMock(return_value=chart)
    datasource = MagicMock()
    mock_ds_dao.get_datasource = AsyncMock(return_value=datasource)

    mock_cmd = AsyncMock()
    mock_cmd.execute = AsyncMock(return_value={"queries": [{"data": [1]}]})
    mock_chart_data_command_cls.return_value = mock_cmd

    result = await _get_chart_data(
        controller,
        request=MagicMock(),
        pk=1,
        dao=mock_chart_dao,
        ds_dao=mock_ds_dao,
        security_manager=mock_security_manager,
        current_user=mock_user,
        state=mock_state,
    )

    mock_chart_data_command_cls.assert_called_once()
    mock_cmd.execute.assert_awaited_once()
    assert result == {"queries": [{"data": [1]}]}


# ---------------------------------------------------------------------------
# data (POST) tests
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_chart_data_body():
    body = MagicMock()
    body.datasource = MagicMock()
    body.datasource.type = "table"
    body.datasource.id = 1
    body.queries = [MagicMock()]
    body.force = False
    return body


async def test_data_datasource_not_found(
    controller,
    mock_chart_data_body,
    mock_ds_dao,
    mock_security_manager,
    mock_user,
    mock_state,
):
    """POST /data raises ObjectNotFoundError when datasource is missing."""
    mock_ds_dao.get_datasource = AsyncMock(return_value=None)
    with pytest.raises(ObjectNotFoundError):
        await _data(
            controller,
            data=mock_chart_data_body,
            request=MagicMock(),
            ds_dao=mock_ds_dao,
            security_manager=mock_security_manager,
            current_user=mock_user,
            state=mock_state,
        )


@patch("superset.controllers.chart.ChartDataCommand")
async def test_data_executes_command(
    mock_chart_data_command_cls,
    controller,
    mock_chart_data_body,
    mock_ds_dao,
    mock_security_manager,
    mock_user,
    mock_state,
):
    """POST /data creates and executes a ChartDataCommand."""
    datasource = MagicMock()
    mock_ds_dao.get_datasource = AsyncMock(return_value=datasource)

    mock_cmd = AsyncMock()
    mock_cmd.execute = AsyncMock(return_value={"queries": [{"data": [99]}]})
    mock_chart_data_command_cls.return_value = mock_cmd

    result = await _data(
        controller,
        data=mock_chart_data_body,
        request=MagicMock(),
        ds_dao=mock_ds_dao,
        security_manager=mock_security_manager,
        current_user=mock_user,
        state=mock_state,
    )

    mock_chart_data_command_cls.assert_called_once()
    mock_cmd.execute.assert_awaited_once()
    # T2-23: endpoint now returns a Response object for CSV/XLSX support
    from litestar.response import Response as LitestarResponse

    if isinstance(result, LitestarResponse):
        assert result.content == {"queries": [{"data": [99]}]}
    else:
        assert result == {"queries": [{"data": [99]}]}
