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
    # On the empty-state path the handler fills form_data with explore
    # defaults (datasource/adhoc_filters/applied_time_extras/url_params) and
    # returns a "[Missing Dataset]" placeholder, matching original
    # GetExploreCommand.
    form_data = result["result"]["form_data"]
    assert form_data["adhoc_filters"] == []
    assert form_data["url_params"] == {}
    assert result["result"]["slice"] is None
    assert result["result"]["dataset"]["name"] == "[Missing Dataset]"
    assert result["result"]["message"] is None


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
    # The handler applies upstream transforms (convert_legacy_filters_into_adhoc
    # / merge_extra_filters / merge_request_params) on top of the loaded
    # form_data, so assert the relevant value rather than an exact dict.
    assert result["result"]["form_data"]["viz_type"] == "bar"


async def test_get_explore_with_slice_id():
    request = MagicMock()
    request.query_params = {"slice_id": "10"}
    chart = MagicMock()
    chart.id = 10
    chart.slice_name = "My Chart"
    chart.viz_type = "table"
    chart.params = json.dumps({"granularity": "day"})
    # ``Slice.form_data`` is a real property on the model; with a MagicMock we
    # supply the migrated form_data dict the handler merges into the response.
    chart.form_data = {"granularity": "day"}
    # Iterable relationships eagerly read by the handler.
    chart.owners = []
    chart.dashboards = []
    chart.created_by = None
    chart.changed_by = None
    chart.changed_on = None
    chart.created_on = None
    chart.datasource_id = None

    chart_dao = AsyncMock()
    # The controller resolves the slice via find_by_id_with_options (eager-load),
    # not find_by_id.
    chart_dao.find_by_id_with_options.return_value = chart
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
    # The controller resolves the slice via find_by_id_with_options.
    chart_dao.find_by_id_with_options.return_value = None
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
