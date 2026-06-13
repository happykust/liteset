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
    sm = MagicMock()
    # The POST /data handler enforces datasource access before result_type
    # dispatch (await raise_for_access); make it awaitable + permissive so the
    # happy-path tests reach the command. Denial is covered by the live probe.
    sm.raise_for_access = AsyncMock()
    return sm


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
    # The handler does an access-scoped lookup via find_all (not find_by_id).
    mock_chart_dao.find_all = AsyncMock(return_value=[])
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
    """get_chart_data returns a 400 response when chart has no query_context.

    1:1 with the original ``data(pk)`` view
    (``superset_old/charts/data/api.py:134-139``): a missing/empty
    ``query_context`` collapses to ``json_body is None`` →
    ``response_400("Chart has no query context saved...")`` — a returned 400,
    not a raised exception.
    """
    chart = MagicMock()
    chart.query_context = None
    mock_chart_dao.find_all = AsyncMock(return_value=[chart])
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
    assert result.status_code == 400
    assert "query context" in result.content["message"].lower()


async def test_get_chart_data_invalid_json(
    controller,
    mock_chart_dao,
    mock_ds_dao,
    mock_security_manager,
    mock_user,
    mock_state,
):
    """get_chart_data returns a 400 response when query_context is invalid JSON.

    1:1 with the original: a JSON parse failure also collapses to
    ``json_body is None`` → the same ``response_400`` message.
    """
    chart = MagicMock()
    chart.query_context = "not valid json {"
    mock_chart_dao.find_all = AsyncMock(return_value=[chart])
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
    assert result.status_code == 400
    assert "query context" in result.content["message"].lower()


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
    mock_chart_dao.find_all = AsyncMock(return_value=[chart])
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
    mock_chart_dao.find_all = AsyncMock(return_value=[chart])
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
    # get_chart_data renders the command output via _render_chart_data_payload:
    # a Response carrying ``{"result": [<query>, ...]}`` (1:1 with the original
    # ``_send_chart_response`` JSON branch), not the raw command dict.
    import msgspec as _msgspec

    payload = _msgspec.json.decode(result.content)
    assert payload["result"][0]["data"] == [1]


# ---------------------------------------------------------------------------
# data (POST) tests
# ---------------------------------------------------------------------------


@pytest.fixture
def post_body_bytes() -> bytes:
    """A POST /data JSON body that routes through the default (full) branch.

    The ``data`` handler no longer accepts a typed ``data`` parameter — it
    reads and ``msgspec``-decodes the raw body off ``request`` (1:1 with the
    original ``request.form.get("form_data")`` / JSON dispatch), so tests must
    drive it with real bytes rather than a pre-built struct.
    """
    return json.dumps(
        {
            "datasource": {"id": 1, "type": "table"},
            "queries": [{"columns": ["col1"]}],
            "result_format": "json",
            "result_type": "full",
        }
    ).encode("utf-8")


def _make_post_request(body: bytes) -> MagicMock:
    """Mock a Litestar request exposing a JSON body + content-type."""
    request = MagicMock()
    request.scope = {"headers": []}
    request.content_type = ("application/json",)
    request.body = AsyncMock(return_value=body)
    return request


async def test_data_datasource_not_found(
    controller,
    post_body_bytes,
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
            request=_make_post_request(post_body_bytes),
            ds_dao=mock_ds_dao,
            security_manager=mock_security_manager,
            current_user=mock_user,
            state=mock_state,
        )


@patch("superset.controllers.chart.ChartDataCommand")
async def test_data_executes_command(
    mock_chart_data_command_cls,
    controller,
    post_body_bytes,
    mock_ds_dao,
    mock_security_manager,
    mock_user,
    mock_state,
):
    """POST /data creates and executes a ChartDataCommand."""
    datasource = MagicMock()
    mock_ds_dao.get_datasource = AsyncMock(return_value=datasource)
    # Non-guest user: the response keeps the raw query untouched.
    mock_security_manager.is_guest_user = MagicMock(return_value=False)

    mock_cmd = AsyncMock()
    # Realistic query result: ``data`` is a list of record dicts (the shape
    # ``df.to_dict(orient="records")`` produces), which the handler's
    # NaN/Decimal cleanup pass iterates over.
    mock_cmd.execute = AsyncMock(return_value={"queries": [{"data": [{"value": 99}]}]})
    mock_chart_data_command_cls.return_value = mock_cmd

    result = await _data(
        controller,
        request=_make_post_request(post_body_bytes),
        ds_dao=mock_ds_dao,
        security_manager=mock_security_manager,
        current_user=mock_user,
        state=mock_state,
    )

    mock_chart_data_command_cls.assert_called_once()
    mock_cmd.execute.assert_awaited_once()
    # The default JSON path serializes the command output as a Response
    # carrying ``{"result": [<query>, ...]}``.
    import msgspec as _msgspec

    payload = _msgspec.json.decode(result.content)
    assert payload["result"][0]["data"] == [{"value": 99}]


async def test_data_enforces_datasource_access_before_result_type(
    controller,
    mock_ds_dao,
    mock_security_manager,
    mock_user,
    mock_state,
):
    """Regression: POST /data must enforce datasource access BEFORE the
    result_type dispatch, so the ``result_type=query`` SQL-preview branch
    (which returns before ChartDataCommand.execute) cannot leak generated SQL
    to a user with no datasource access."""
    from superset.errors import ErrorLevel, SupersetError, SupersetErrorType
    from superset.exceptions import SupersetSecurityException

    mock_ds_dao.get_datasource = AsyncMock(return_value=MagicMock())
    mock_security_manager.raise_for_access = AsyncMock(
        side_effect=SupersetSecurityException(
            SupersetError(
                error_type=SupersetErrorType.DATASOURCE_SECURITY_ACCESS_ERROR,
                message="denied",
                level=ErrorLevel.ERROR,
            )
        )
    )
    body = json.dumps(
        {
            "datasource": {"id": 1, "type": "table"},
            "queries": [{"columns": ["col1"]}],
            "result_type": "query",
        }
    ).encode("utf-8")
    with pytest.raises(SupersetSecurityException):
        await _data(
            controller,
            request=_make_post_request(body),
            ds_dao=mock_ds_dao,
            security_manager=mock_security_manager,
            current_user=mock_user,
            state=mock_state,
        )


class TestTableLikeFileResponseVerboseMap:
    """CSV/XLSX exports must apply the datasource verbose_map to column names
    (1:1 with upstream get_data, which renames columns from
    datasource.data['verbose_map'])."""

    def test_csv_export_applies_verbose_map(self) -> None:
        from superset.controllers.chart import _table_like_file_response

        result = {"queries": [{"data": [{"count__col": 5, "plain": "x"}]}]}
        resp = _table_like_file_response(
            result, "csv", verbose_map={"count__col": "Distinct Users"}
        )
        body = (
            resp.content.decode()
            if isinstance(resp.content, bytes)
            else str(resp.content)
        )
        assert "Distinct Users" in body
        assert "count__col" not in body

    def test_csv_export_without_verbose_map_keeps_raw_names(self) -> None:
        from superset.controllers.chart import _table_like_file_response

        result = {"queries": [{"data": [{"count__col": 5}]}]}
        resp = _table_like_file_response(result, "csv")
        body = (
            resp.content.decode()
            if isinstance(resp.content, bytes)
            else str(resp.content)
        )
        assert "count__col" in body
