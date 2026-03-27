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
"""msgspec Structs for the Database API — replaces Marshmallow schemas."""

# ruff: noqa: N815  — camelCase field names required for JSON API contract parity
from __future__ import annotations

from typing import Any

import msgspec

from liteset.schemas.base import ApiListResponse, ApiResponse

# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class DatabasePostSchema(msgspec.Struct):
    database_name: str
    sqlalchemy_uri: str | None = None
    engine: str | None = None
    driver: str | None = None
    configuration_method: str = "sqlalchemy_form"
    masked_encrypted_extra: str | None = None
    extra: str | None = None
    impersonate_user: bool = False
    server_cert: str | None = None
    is_managed_externally: bool = False
    external_url: str | None = None
    uuid: str | None = None
    ssh_tunnel: dict[str, Any] | None = None
    parameters: dict[str, Any] = {}
    cache_timeout: int | None = None
    expose_in_sqllab: bool = True
    allow_run_async: bool = False
    allow_ctas: bool = False
    allow_cvas: bool = False
    allow_dml: bool = False
    allow_file_upload: bool = False
    force_ctas_schema: str | None = None


class DatabasePutSchema(msgspec.Struct):
    database_name: str | None | msgspec.UnsetType = msgspec.UNSET
    sqlalchemy_uri: str | None | msgspec.UnsetType = msgspec.UNSET
    engine: str | None | msgspec.UnsetType = msgspec.UNSET
    driver: str | None | msgspec.UnsetType = msgspec.UNSET
    configuration_method: str | None | msgspec.UnsetType = msgspec.UNSET
    masked_encrypted_extra: str | None | msgspec.UnsetType = msgspec.UNSET
    extra: str | None | msgspec.UnsetType = msgspec.UNSET
    impersonate_user: bool | None | msgspec.UnsetType = msgspec.UNSET
    server_cert: str | None | msgspec.UnsetType = msgspec.UNSET
    is_managed_externally: bool | None | msgspec.UnsetType = msgspec.UNSET
    external_url: str | None | msgspec.UnsetType = msgspec.UNSET
    ssh_tunnel: dict[str, Any] | None | msgspec.UnsetType = msgspec.UNSET
    parameters: dict[str, Any] | None | msgspec.UnsetType = msgspec.UNSET
    cache_timeout: int | None | msgspec.UnsetType = msgspec.UNSET
    expose_in_sqllab: bool | None | msgspec.UnsetType = msgspec.UNSET
    allow_run_async: bool | None | msgspec.UnsetType = msgspec.UNSET
    allow_ctas: bool | None | msgspec.UnsetType = msgspec.UNSET
    allow_cvas: bool | None | msgspec.UnsetType = msgspec.UNSET
    allow_dml: bool | None | msgspec.UnsetType = msgspec.UNSET
    allow_file_upload: bool | None | msgspec.UnsetType = msgspec.UNSET
    force_ctas_schema: str | None | msgspec.UnsetType = msgspec.UNSET


class DatabaseTestConnectionSchema(msgspec.Struct):
    database_name: str | None = None
    sqlalchemy_uri: str | None = None
    engine: str | None = None
    driver: str | None = None
    configuration_method: str = "sqlalchemy_form"
    masked_encrypted_extra: str | None = None
    extra: str | None = None
    impersonate_user: bool = False
    server_cert: str | None = None
    ssh_tunnel: dict[str, Any] | None = None
    parameters: dict[str, Any] = {}


class ValidateSQLSchema(msgspec.Struct):
    sql: str
    schema: str | None = None
    catalog: str | None = None


class DatabaseValidateParamsSchema(msgspec.Struct):
    engine: str
    parameters: dict[str, Any] = {}
    database_name: str | None = None
    configuration_method: str = "sqlalchemy_form"


# ---------------------------------------------------------------------------
# Response bodies
# ---------------------------------------------------------------------------


class DatabaseConnectionResponse(msgspec.Struct):
    id: int
    result: dict[str, Any] = {}


class TableMetadataColumn(msgspec.Struct):
    name: str
    type: str = ""
    longType: str | None = None
    keys: list[dict[str, Any]] = []
    duplicates_constraint: str | None = None
    is_dttm: bool = False
    comment: str | None = None


class TableMetadataIndex(msgspec.Struct):
    column_names: list[str] = []
    name: str | None = None
    type: str = "index"
    options: dict[str, Any] = {}


class TableMetadataResponse(msgspec.Struct):
    name: str
    columns: list[TableMetadataColumn] = []
    foreignKeys: list[TableMetadataIndex] = []
    indexes: list[TableMetadataIndex] = []
    primaryKey: dict[str, Any] = {}
    selectStar: str | None = None
    comment: str | None = None


class TableExtraMetadata(msgspec.Struct):
    metadata: dict[str, Any] = {}
    partitions: dict[str, Any] = {}
    clustering: dict[str, Any] = {}


class SelectStarResponse(msgspec.Struct):
    result: str = ""


class SchemasResponse(msgspec.Struct):
    result: list[str] = []


class CatalogsResponse(msgspec.Struct):
    result: list[str] = []


# ---------------------------------------------------------------------------
# Import / Export
# ---------------------------------------------------------------------------


class ImportV1DatabaseExtra(msgspec.Struct):
    metadata_params: dict[str, Any] = {}
    engine_params: dict[str, Any] = {}
    metadata_cache_timeout: dict[str, int] = {}
    schemas_allowed_for_file_upload: list[str] = []


class ImportV1Database(msgspec.Struct):
    database_name: str
    sqlalchemy_uri: str = ""
    cache_timeout: int | None = None
    expose_in_sqllab: bool = True
    allow_run_async: bool = False
    allow_ctas: bool = False
    allow_cvas: bool = False
    allow_dml: bool = False
    allow_file_upload: bool = False
    extra: ImportV1DatabaseExtra = msgspec.field(default_factory=ImportV1DatabaseExtra)
    uuid: str | None = None
    version: str = "1.0.0"
    is_managed_externally: bool = False


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


class UploadSchema(msgspec.Struct):
    table_name: str
    schema_name: str | None = None
    delimiter: str = ","
    already_exists: str = "fail"
    sheet_name: str | None = None
    column_data_types: str | None = None
    index_label: str | None = None
    header_row: int = 0


class UploadMetadataSchema(msgspec.Struct):
    table_name: str
    schema_name: str | None = None


# ---------------------------------------------------------------------------
# Misc responses
# ---------------------------------------------------------------------------


class OAuth2ProviderResponse(msgspec.Struct):
    id: int
    name: str


class SchemaAccessForUploadResponse(msgspec.Struct):
    schemas: list[str] = []


class EngineInformation(msgspec.Struct):
    supports_file_upload: bool = False
    disable_ssh_tunneling: bool = False


class QualifiedTable(msgspec.Struct):
    catalog: str | None = None
    schema_name: str | None = None
    table_name: str = ""


DatabaseGetResponse = ApiResponse
DatabaseListResponse = ApiListResponse
