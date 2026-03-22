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

from unittest.mock import MagicMock

import pytest

from liteset.common.query_context import AsyncQueryContext
from liteset.common.query_object import AsyncQueryObject


@pytest.fixture
def mock_datasource():
    ds = MagicMock()
    ds.uid = "table__1"
    ds.id = 1
    ds.type = "table"
    return ds


def test_query_context_creation(mock_datasource):
    qc = AsyncQueryContext(
        datasource=mock_datasource,
        queries=[
            AsyncQueryObject(datasource={"type": "table", "id": 1}),
        ],
    )
    assert qc.datasource is mock_datasource
    assert len(qc.queries) == 1


def test_query_context_form_data(mock_datasource):
    qc = AsyncQueryContext(
        datasource=mock_datasource,
        queries=[],
        form_data={"viz_type": "table"},
    )
    assert qc.form_data["viz_type"] == "table"


def test_query_context_force_flag(mock_datasource):
    qc = AsyncQueryContext(
        datasource=mock_datasource,
        queries=[],
        force=True,
    )
    assert qc.force is True


def test_query_context_result_type(mock_datasource):
    qc = AsyncQueryContext(
        datasource=mock_datasource,
        queries=[],
        result_type="full",
        result_format="json",
    )
    assert qc.result_type == "full"
    assert qc.result_format == "json"


def test_query_context_default_values(mock_datasource):
    """Optional fields get correct defaults when not provided."""
    qc = AsyncQueryContext(datasource=mock_datasource)
    assert qc.queries == []
    assert qc.form_data == {}
    assert qc.force is False
    assert qc.custom_cache_timeout is None
    assert qc.result_type is None
    assert qc.result_format is None


def test_query_context_multiple_queries(mock_datasource):
    """Context can hold multiple query objects."""
    q1 = AsyncQueryObject(datasource={"type": "table", "id": 1}, columns=["a"])
    q2 = AsyncQueryObject(datasource={"type": "table", "id": 1}, columns=["b"])
    q3 = AsyncQueryObject(datasource={"type": "table", "id": 2}, metrics=["count"])
    qc = AsyncQueryContext(datasource=mock_datasource, queries=[q1, q2, q3])
    assert len(qc.queries) == 3
    assert qc.queries[0].columns == ["a"]
    assert qc.queries[1].columns == ["b"]
    assert qc.queries[2].metrics == ["count"]


def test_query_context_custom_cache_timeout(mock_datasource):
    """custom_cache_timeout is stored and accessible."""
    qc = AsyncQueryContext(
        datasource=mock_datasource,
        queries=[],
        custom_cache_timeout=120,
    )
    assert qc.custom_cache_timeout == 120


def test_query_context_empty_form_data_default(mock_datasource):
    """form_data defaults to empty dict, not None."""
    qc = AsyncQueryContext(datasource=mock_datasource, queries=[])
    assert qc.form_data == {}
    assert isinstance(qc.form_data, dict)
