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

import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import jwt as pyjwt
import pytest
from litestar.exceptions import NotAuthorizedException

from superset.controllers.chart import ChartController

GAQ_SECRET = "test-gaq-secret-at-least-16-chars"
COOKIE_NAME = "async-token"


def _get_raw_method(controller_cls: type, method_name: str) -> Any:
    handler = getattr(controller_cls, method_name)
    return handler.fn if hasattr(handler, "fn") else handler


def _mint_async_token(channel: str, secret: str = GAQ_SECRET) -> str:
    token = pyjwt.encode(
        {"channel": channel, "sub": "1"},
        secret,
        algorithm="HS256",
    )
    return token.decode("ascii") if isinstance(token, bytes) else token


def _cookie_header_bytes(channel: str, secret: str = GAQ_SECRET) -> list[tuple]:
    token = _mint_async_token(channel, secret)
    raw = f"{COOKIE_NAME}={token}"
    return [(b"cookie", raw.encode("utf-8"))]


def _make_request(headers: list[tuple]) -> MagicMock:
    request = MagicMock()
    request.scope = {"headers": headers}
    return request


_get_chart_data = _get_raw_method(ChartController, "get_chart_data")
_data = _get_raw_method(ChartController, "data")


@pytest.fixture
def controller() -> ChartController:
    return ChartController(owner=MagicMock())


@pytest.fixture
def mock_user() -> MagicMock:
    user = MagicMock()
    user.id = 1
    user.is_authenticated = True
    return user


@pytest.fixture
def mock_security_manager() -> MagicMock:
    sm = MagicMock()
    # A bare MagicMock would return a truthy is_guest_user; set it explicitly.
    sm.is_guest_user = MagicMock(return_value=False)
    sm.raise_for_access = AsyncMock()
    return sm


@pytest.fixture
def async_state() -> MagicMock:
    state = MagicMock()
    settings = MagicMock()
    settings.global_async_queries = True
    settings.global_async_queries_jwt_secret = GAQ_SECRET
    settings.global_async_queries_jwt_cookie_name = COOKIE_NAME
    state.settings = settings
    return state


@pytest.fixture
def chart_query_context() -> str:
    return json.dumps(
        {
            "datasource": {"id": 1, "type": "table"},
            "queries": [{"columns": ["col1"]}],
            "result_format": "json",
            "result_type": "full",
        }
    )


@pytest.fixture
def post_body_bytes() -> bytes:
    return json.dumps(
        {
            "datasource": {"id": 1, "type": "table"},
            "queries": [{"columns": ["col1"]}],
            "result_format": "json",
            "result_type": "full",
        }
    ).encode("utf-8")


@patch("superset.tasks.async_queries.load_chart_data_into_cache")
async def test_get_chart_data_async_uses_cookie_channel(
    mock_task: MagicMock,
    controller: ChartController,
    mock_user: MagicMock,
    mock_security_manager: MagicMock,
    async_state: MagicMock,
    chart_query_context: str,
) -> None:
    """GET async submit writes to the cookie's channel, NOT a random uuid."""
    channel = str(uuid.uuid4())
    mock_task.delay = MagicMock()

    chart = MagicMock()
    chart.query_context = chart_query_context
    dao = AsyncMock()
    dao.find_all = AsyncMock(return_value=[chart])

    request = _make_request(_cookie_header_bytes(channel))

    result = await _get_chart_data(
        controller,
        request=request,
        pk=1,
        dao=dao,
        ds_dao=AsyncMock(),
        security_manager=mock_security_manager,
        current_user=mock_user,
        state=async_state,
    )

    mock_task.delay.assert_called_once()
    job_metadata, _form_data = mock_task.delay.call_args.args
    assert job_metadata["channel_id"] == channel

    assert result.status_code == 202
    assert result.content["channel_id"] == channel
    assert set(result.content) == {
        "channel_id",
        "job_id",
        "user_id",
        "status",
        "errors",
        "result_url",
    }
    assert result.content["job_id"] != channel
    assert result.content["status"] == "pending"
    assert result.content["user_id"] == 1


