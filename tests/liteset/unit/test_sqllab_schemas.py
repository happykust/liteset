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

from liteset.schemas.sqllab import (
    EstimateQueryCostSchema,
    ExecutePayloadSchema,
    FormatSQLSchema,
    QueryExecutionResponse,
    QueryResult,
    SQLLabBootstrap,
    SqlLabPermalinkSchema,
)


def test_estimate_query_cost_body():
    body = msgspec.json.decode(
        b'{"database_id": 1, "sql": "SELECT 1"}',
        type=EstimateQueryCostSchema,
    )
    assert body.database_id == 1
    assert body.sql == "SELECT 1"
    assert body.schema is None
    assert body.template_params == {}


def test_estimate_query_cost_body_missing_required():
    with pytest.raises(msgspec.ValidationError):
        msgspec.json.decode(b'{"database_id": 1}', type=EstimateQueryCostSchema)


def test_format_sql_body():
    body = FormatSQLSchema(sql="SELECT * FROM t")
    assert body.sql == "SELECT * FROM t"
    assert body.engine is None


def test_format_sql_body_with_engine():
    body = FormatSQLSchema(sql="SELECT 1", engine="postgres")
    assert body.engine == "postgres"


def test_execute_payload_body_defaults():
    body = ExecutePayloadSchema(database_id=1, sql="SELECT 1")
    assert body.select_as_cta is False
    assert body.ctas_method == "TABLE"
    assert body.runAsync is False
    assert body.expand_data is True
    assert body.queryLimit is None
    assert body.client_id is None


def test_execute_payload_body_full():
    body = ExecutePayloadSchema(
        database_id=5,
        sql="SELECT * FROM users",
        schema="public",
        catalog="main",
        tab="Tab 1",
        tmp_table_name="tmp_res",
        select_as_cta=True,
        ctas_method="VIEW",
        queryLimit=1000,
        runAsync=True,
        client_id="client-abc",
    )
    assert body.database_id == 5
    assert body.select_as_cta is True
    assert body.ctas_method == "VIEW"
    assert body.queryLimit == 1000


def test_execute_payload_body_json_roundtrip():
    body = ExecutePayloadSchema(database_id=1, sql="SELECT 1")
    encoded = msgspec.json.encode(body)
    decoded = msgspec.json.decode(encoded, type=ExecutePayloadSchema)
    assert decoded.database_id == 1
    assert decoded.sql == "SELECT 1"


def test_query_result_defaults():
    qr = QueryResult()
    assert qr.status == "success"
    assert qr.data == []
    assert qr.columns == []
    assert qr.query_id is None


def test_query_execution_response():
    resp = QueryExecutionResponse(
        status="success",
        query_id=42,
        data=[{"col": 1}],
        columns=[{"name": "col", "type": "INT"}],
    )
    assert resp.query_id == 42
    assert len(resp.data) == 1


def test_sqllab_bootstrap_defaults():
    bs = SQLLabBootstrap()
    assert bs.tab_state_ids == []
    assert bs.databases == {}
    assert bs.queries == {}
    assert bs.user == {}


def test_sqllab_bootstrap_with_data():
    bs = SQLLabBootstrap(
        user={"id": 1, "username": "admin"},
        databases={"1": {"name": "main"}},
    )
    assert bs.user["username"] == "admin"
    assert "1" in bs.databases


def test_sqllab_permalink_body_default():
    body = SqlLabPermalinkSchema()
    assert body.state == {}


def test_sqllab_permalink_body_with_state():
    body = SqlLabPermalinkSchema(state={"sql": "SELECT 1", "dbId": 1})
    assert body.state["sql"] == "SELECT 1"


def test_sqllab_permalink_body_json_roundtrip():
    body = SqlLabPermalinkSchema(state={"key": "value"})
    encoded = msgspec.json.encode(body)
    decoded = msgspec.json.decode(encoded, type=SqlLabPermalinkSchema)
    assert decoded.state == {"key": "value"}
