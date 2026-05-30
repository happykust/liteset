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

from types import SimpleNamespace

import msgspec

from superset.schemas.query import (
    ImportV1SavedQuery,
    QueryDatabaseInfo,
    QueryResponse,
    SavedQueryDetailResult,
    StopQuerySchema,
)


def test_saved_query_detail_includes_id() -> None:
    """``id`` is in upstream show_columns -> must appear inside ``result``.

    The Saved Queries preview modal spreads ``json.result`` and needs the id.
    """
    obj = SimpleNamespace(
        id=7,
        label="my query",
        schema="public",
        sql="SELECT 1",
        db_id=3,
        description=None,
        template_parameters=None,
        catalog=None,
        changed_on=None,
        changed_by=None,
        created_by=None,
        database=None,
    )
    result = SavedQueryDetailResult.from_model(obj)
    assert result.id == 7
    assert result.label == "my query"


def test_query_response_defaults():
    qr = QueryResponse()
    assert qr.id is None
    assert qr.progress == 0
    assert qr.database == {}
    assert qr.status is None


def test_query_response_with_fields():
    qr = QueryResponse(
        id=42,
        status="success",
        sql="SELECT 1",
        rows=10,
        progress=100,
    )
    assert qr.id == 42
    assert qr.rows == 10


def test_query_response_json_roundtrip():
    qr = QueryResponse(id=1, status="running", sql="SELECT *")
    encoded = msgspec.json.encode(qr)
    decoded = msgspec.json.decode(encoded, type=QueryResponse)
    assert decoded.id == 1
    assert decoded.status == "running"


def test_stop_query_body():
    body = msgspec.json.decode(b'{"client_id": "abc-123"}', type=StopQuerySchema)
    assert body.client_id == "abc-123"


def test_stop_query_body_missing_client_id():
    import pytest

    with pytest.raises(msgspec.ValidationError):
        msgspec.json.decode(b"{}", type=StopQuerySchema)


def test_query_database_info():
    info = QueryDatabaseInfo(id=5, database_name="my_db")
    assert info.id == 5
    assert info.database_name == "my_db"


def test_query_database_info_default_name():
    info = QueryDatabaseInfo(id=1)
    assert info.database_name == ""


def test_import_v1_saved_query_defaults():
    """Required fields must be provided; verify defaults for optional fields."""
    sq = ImportV1SavedQuery(
        sql="SELECT 1",
        uuid="sq-uuid-001",
        version="1.0.0",
        database_uuid="db-uuid-001",
    )
    assert sq.label == ""
    assert sq.sql == "SELECT 1"
    assert sq.version == "1.0.0"
    assert sq.uuid == "sq-uuid-001"
    assert sq.database_uuid == "db-uuid-001"
    assert sq.schema is None
    assert sq.description is None
    assert sq.catalog is None


def test_import_v1_saved_query_full():
    sq = ImportV1SavedQuery(
        sql="SELECT * FROM users",
        uuid="abc-def",
        version="1.0.0",
        database_uuid="db-uuid-1",
        schema="public",
        label="My Query",
    )
    assert sq.label == "My Query"
    assert sq.schema == "public"


def test_import_v1_saved_query_json_roundtrip():
    sq = ImportV1SavedQuery(
        sql="SELECT 1",
        uuid="sq-uuid-rt",
        version="1.0.0",
        database_uuid="db-uuid-rt",
        label="Test",
    )
    encoded = msgspec.json.encode(sq)
    decoded = msgspec.json.decode(encoded, type=ImportV1SavedQuery)
    assert decoded.label == "Test"
    assert decoded.sql == "SELECT 1"
