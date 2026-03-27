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

from liteset.schemas.dataset import (
    DatasetColumnsPut,
    DatasetDrillInfo,
    DatasetDrillResponse,
    DatasetDuplicateSchema,
    DatasetMetricsPut,
    DatasetPostSchema,
    DatasetPutSchema,
    GetOrCreateDatasetSchema,
    ImportV1Column,
    ImportV1Dataset,
    ImportV1Metric,
)


def test_dataset_post_body():
    body = msgspec.json.decode(
        b'{"table_name": "my_table", "database": 1}',
        type=DatasetPostSchema,
    )
    assert body.table_name == "my_table"
    assert body.database == 1
    assert body.schema_name is None
    assert body.sql is None
    assert body.is_managed_externally is False
    assert body.normalize_columns is False
    assert body.always_filter_main_dttm is False
    assert body.tags is None


def test_dataset_put_body_partial():
    body = msgspec.json.decode(
        b'{"table_name": "updated_table"}',
        type=DatasetPutSchema,
    )
    assert body.table_name == "updated_table"
    assert body.database_id is msgspec.UNSET
    assert body.sql is msgspec.UNSET
    assert body.columns is msgspec.UNSET
    assert body.metrics is msgspec.UNSET
    assert body.owners is msgspec.UNSET


def test_dataset_put_body_with_columns():
    payload = msgspec.json.encode(
        {
            "table_name": "t",
            "columns": [
                {"column_name": "id", "type": "INTEGER", "groupby": True},
                {"column_name": "ts", "is_dttm": True},
            ],
            "metrics": [
                {"metric_name": "count", "expression": "COUNT(*)"},
            ],
        }
    )
    body = msgspec.json.decode(payload, type=DatasetPutSchema)
    assert body.table_name == "t"
    assert len(body.columns) == 2
    assert isinstance(body.columns[0], DatasetColumnsPut)
    assert body.columns[0].column_name == "id"
    assert body.columns[0].type == "INTEGER"
    assert body.columns[1].is_dttm is True
    assert len(body.metrics) == 1
    assert isinstance(body.metrics[0], DatasetMetricsPut)
    assert body.metrics[0].metric_name == "count"
    assert body.metrics[0].expression == "COUNT(*)"


def test_dataset_duplicate_body():
    body = DatasetDuplicateSchema(base_model_id=42, table_name="copy_of_table")
    assert body.base_model_id == 42
    assert body.table_name == "copy_of_table"

    roundtrip = msgspec.json.decode(
        msgspec.json.encode(body),
        type=DatasetDuplicateSchema,
    )
    assert roundtrip.base_model_id == 42
    assert roundtrip.table_name == "copy_of_table"


def test_get_or_create_body():
    body = GetOrCreateDatasetSchema(table_name="events", database=3)
    assert body.table_name == "events"
    assert body.database == 3
    assert body.schema_name is None
    assert body.normalize_columns is False
    assert body.always_filter_main_dttm is False

    body_full = GetOrCreateDatasetSchema(
        table_name="events",
        database=3,
        schema_name="public",
        template_params='{"x": 1}',
        normalize_columns=True,
        always_filter_main_dttm=True,
    )
    assert body_full.schema_name == "public"
    assert body_full.normalize_columns is True


def test_import_v1_dataset():
    payload = {
        "table_name": "imported_ds",
        "uuid": "ds-uuid-001",
        "columns": [
            {"column_name": "col1", "is_dttm": False, "groupby": True},
            {"column_name": "col2", "type": "VARCHAR"},
        ],
        "metrics": [
            {"metric_name": "sum_val", "expression": "SUM(val)"},
        ],
        "version": "1.0.0",
        "database_uuid": "abc-123",
    }
    body = msgspec.json.decode(
        msgspec.json.encode(payload),
        type=ImportV1Dataset,
    )
    assert body.table_name == "imported_ds"
    assert len(body.columns) == 2
    assert isinstance(body.columns[0], ImportV1Column)
    assert body.columns[0].column_name == "col1"
    assert body.columns[0].groupby is True
    assert body.columns[1].type == "VARCHAR"
    assert len(body.metrics) == 1
    assert isinstance(body.metrics[0], ImportV1Metric)
    assert body.metrics[0].metric_name == "sum_val"
    assert body.version == "1.0.0"
    assert body.database_uuid == "abc-123"
    assert body.offset == 0
    assert body.filter_select_enabled is True
    assert body.is_managed_externally is False


def test_dataset_drill_response():
    resp = DatasetDrillResponse(
        columns=[
            DatasetDrillInfo(
                column_name="city", groupby=True, is_dttm=False, type="VARCHAR"
            ),
            DatasetDrillInfo(
                column_name="ts", groupby=False, is_dttm=True, type="TIMESTAMP"
            ),
        ]
    )
    assert len(resp.columns) == 2
    assert resp.columns[0].column_name == "city"
    assert resp.columns[0].groupby is True
    assert resp.columns[1].is_dttm is True

    # Roundtrip through JSON
    data = msgspec.json.decode(msgspec.json.encode(resp), type=DatasetDrillResponse)
    assert len(data.columns) == 2
    assert data.columns[1].type == "TIMESTAMP"

    # Default empty
    empty = DatasetDrillResponse()
    assert empty.columns == []
