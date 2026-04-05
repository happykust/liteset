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

from __future__ import annotations

import json
from typing import Any

import msgspec

from superset.databases.utils import make_url_safe

from superset.schemas.base import ApiListResponse, ApiResponse, ModelStruct

# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class DatabaseSSHTunnel(msgspec.Struct):
    """SSH tunnel configuration for database connections."""

    id: int | None = None
    server_address: str = ""
    server_port: int = 22
    username: str = ""
    password: str | None = None
    private_key: str | None = None
    private_key_password: str | None = None


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
    ssh_tunnel: DatabaseSSHTunnel | None = None
    parameters: dict[str, Any] = {}
    cache_timeout: int | None = None
    expose_in_sqllab: bool = True
    allow_run_async: bool = False
    allow_ctas: bool = False
    allow_cvas: bool = False
    allow_dml: bool = False
    allow_file_upload: bool = False
    force_ctas_schema: str | None = None

    def __post_init__(self) -> None:
        if self.sqlalchemy_uri:
            make_url_safe(self.sqlalchemy_uri)
        if self.masked_encrypted_extra:
            try:
                json.loads(self.masked_encrypted_extra)
            except json.JSONDecodeError as ex:
                raise ValueError(
                    f"encrypted_extra is not valid JSON: {ex}"
                ) from ex


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
    ssh_tunnel: DatabaseSSHTunnel | None | msgspec.UnsetType = msgspec.UNSET
    parameters: dict[str, Any] | None | msgspec.UnsetType = msgspec.UNSET
    cache_timeout: int | None | msgspec.UnsetType = msgspec.UNSET
    expose_in_sqllab: bool | None | msgspec.UnsetType = msgspec.UNSET
    allow_run_async: bool | None | msgspec.UnsetType = msgspec.UNSET
    allow_ctas: bool | None | msgspec.UnsetType = msgspec.UNSET
    allow_cvas: bool | None | msgspec.UnsetType = msgspec.UNSET
    allow_dml: bool | None | msgspec.UnsetType = msgspec.UNSET
    allow_file_upload: bool | None | msgspec.UnsetType = msgspec.UNSET
    force_ctas_schema: str | None | msgspec.UnsetType = msgspec.UNSET

    def __post_init__(self) -> None:
        if isinstance(self.sqlalchemy_uri, str) and self.sqlalchemy_uri:
            make_url_safe(self.sqlalchemy_uri)
        if isinstance(self.masked_encrypted_extra, str) and self.masked_encrypted_extra:
            try:
                json.loads(self.masked_encrypted_extra)
            except json.JSONDecodeError as ex:
                raise ValueError(
                    f"encrypted_extra is not valid JSON: {ex}"
                ) from ex


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
    ssh_tunnel: DatabaseSSHTunnel | None = None
    parameters: dict[str, Any] = {}
    catalog: str | None = None

    def __post_init__(self) -> None:
        if self.sqlalchemy_uri:
            make_url_safe(self.sqlalchemy_uri)
        if self.masked_encrypted_extra:
            try:
                json.loads(self.masked_encrypted_extra)
            except json.JSONDecodeError as ex:
                raise ValueError(
                    f"encrypted_extra is not valid JSON: {ex}"
                ) from ex


class ValidateSQLSchema(msgspec.Struct):
    sql: str
    schema: str | None = None
    catalog: str | None = None
    template_params: dict[str, Any] | None = None


class DatabaseValidateParamsSchema(msgspec.Struct):
    engine: str
    configuration_method: str = "sqlalchemy_form"
    parameters: dict[str, Any] = {}
    database_name: str | None = None
    id: int | None = None
    driver: str | None = None
    catalog: dict[str, Any] | None = None
    impersonate_user: bool = False
    extra: str | None = None
    masked_encrypted_extra: str | None = None
    server_cert: str | None = None


# ---------------------------------------------------------------------------
# Response bodies
# ---------------------------------------------------------------------------


class DatabaseConnectionResponse(msgspec.Struct):
    id: int
    result: dict[str, Any] = {}


class TableMetadataColumn(msgspec.Struct, rename="camel"):
    name: str
    type: str = ""
    long_type: str | None = None
    keys: list[dict[str, Any]] = []
    duplicates_constraint: str | None = None
    is_dttm: bool = False
    comment: str | None = None


class TableMetadataIndex(msgspec.Struct):
    column_names: list[str] = []
    name: str | None = None
    type: str = "index"
    options: dict[str, Any] = {}


