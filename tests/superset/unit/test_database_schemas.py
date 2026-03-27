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

from superset.schemas.database import (
    DatabasePostSchema,
    DatabasePutSchema,
    DatabaseTestConnectionSchema,
    DatabaseValidateParamsSchema,
    ImportV1Database,
    SchemasResponse,
    TableMetadataColumn,
    TableMetadataIndex,
    TableMetadataResponse,
)


def test_database_post_body():
    body = msgspec.json.decode(
        b'{"database_name": "my_pg", "sqlalchemy_uri": "postgresql://localhost/test"}',
        type=DatabasePostSchema,
    )
    assert body.database_name == "my_pg"
    assert body.sqlalchemy_uri == "postgresql://localhost/test"
    assert body.configuration_method == "sqlalchemy_form"
    assert body.impersonate_user is False
    assert body.parameters == {}
    assert body.ssh_tunnel is None


def test_database_put_body_partial():
    body = msgspec.json.decode(
        b'{"database_name": "renamed"}',
        type=DatabasePutSchema,
    )
    assert body.database_name == "renamed"
    assert body.sqlalchemy_uri is msgspec.UNSET
    assert body.engine is msgspec.UNSET
    assert body.impersonate_user is msgspec.UNSET
    assert body.parameters is msgspec.UNSET


def test_database_put_body_omitted_fields_are_unset():
    """Fields not sent in PUT body should be UNSET, not None."""
    body = msgspec.json.decode(
        b'{"cache_timeout": 60}',
        type=DatabasePutSchema,
    )
    assert body.cache_timeout == 60
    # All other fields must be UNSET, not None
    assert body.database_name is msgspec.UNSET
    assert body.sqlalchemy_uri is msgspec.UNSET
    assert body.engine is msgspec.UNSET
    assert body.configuration_method is msgspec.UNSET
    assert body.impersonate_user is msgspec.UNSET
    assert body.is_managed_externally is msgspec.UNSET
    assert body.expose_in_sqllab is msgspec.UNSET
    assert body.allow_run_async is msgspec.UNSET
    assert body.allow_ctas is msgspec.UNSET
    assert body.allow_cvas is msgspec.UNSET
    assert body.allow_dml is msgspec.UNSET
    assert body.allow_file_upload is msgspec.UNSET


def test_test_connection_body():
    body = msgspec.json.decode(
        b'{"sqlalchemy_uri": "postgresql://localhost/test", "engine": "postgresql"}',
        type=DatabaseTestConnectionSchema,
    )
    assert body.sqlalchemy_uri == "postgresql://localhost/test"
    assert body.engine == "postgresql"
    assert body.configuration_method == "sqlalchemy_form"
    assert body.impersonate_user is False
    assert body.parameters == {}


def test_validate_params_body():
    payload = (
        b'{"engine": "postgresql", "parameters": {"host": "localhost", "port": 5432}}'
    )
    body = msgspec.json.decode(payload, type=DatabaseValidateParamsSchema)
    assert body.engine == "postgresql"
    assert body.parameters == {"host": "localhost", "port": 5432}
    assert body.configuration_method == "sqlalchemy_form"
    assert body.database_name is None


def test_table_metadata_response():
    col = TableMetadataColumn(name="id", type="INTEGER", is_dttm=False)
    idx = TableMetadataIndex(column_names=["id"], name="pk_id", type="unique")
    resp = TableMetadataResponse(
        name="my_table",
        columns=[col],
        indexes=[idx],
        primaryKey={"constrained_columns": ["id"]},
    )
    assert resp.name == "my_table"
    assert len(resp.columns) == 1
    assert resp.columns[0].name == "id"
    assert resp.columns[0].type == "INTEGER"
    assert len(resp.indexes) == 1
    assert resp.indexes[0].name == "pk_id"
    assert resp.foreignKeys == []
    assert resp.selectStar is None


def test_schemas_response():
    resp = msgspec.json.decode(
        b'{"result": ["public", "analytics"]}',
        type=SchemasResponse,
    )
    assert resp.result == ["public", "analytics"]


def test_import_v1_database():
    payload = (
        b'{"database_name": "prod_pg", "sqlalchemy_uri": "postgresql://localhost/prod"}'
    )
    db = msgspec.json.decode(payload, type=ImportV1Database)
    assert db.database_name == "prod_pg"
    assert db.sqlalchemy_uri == "postgresql://localhost/prod"
    assert db.expose_in_sqllab is True
    assert db.allow_run_async is False
    assert db.allow_ctas is False
    assert db.allow_file_upload is False
    assert db.version == "1.0.0"
    assert db.uuid is None
    assert db.is_managed_externally is False
