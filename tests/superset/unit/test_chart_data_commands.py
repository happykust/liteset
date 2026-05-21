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

from superset.commands.chart_data import ChartDataCommand
from superset.common.query_context import AsyncQueryContext
from superset.common.query_context_processor import AsyncQueryContextProcessor
from superset.common.query_object import AsyncQueryObject


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

