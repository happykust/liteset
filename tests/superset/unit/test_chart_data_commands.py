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

from superset.commands.chart_data import ChartDataCommand, GetCachedChartDataCommand
from superset.common.query_context import AsyncQueryContext
from superset.common.query_context_processor import AsyncQueryContextProcessor
from superset.common.query_object import AsyncQueryObject
from superset.exceptions import ForbiddenError


@pytest.fixture
def mock_processor():
    proc = AsyncMock(spec=AsyncQueryContextProcessor)
    proc.raise_for_access = AsyncMock()
    proc.get_payload = AsyncMock(return_value={"queries": [{"data": [1, 2]}]})
    return proc


@pytest.fixture
def query_context():
    ds = MagicMock()
    return AsyncQueryContext(
        datasource=ds,
        queries=[AsyncQueryObject(datasource={"type": "table", "id": 1})],
        force=False,
    )


async def test_chart_data_command_validates(mock_processor, query_context):
    cmd = ChartDataCommand(query_context=query_context, processor=mock_processor)
    await cmd.validate()
    mock_processor.raise_for_access.assert_awaited_once()


async def test_chart_data_command_runs(mock_processor, query_context):
    cmd = ChartDataCommand(query_context=query_context, processor=mock_processor)
    result = await cmd.execute()
    assert "queries" in result
    assert len(result["queries"]) == 1


async def test_get_cached_data_cache_miss():
    cmd = GetCachedChartDataCommand(cache_key="test-key", cache_manager=None)
    result = await cmd.execute()
    assert result is None


async def test_get_cached_data_cache_hit():
    cached = {"data": [1, 2, 3], "datasource_id": 1, "datasource_type": "table"}
    cache_manager = AsyncMock()
    cache_manager.get = AsyncMock(return_value=cached)
    security_manager = MagicMock()
    security_manager.raise_for_access = AsyncMock()
    security_manager.is_guest_user = MagicMock(return_value=False)
    datasource_dao = AsyncMock()
    datasource_dao.find_by_id_and_type = AsyncMock(return_value=MagicMock())
    cmd = GetCachedChartDataCommand(
        cache_key="test-key",
        cache_manager=cache_manager,
        security_manager=security_manager,
        datasource_dao=datasource_dao,
    )
    result = await cmd.execute()
    assert result is not None
    assert result["result"] == [cached]


async def test_get_cached_data_empty_key():
    cmd = GetCachedChartDataCommand(cache_key="", cache_manager=None)
    with pytest.raises(Exception, match="cache_key"):
        await cmd.validate()


# ---------------------------------------------------------------------------
# NEW-T8: GetCachedChartDataCommand — cache exception and "result" key
# ---------------------------------------------------------------------------


async def test_get_cached_data_cache_exception_returns_none():
    """Cache exception returns None instead of propagating."""
    cache_manager = MagicMock()
    cache_manager.get = MagicMock(side_effect=RuntimeError("connection refused"))
    security_manager = MagicMock()
    cmd = GetCachedChartDataCommand(
        cache_key="test-key",
        cache_manager=cache_manager,
        security_manager=security_manager,
    )
    result = await cmd.execute()
    assert result is None


async def test_get_cached_data_with_result_key():
    """Cached data that already has a 'result' key is returned as-is."""
    payload = {
        "result": [{"data": [1, 2, 3]}],
        "datasource_id": 1,
        "datasource_type": "table",
    }
    cache_manager = AsyncMock()
    cache_manager.get = AsyncMock(return_value=payload)
    security_manager = MagicMock()
    security_manager.raise_for_access = AsyncMock()
    security_manager.is_guest_user = MagicMock(return_value=False)
    datasource_dao = AsyncMock()
    datasource_dao.find_by_id_and_type = AsyncMock(return_value=MagicMock())
    cmd = GetCachedChartDataCommand(
        cache_key="test-key",
        cache_manager=cache_manager,
        security_manager=security_manager,
        datasource_dao=datasource_dao,
    )
    result = await cmd.execute()
    assert result == payload
    assert result["result"] == [{"data": [1, 2, 3]}]


async def test_get_cached_data_with_data_key():
    """Cached data with 'data' key (legacy) is wrapped into result list."""
    cached = {"data": [10, 20], "datasource_id": 1, "datasource_type": "table"}
    cache_manager = AsyncMock()
    cache_manager.get = AsyncMock(return_value=cached)
    security_manager = MagicMock()
    security_manager.raise_for_access = AsyncMock()
    security_manager.is_guest_user = MagicMock(return_value=False)
    datasource_dao = AsyncMock()
    datasource_dao.find_by_id_and_type = AsyncMock(return_value=MagicMock())
    cmd = GetCachedChartDataCommand(
        cache_key="test-key",
        cache_manager=cache_manager,
        security_manager=security_manager,
        datasource_dao=datasource_dao,
    )
    result = await cmd.execute()
    assert result is not None
    assert result["result"] == [cached]


async def test_get_cached_data_plain_dict():
    """Cached dict without 'result' or 'data' keys but with datasource is wrapped."""
    cached = {"col1": [1], "col2": [2], "datasource_id": 1, "datasource_type": "table"}
    cache_manager = AsyncMock()
    cache_manager.get = AsyncMock(return_value=cached)
    security_manager = MagicMock()
    security_manager.raise_for_access = AsyncMock()
    security_manager.is_guest_user = MagicMock(return_value=False)
    datasource_dao = AsyncMock()
    datasource_dao.find_by_id_and_type = AsyncMock(return_value=MagicMock())
    cmd = GetCachedChartDataCommand(
        cache_key="test-key",
        cache_manager=cache_manager,
        security_manager=security_manager,
        datasource_dao=datasource_dao,
    )
    result = await cmd.execute()
    assert result is not None
    assert result["result"] == [cached]


async def test_get_cached_data_non_dict():
    """Cached non-dict value is denied (fail-closed — no datasource metadata)."""
    cache_manager = AsyncMock()
    cache_manager.get = AsyncMock(return_value=[[1, 2], [3, 4]])
    security_manager = MagicMock()
    cmd = GetCachedChartDataCommand(
        cache_key="test-key",
        cache_manager=cache_manager,
        security_manager=security_manager,
    )
    with pytest.raises(ForbiddenError):
        await cmd.execute()


async def test_get_cached_data_no_security_manager_denied():
    """Cached data without security_manager is denied (fail-closed)."""
    cache_manager = AsyncMock()
    cache_manager.get = AsyncMock(return_value={"data": [1, 2]})
    cmd = GetCachedChartDataCommand(cache_key="test-key", cache_manager=cache_manager)
    with pytest.raises(ForbiddenError):
        await cmd.execute()
