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
"""Tests for FilterStateCacheManager and FormDataCacheManager."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from liteset.temporary_cache.filter_state import FilterStateCacheManager
from liteset.temporary_cache.form_data import FormDataCacheManager


@pytest.fixture
def mock_kv_dao() -> AsyncMock:
    dao = AsyncMock()
    dao.get_value = AsyncMock(return_value="stored_value")
    dao.set_value = AsyncMock()
    dao.delete_value = AsyncMock(return_value=True)
    return dao


# ---------------------------------------------------------------------------
# FilterStateCacheManager
# ---------------------------------------------------------------------------


async def test_filter_state_get(mock_kv_dao: AsyncMock) -> None:
    mgr = FilterStateCacheManager(mock_kv_dao)
    result = await mgr.get(resource_id=1, key="abc")
    assert result == "stored_value"
    mock_kv_dao.get_value.assert_awaited_once_with(
        resource="dashboard_filter_state", resource_id=1, key="abc"
    )


async def test_filter_state_create(mock_kv_dao: AsyncMock) -> None:
    mgr = FilterStateCacheManager(mock_kv_dao)
    await mgr.create(resource_id=1, key="abc", value='{"filters": []}')
    mock_kv_dao.set_value.assert_awaited_once_with(
        resource="dashboard_filter_state",
        resource_id=1,
        key="abc",
        value='{"filters": []}',
    )


async def test_filter_state_update(mock_kv_dao: AsyncMock) -> None:
    mgr = FilterStateCacheManager(mock_kv_dao)
    await mgr.update(resource_id=1, key="abc", value='{"filters": [1]}')
    mock_kv_dao.set_value.assert_awaited_once_with(
        resource="dashboard_filter_state",
        resource_id=1,
        key="abc",
        value='{"filters": [1]}',
    )


async def test_filter_state_delete(mock_kv_dao: AsyncMock) -> None:
    mgr = FilterStateCacheManager(mock_kv_dao)
    result = await mgr.delete(resource_id=1, key="abc")
    assert result is True
    mock_kv_dao.delete_value.assert_awaited_once_with(
        resource="dashboard_filter_state", resource_id=1, key="abc"
    )


async def test_filter_state_get_returns_none(mock_kv_dao: AsyncMock) -> None:
    mock_kv_dao.get_value.return_value = None
    mgr = FilterStateCacheManager(mock_kv_dao)
    result = await mgr.get(resource_id=99, key="missing")
    assert result is None


async def test_filter_state_resource_name() -> None:
    mgr = FilterStateCacheManager(AsyncMock())
    assert mgr._resource == "dashboard_filter_state"


# ---------------------------------------------------------------------------
# FormDataCacheManager
# ---------------------------------------------------------------------------


async def test_form_data_get(mock_kv_dao: AsyncMock) -> None:
    mgr = FormDataCacheManager(mock_kv_dao)
    result = await mgr.get(resource_id=5, key="xyz")
    assert result == "stored_value"
    mock_kv_dao.get_value.assert_awaited_once_with(
        resource="explore_form_data", resource_id=5, key="xyz"
    )


async def test_form_data_create(mock_kv_dao: AsyncMock) -> None:
    mgr = FormDataCacheManager(mock_kv_dao)
    await mgr.create(resource_id=5, key="xyz", value='{"viz_type": "bar"}')
    mock_kv_dao.set_value.assert_awaited_once_with(
        resource="explore_form_data",
        resource_id=5,
        key="xyz",
        value='{"viz_type": "bar"}',
    )


async def test_form_data_update(mock_kv_dao: AsyncMock) -> None:
    mgr = FormDataCacheManager(mock_kv_dao)
    await mgr.update(resource_id=5, key="xyz", value='{"viz_type": "line"}')
    mock_kv_dao.set_value.assert_awaited_once_with(
        resource="explore_form_data",
        resource_id=5,
        key="xyz",
        value='{"viz_type": "line"}',
    )


async def test_form_data_delete(mock_kv_dao: AsyncMock) -> None:
    mgr = FormDataCacheManager(mock_kv_dao)
    result = await mgr.delete(resource_id=5, key="xyz")
    assert result is True
    mock_kv_dao.delete_value.assert_awaited_once_with(
        resource="explore_form_data", resource_id=5, key="xyz"
    )


async def test_form_data_delete_returns_false(mock_kv_dao: AsyncMock) -> None:
    mock_kv_dao.delete_value.return_value = False
    mgr = FormDataCacheManager(mock_kv_dao)
    result = await mgr.delete(resource_id=5, key="missing")
    assert result is False


async def test_form_data_resource_name() -> None:
    mgr = FormDataCacheManager(AsyncMock())
    assert mgr._resource == "explore_form_data"