@patch("superset.tasks.async_queries.load_chart_data_into_cache")
async def test_get_chart_data_async_missing_cookie_401(
    mock_task: MagicMock,
    controller: ChartController,
    mock_user: MagicMock,
    mock_security_manager: MagicMock,
    async_state: MagicMock,
    chart_query_context: str,
) -> None:
    """GET async submit returns 401 when the async-token cookie is absent."""
    mock_task.delay = MagicMock()

    chart = MagicMock()
    chart.query_context = chart_query_context
    dao = AsyncMock()
    dao.find_all = AsyncMock(return_value=[chart])

    request = _make_request([])  # no cookie header

    with pytest.raises(NotAuthorizedException):
        await _get_chart_data(
            controller,
            request=request,
            pk=1,
            dao=dao,
            ds_dao=AsyncMock(),
            security_manager=mock_security_manager,
            current_user=mock_user,
            state=async_state,
        )
    mock_task.delay.assert_not_called()


@patch("superset.tasks.async_queries.load_chart_data_into_cache")
async def test_get_chart_data_async_wrong_secret_401(
    mock_task: MagicMock,
    controller: ChartController,
    mock_user: MagicMock,
    mock_security_manager: MagicMock,
    async_state: MagicMock,
    chart_query_context: str,
) -> None:
    """GET async submit returns 401 when the cookie is signed with a bad key."""
    mock_task.delay = MagicMock()

    chart = MagicMock()
    chart.query_context = chart_query_context
    dao = AsyncMock()
    dao.find_all = AsyncMock(return_value=[chart])

    request = _make_request(
        _cookie_header_bytes(
            str(uuid.uuid4()), secret="some-other-wrong-secret-32-bytes-long"
        )
    )

    with pytest.raises(NotAuthorizedException):
        await _get_chart_data(
            controller,
            request=request,
            pk=1,
            dao=dao,
            ds_dao=AsyncMock(),
            security_manager=mock_security_manager,
            current_user=mock_user,
            state=async_state,
        )
    mock_task.delay.assert_not_called()


@patch("superset.tasks.async_queries.load_chart_data_into_cache")
async def test_get_chart_data_async_secretstr_secret(
    mock_task: MagicMock,
    controller: ChartController,
    mock_user: MagicMock,
    mock_security_manager: MagicMock,
    chart_query_context: str,
) -> None:
    """Channel resolution unwraps a ``SecretStr`` GAQ secret (get_secret_value).

    Mirrors the polling endpoint / middleware which both call
    ``get_secret_value`` when the configured secret is a pydantic SecretStr.
    """
    from pydantic import SecretStr

    channel = str(uuid.uuid4())
    mock_task.delay = MagicMock()

    state = MagicMock()
    settings = MagicMock()
    settings.global_async_queries = True
    settings.global_async_queries_jwt_secret = SecretStr(GAQ_SECRET)
    settings.global_async_queries_jwt_cookie_name = COOKIE_NAME
    state.settings = settings

    chart = MagicMock()
    chart.query_context = chart_query_context
    dao = AsyncMock()
    dao.find_all = AsyncMock(return_value=[chart])

    request = _make_request(_cookie_header_bytes(channel))

    result = await _get_chart_data(
        controller,
        request=request,
        pk=1,
        dao=dao,
        ds_dao=AsyncMock(),
        security_manager=mock_security_manager,
        current_user=mock_user,
        state=state,
    )

    mock_task.delay.assert_called_once()
    job_metadata, _form_data = mock_task.delay.call_args.args
    assert job_metadata["channel_id"] == channel
    assert result.content["channel_id"] == channel


def _make_post_request(headers: list[tuple], body: bytes) -> MagicMock:
    request = MagicMock()
    request.scope = {"headers": headers}
    request.content_type = ("application/json",)
    request.body = AsyncMock(return_value=body)
    return request


