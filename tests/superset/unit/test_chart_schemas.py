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

import msgspec
import pytest

from superset.schemas.chart import (
    AnnotationLayer,
    ChartDataFilter,
    ChartDataQueryContext,
    ChartDataQueryObject,
    ChartDataResponse,
    ChartExportParams,
    ChartGetResponse,
    ChartListResponse,
    ChartPostSchema,
    ChartPutSchema,
    FavoriteStatusParams,
)


def test_chart_post_body():
    body = msgspec.json.decode(
        b'{"slice_name": "Test", "viz_type": "table",'
        b' "datasource_id": 1, "datasource_type": "table"}',
        type=ChartPostSchema,
    )
    assert body.slice_name == "Test"
    assert body.viz_type == "table"


def test_chart_post_body_invalid_datasource_type():
    with pytest.raises(msgspec.ValidationError):
        msgspec.json.decode(
            b'{"slice_name": "X", "viz_type": "table",'
            b' "datasource_id": 1, "datasource_type": "invalid"}',
            type=ChartPostSchema,
        )


def test_chart_post_body_empty_name():
    """Empty string is rejected by Meta(min_length=1); whitespace-only
    is caught by CreateChartCommand.validate()."""
    with pytest.raises(msgspec.ValidationError):
        msgspec.json.decode(
            b'{"slice_name": "", "viz_type": "table",'
            b' "datasource_id": 1, "datasource_type": "table"}',
            type=ChartPostSchema,
        )


def test_chart_put_body_partial():
    body = msgspec.json.decode(
        b'{"slice_name": "Updated"}',
        type=ChartPutSchema,
    )
    assert body.slice_name == "Updated"
    assert body.viz_type is msgspec.UNSET


def test_chart_get_response_roundtrip():
    resp = ChartGetResponse(id=1, result={"slice_name": "Test", "viz_type": "table"})
    encoded = msgspec.json.encode(resp)
    decoded = msgspec.json.decode(encoded, type=ChartGetResponse)
    assert decoded.id == 1


def test_chart_data_query_context():
    ctx = ChartDataQueryContext(
        datasource={"type": "table", "id": 1},
        queries=[{"columns": ["col1"], "metrics": ["count"]}],
    )
    assert len(ctx.queries) == 1


def test_chart_data_query_object_defaults():
    qo = ChartDataQueryObject()
    assert qo.columns == []
    assert qo.metrics is None
    assert qo.row_limit is None
    assert qo.order_desc is True


def test_chart_list_response():
    resp = ChartListResponse(
        result=[{"id": 1, "slice_name": "A"}],
        count=1,
    )
    assert resp.count == 1
    assert len(resp.result) == 1


def test_chart_export_params():
    p = ChartExportParams(ids=[1, 2, 3])
    assert p.ids == [1, 2, 3]


def test_favorite_status_params():
    p = FavoriteStatusParams(ids=[10, 20])
    assert p.ids == [10, 20]


def test_chart_data_response_empty():
    resp = ChartDataResponse()
    assert resp.result == []


def test_chart_data_query_object_groupby():
    obj = msgspec.json.decode(b'{"groupby": ["col1"]}', type=ChartDataQueryObject)
    assert obj.groupby == ["col1"]


def test_annotation_layer_formula_value():
    layer = msgspec.json.decode(
        b'{"name": "formula", "value": "sin(x)",'
        b' "showLabel": true, "timeColumn": "__timestamp"}',
        type=AnnotationLayer,
    )
    assert layer.value == "sin(x)"
    assert layer.show_label is True


def test_chart_data_filter_adhoc_col():
    f = msgspec.json.decode(
        b'{"col": {"sqlExpression": "UPPER(name)"}, "op": "==", "val": "X"}',
        type=ChartDataFilter,
    )
    assert isinstance(f.col, dict)


def test_chart_data_query_object_backward_compat():
    obj = msgspec.json.decode(b'{"columns": ["c1"]}', type=ChartDataQueryObject)
    assert obj.groupby is None
    assert obj.apply_fetch_values_predicate is False