class TableMetadataResponse(msgspec.Struct, rename="camel"):
    name: str
    columns: list[TableMetadataColumn] = []
    foreign_keys: list[TableMetadataIndex] = []
    indexes: list[TableMetadataIndex] = []
    primary_key: dict[str, Any] = {}
    select_star: str | None = None
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
    cost_estimate_enabled: bool = False
    allows_virtual_table_explore: bool = True
    cancel_query_on_windows_unload: bool = False
    disable_data_preview: bool = False
    disable_drill_to_detail: bool = False
    allow_multi_catalog: bool = False
    version: str | None = None
    schema_options: dict[str, Any] = {}
    schemas_allowed_for_csv_upload: list[str] = []


class ImportV1Database(msgspec.Struct):
    database_name: str
    sqlalchemy_uri: str
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
    password: str | None = None
    encrypted_extra: str | None = None
    impersonate_user: bool = False
    external_url: str | None = None
    ssh_tunnel: dict[str, Any] | None = None


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
    """Schema for upload_metadata — not used as a request body directly.

    The endpoint accepts multipart/form-data with individual parameters
    (``file``, ``type``, ``delimiter``, ``header_row``).  This struct is
    kept for potential programmatic use / documentation purposes.
    """

    table_name: str
    schema_name: str | None = None


class FileMetadataItem(msgspec.Struct, rename="camel"):
    """Single item in the upload-metadata response."""

    column_names: list[str]
    sheet_name: str | None = None


class FileMetadataResponse(msgspec.Struct, rename="camel"):
    """Response body for POST /upload_metadata/."""

    items: list[FileMetadataItem] = []


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
    supports_dynamic_catalog: bool = False
    supports_oauth2: bool = False


class QualifiedTable(msgspec.Struct):
    catalog: str | None = None
    schema_name: str | None = None
    table_name: str = ""


# ---------------------------------------------------------------------------
# Detail result Struct for GET /{pk}
# ---------------------------------------------------------------------------


class EngineInformationRef(msgspec.Struct, omit_defaults=True):
    """Engine information embedded in database detail response."""

    supports_file_upload: bool = False
    disable_ssh_tunneling: bool = False
    supports_dynamic_catalog: bool = False
    supports_oauth2: bool = False


class DatabaseDetailResult(ModelStruct):
    """Full database detail returned by GET /api/v1/database/{pk}."""

    database_name: str = ""
    backend: str = ""
    expose_in_sqllab: bool = True
    allow_run_async: bool = False
    cache_timeout: int | None = None
    uuid: str | None = None
    configuration_method: str | None = None
    allow_ctas: bool = False
    allow_cvas: bool = False
    allow_dml: bool = False
    allow_file_upload: bool = False
    driver: str | None = None
    force_ctas_schema: str | None = None
    impersonate_user: bool = False
    is_managed_externally: bool = False
    sqlalchemy_uri: str = ""
    extra: str | None = None
    server_cert: str | None = None
    masked_encrypted_extra: str | None = None
    ssh_tunnel: Any = None
    parameters: dict[str, Any] = {}
    engine_information: EngineInformationRef | dict[str, Any] | None = None

    @classmethod
    def _resolve_parameters(cls, obj: Any) -> dict[str, Any]:
        return getattr(obj, "parameters", None) or {}

    @classmethod
    def _resolve_engine_information(
        cls,
        obj: Any,
    ) -> EngineInformationRef | dict[str, Any]:
        raw = getattr(obj, "engine_information", None)
        if raw and isinstance(raw, dict):
            return raw
        return EngineInformationRef(
            supports_file_upload=getattr(obj, "allow_file_upload", False),
        )

    @classmethod
    def from_model(
        cls,
        obj: Any,
        *,
        mask_uri: Any = None,
        **overrides: Any,
    ) -> DatabaseDetailResult:
        """Build from a Database ORM model.

        *mask_uri* should be the ``mask_uri_password`` callable when the
        SQLAlchemy URI should be masked in the response.
        """
        if mask_uri is not None:
            uri = getattr(obj, "sqlalchemy_uri", "")
            overrides["sqlalchemy_uri"] = mask_uri(uri)
        return super().from_model(obj, **overrides)  # type: ignore[return-value]


DatabaseGetResponse = ApiResponse
DatabaseListResponse = ApiListResponse
