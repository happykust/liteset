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
"""Tests for ExploreController."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from superset.controllers.explore import ExploreController


# ---------------------------------------------------------------------------
# Controller metadata
# ---------------------------------------------------------------------------


def test_controller_path():
    assert ExploreController.path == "/api/v1/explore"


def test_controller_tags():
    assert ExploreController.tags == ["Explore"]


# ---------------------------------------------------------------------------
# Handler logic tests (call underlying fn directly)
# ---------------------------------------------------------------------------


async def test_get_explore_empty():
    request = MagicMock()
    request.query_params = {}
    chart_dao = AsyncMock()
    dataset_dao = AsyncMock()
    kv_dao = AsyncMock()

    get_fn = ExploreController.get_explore.fn
    result = await get_fn(
        None,
        request=request,
        chart_dao=chart_dao,
        dataset_dao=dataset_dao,
        kv_dao=kv_dao,
    )
    assert result["result"]["form_data"] == {}
    assert result["result"]["slice"] is None
    assert result["result"]["dataset"] is None
    assert result["result"]["message"] == ""


async def test_get_explore_with_form_data_key():
    request = MagicMock()
    request.query_params = {"form_data_key": "my-key"}
    chart_dao = AsyncMock()
    dataset_dao = AsyncMock()
    kv_dao = AsyncMock()
    kv_dao.get_value.return_value = json.dumps({"viz_type": "bar"})

    get_fn = ExploreController.get_explore.fn
    result = await get_fn(
        None,
        request=request,
        chart_dao=chart_dao,
        dataset_dao=dataset_dao,
        kv_dao=kv_dao,
    )
    assert result["result"]["form_data"] == {"viz_type": "bar"}


async def test_get_explore_with_slice_id():
    request = MagicMock()
    request.query_params = {"slice_id": "10"}
    chart = MagicMock()
    chart.id = 10
    chart.slice_name = "My Chart"
    chart.viz_type = "table"
    chart.params = json.dumps({"granularity": "day"})

    chart_dao = AsyncMock()
    chart_dao.find_by_id.return_value = chart
    dataset_dao = AsyncMock()
    kv_dao = AsyncMock()

    get_fn = ExploreController.get_explore.fn
    result = await get_fn(
        None,
        request=request,
        chart_dao=chart_dao,
        dataset_dao=dataset_dao,
        kv_dao=kv_dao,
    )
    assert result["result"]["slice"]["slice_id"] == 10
    assert result["result"]["form_data"]["granularity"] == "day"


async def test_get_explore_chart_not_found():
    request = MagicMock()
    request.query_params = {"slice_id": "999"}
    chart_dao = AsyncMock()
    chart_dao.find_by_id.return_value = None
    dataset_dao = AsyncMock()
    kv_dao = AsyncMock()

    get_fn = ExploreController.get_explore.fn
    result = await get_fn(
        None,
        request=request,
        chart_dao=chart_dao,
        dataset_dao=dataset_dao,
        kv_dao=kv_dao,
    )
    assert result["result"]["slice"] is None
    assert "not found" in result["result"]["message"]
