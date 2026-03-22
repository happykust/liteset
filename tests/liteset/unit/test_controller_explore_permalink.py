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
"""Tests for ExplorePermalinkController."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, PropertyMock

import msgspec
import pytest

from liteset.controllers.explore_permalink import (
    ExplorePermalinkController,
    ExplorePermalinkCreateBody,
)


# ---------------------------------------------------------------------------
# Controller metadata
# ---------------------------------------------------------------------------


def test_controller_path():
    assert ExplorePermalinkController.path == "/api/v1/explore/permalink"


def test_controller_tags():
    assert ExplorePermalinkController.tags == ["Explore Permalink"]


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------


def test_create_body_defaults():
    body = ExplorePermalinkCreateBody()
    assert body.chart_id is None
    assert body.form_data == {}
    assert body.url_params == {}


def test_create_body_with_values():
    body = msgspec.convert(
        {"chart_id": 42, "form_data": {"viz_type": "table"}, "url_params": {"foo": "bar"}},
        ExplorePermalinkCreateBody,
    )
    assert body.chart_id == 42
    assert body.form_data == {"viz_type": "table"}
    assert body.url_params == {"foo": "bar"}


# ---------------------------------------------------------------------------
# Handler logic tests (call underlying fn directly)
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_kv_dao():
    return AsyncMock()


@pytest.fixture
def mock_user():
    user = MagicMock()
    type(user).id = PropertyMock(return_value=1)
    return user


async def test_create_permalink(mock_kv_dao, mock_user):
    data = ExplorePermalinkCreateBody(chart_id=1, form_data={"viz_type": "table"})
    # Access the underlying function via .fn
    create_fn = ExplorePermalinkController.create_permalink.fn
    result = await create_fn(
        None, data=data, kv_dao=mock_kv_dao, current_user=mock_user
    )
    assert "key" in result
    assert "url" in result
    assert result["url"].startswith("/explore/p/")
    mock_kv_dao.set_value.assert_called_once()


async def test_get_permalink_found(mock_kv_dao):
    mock_kv_dao.get_value.return_value = json.dumps({"chart_id": 1})
    get_fn = ExplorePermalinkController.get_permalink.fn
    result = await get_fn(None, key="abc123", kv_dao=mock_kv_dao)
    assert result["result"]["chart_id"] == 1


async def test_get_permalink_not_found(mock_kv_dao):
    from liteset.exceptions import ObjectNotFoundError

    mock_kv_dao.get_value.return_value = None
    get_fn = ExplorePermalinkController.get_permalink.fn
    with pytest.raises(ObjectNotFoundError):
        await get_fn(None, key="missing", kv_dao=mock_kv_dao)