@patch("superset.tasks.async_queries.load_chart_data_into_cache")
async def test_post_data_async_uses_cookie_channel(
    mock_task: MagicMock,
    controller: ChartController,
    mock_user: MagicMock,
    mock_security_manager: MagicMock,
    async_state: MagicMock,
    post_body_bytes: bytes,
) -> None:
    """POST async submit writes to the cookie's channel, NOT a random uuid."""
    channel = str(uuid.uuid4())
    mock_task.delay = MagicMock()

    request = _make_post_request(_cookie_header_bytes(channel), post_body_bytes)

    result = await _data(
        controller,
        request=request,
        ds_dao=AsyncMock(),
        security_manager=mock_security_manager,
        current_user=mock_user,
        state=async_state,
    )

    mock_task.delay.assert_called_once()
    job_metadata, _form_data = mock_task.delay.call_args.args
    assert job_metadata["channel_id"] == channel

    assert result.status_code == 202
    assert result.content["channel_id"] == channel
    assert set(result.content) == {
        "channel_id",
        "job_id",
        "user_id",
        "status",
        "errors",
        "result_url",
    }
    assert result.content["job_id"] != channel
    assert result.content["status"] == "pending"
    assert result.content["user_id"] == 1


@patch("superset.tasks.async_queries.load_chart_data_into_cache")
async def test_post_data_async_missing_cookie_401(
    mock_task: MagicMock,
    controller: ChartController,
    mock_user: MagicMock,
    mock_security_manager: MagicMock,
    async_state: MagicMock,
    post_body_bytes: bytes,
) -> None:
    """POST async submit returns 401 when the async-token cookie is absent."""
    mock_task.delay = MagicMock()

    request = _make_post_request([], post_body_bytes)

    with pytest.raises(NotAuthorizedException):
        await _data(
            controller,
            request=request,
            ds_dao=AsyncMock(),
            security_manager=mock_security_manager,
            current_user=mock_user,
            state=async_state,
        )
    mock_task.delay.assert_not_called()


@patch("superset.controllers.chart._try_cached_chart_data")
@patch("superset.tasks.async_queries.load_chart_data_into_cache")
async def test_get_chart_data_async_cache_hit_returns_inline(
    mock_task: MagicMock,
    mock_cache_first: MagicMock,
    controller: ChartController,
    mock_user: MagicMock,
    mock_security_manager: MagicMock,
    async_state: MagicMock,
    chart_query_context: str,
) -> None:
    """A cache hit returns the inline payload and skips the Celery dispatch."""
    from litestar import Response

    mock_task.delay = MagicMock()
    sentinel = Response(
        content={"result": [{"data": []}]}, media_type="application/json"
    )
    mock_cache_first.return_value = sentinel

    chart = MagicMock()
    chart.query_context = chart_query_context
    dao = AsyncMock()
    dao.find_all = AsyncMock(return_value=[chart])

    request = _make_request([])

    result = await _get_chart_data(
        controller,
        request=request,
        pk=1,
        dao=dao,
        ds_dao=AsyncMock(),
        security_manager=mock_security_manager,
        current_user=mock_user,
        state=async_state,
    )

    assert result is sentinel
    mock_cache_first.assert_awaited_once()
    mock_task.delay.assert_not_called()


@patch("superset.controllers.chart._try_cached_chart_data")
@patch("superset.tasks.async_queries.load_chart_data_into_cache")
async def test_post_data_async_cache_hit_returns_inline(
    mock_task: MagicMock,
    mock_cache_first: MagicMock,
    controller: ChartController,
    mock_user: MagicMock,
    mock_security_manager: MagicMock,
    async_state: MagicMock,
    post_body_bytes: bytes,
) -> None:
    """POST: a cache hit returns inline and skips the Celery dispatch."""
    from litestar import Response

    mock_task.delay = MagicMock()
    sentinel = Response(
        content={"result": [{"data": []}]}, media_type="application/json"
    )
    mock_cache_first.return_value = sentinel

    request = _make_post_request([], post_body_bytes)

    result = await _data(
        controller,
        request=request,
        ds_dao=AsyncMock(),
        security_manager=mock_security_manager,
        current_user=mock_user,
        state=async_state,
    )

    assert result is sentinel
    mock_cache_first.assert_awaited_once()
    mock_task.delay.assert_not_called()


async def test_submit_channel_matches_polling_reader_channel() -> None:
    from superset.middleware.async_token import parse_channel_id_from_cookie

    channel = str(uuid.uuid4())
    token = _mint_async_token(channel)
    raw_cookie = f"{COOKIE_NAME}={token}"

    reader_channel = parse_channel_id_from_cookie(
        raw_cookie, GAQ_SECRET, cookie_name=COOKIE_NAME
    )
    assert reader_channel == channel
