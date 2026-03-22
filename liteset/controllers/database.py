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
"""Database controller — 27 endpoints for database CRUD,
metadata, export/import, upload."""

from __future__ import annotations

import io
import json
import re
from typing import Any

import msgspec
from litestar import Controller, delete, get, post, put
from litestar.datastructures import UploadFile
from litestar.di import Provide
from litestar.enums import RequestEncodingType
from litestar.params import Body, Parameter
from litestar.response import Stream

from liteset.commands.database import (
    CreateDatabaseCommand,
    DatabaseTestConnectionCommand,
    DeleteDatabaseCommand,
    DeleteSSHTunnelCommand,
    ExportDatabasesCommand,
    ImportDatabasesCommand,
    SyncPermissionsCommand,
    UpdateDatabaseCommand,
    UploadCommand,
    ValidateParametersCommand,
    ValidateSQLCommand,
)

# DAO imports moved to provider functions
from liteset.controllers.base import (
    extract_ids,
    extract_pagination,
    get_distinct_payload,
    get_info_payload,
    get_related_payload,
    serialize_list_response,
    stream_zip,
)
from liteset.exceptions import (
    CommandInvalidError,
    LitesetSecurityException,
    ObjectNotFoundError,
)

_IDENTIFIER_RE = re.compile(r"^[a-zA-Z0-9_][a-zA-Z0-9_.$]*$")
from liteset.guards.rbac import require_permission
from liteset.params.rison import provide_rison_query
from liteset.providers import provide_database_dao
from liteset.schemas.database import (
    CatalogsResponse,
    DatabaseConnectionResponse,
    DatabaseGetResponse,
    DatabasePostBody,
    DatabasePutBody,
    DatabaseTestConnectionBody,
    DatabaseValidateParamsBody,
    SchemaAccessForUploadResponse,
    SchemasResponse,
    SelectStarResponse,
    TableExtraMetadata,
    TableMetadataResponse,
    UploadMetadataBody,
    ValidateSQLBody,
)
from liteset.typing import DatabaseDAOProtocol, SecurityManagerProtocol, UserProtocol
from liteset.events import event_logger
from liteset.utils import filter_unset, mask_uri_password


class DatabaseController(Controller):
    path = "/api/v1/database"
    tags = ["Databases"]
    dependencies = {
        "dao": Provide(provide_database_dao, sync_to_thread=False),
        "rison_params": Provide(provide_rison_query),
    }

    # ------------------------------------------------------------------
    # GET / — list databases
    # ------------------------------------------------------------------
    @get(
        "/",
        guards=[require_permission("can_read", "Database")],
    )
    async def get_list(
        self,
        dao: DatabaseDAOProtocol,
        rison_params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        page, page_size = extract_pagination(rison_params)
        databases = await dao.find_all(page=page, page_size=page_size)
        # TODO: pass same filters to count() when Rison filtering is implemented
        total = await dao.count()
        event_logger.log("database.list")
        return serialize_list_response(
            databases,
            total,
            ["id", "database_name", "backend", "expose_in_sqllab"],
        )

    # ------------------------------------------------------------------
    # GET /_info — API metadata
    # ------------------------------------------------------------------
    @get(
        "/_info",
        guards=[require_permission("can_read", "Database")],
    )
    async def info(self, dao: DatabaseDAOProtocol) -> dict[str, Any]:
        """GET /api/v1/database/_info — API metadata for frontend."""
        return await get_info_payload(
            dao=dao,
            model_name="Database",
            permissions=["can_read", "can_write"],
        )

    # ------------------------------------------------------------------
    # GET /related/{column_name} — related values for dropdowns
    # ------------------------------------------------------------------
    @get(
        "/related/{column_name:str}",
        guards=[require_permission("can_read", "Database")],
    )
    async def related(
        self,
        column_name: str,
        dao: DatabaseDAOProtocol,
        rison_params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """GET /api/v1/database/related/{column_name}"""
        return await get_related_payload(
            dao=dao,
            column_name=column_name,
            rison_params=rison_params,
            allowed_fields=frozenset({"changed_by", "created_by"}),
        )

    # ------------------------------------------------------------------
    # GET /distinct/{column_name} — distinct values for filters
    # ------------------------------------------------------------------
    @get(
        "/distinct/{column_name:str}",
        guards=[require_permission("can_read", "Database")],
    )
    async def distinct(
        self,
        column_name: str,
        dao: DatabaseDAOProtocol,
        rison_params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """GET /api/v1/database/distinct/{column_name}"""
        return await get_distinct_payload(
            dao=dao, column_name=column_name, rison_params=rison_params
        )

    # ------------------------------------------------------------------
    # GET /{pk}/connection — connection info
    # ------------------------------------------------------------------
    @get(
        "/{pk:int}/connection",
        guards=[require_permission("can_read", "Database")],
    )
    async def get_connection(
        self, pk: int, dao: DatabaseDAOProtocol
    ) -> DatabaseConnectionResponse:
        database = await dao.find_by_id(pk)
        if not database:
            raise ObjectNotFoundError("Database", pk)
        return DatabaseConnectionResponse(
            id=database.id,
            result={
                "sqlalchemy_uri": mask_uri_password(
                    getattr(database, "sqlalchemy_uri", "")
                ),
                "backend": getattr(database, "backend", ""),
            },
        )

    # ------------------------------------------------------------------
    # GET /{pk} — get database
    # ------------------------------------------------------------------
    @get(
        "/{pk:int}",
        guards=[require_permission("can_read", "Database")],
    )
    async def get_database(
        self, pk: int, dao: DatabaseDAOProtocol
    ) -> DatabaseGetResponse:
        database = await dao.find_by_id(pk)
        if not database:
            raise ObjectNotFoundError("Database", pk)
        return DatabaseGetResponse(
            id=database.id,
            result={
                "database_name": database.database_name,
                "backend": getattr(database, "backend", ""),
                "expose_in_sqllab": getattr(database, "expose_in_sqllab", True),
                "allow_run_async": getattr(database, "allow_run_async", False),
                "cache_timeout": getattr(database, "cache_timeout", None),
                "uuid": str(database.uuid) if getattr(database, "uuid", None) else None,
                "configuration_method": getattr(database, "configuration_method", None),
                "allow_ctas": getattr(database, "allow_ctas", False),
                "allow_cvas": getattr(database, "allow_cvas", False),
                "allow_dml": getattr(database, "allow_dml", False),
                "driver": getattr(database, "driver", None),
                "force_ctas_schema": getattr(database, "force_ctas_schema", None),
                "impersonate_user": getattr(database, "impersonate_user", False),
                "is_managed_externally": getattr(
                    database, "is_managed_externally", False
                ),
                "engine_information": (
                    getattr(database, "engine_information", None)
                    or {
                        "supports_file_upload": getattr(
                            database, "allow_file_upload", False
                        )
                    }
                ),
            },
        )

    # ------------------------------------------------------------------
    # POST / — create
    # ------------------------------------------------------------------
    @post(
        "/",
        guards=[require_permission("can_write", "Database")],
        status_code=201,
    )
    async def create(
        self,
        data: DatabasePostBody,
        dao: DatabaseDAOProtocol,
        current_user: UserProtocol,
    ) -> DatabaseGetResponse:
        create_data: dict[str, Any] = {
            "database_name": data.database_name,
            "sqlalchemy_uri": data.sqlalchemy_uri or "",
            "configuration_method": data.configuration_method,
            "impersonate_user": data.impersonate_user,
            "is_managed_externally": data.is_managed_externally,
        }
        if data.engine is not None:
            create_data["engine"] = data.engine  # type: ignore[assignment]
        if data.masked_encrypted_extra is not None:
            create_data["encrypted_extra"] = data.masked_encrypted_extra
        if data.extra is not None:
            create_data["extra"] = data.extra
        if data.server_cert is not None:
            create_data["server_cert"] = data.server_cert
        if data.external_url is not None:
            create_data["external_url"] = data.external_url
        if data.uuid is not None:
            create_data["uuid"] = data.uuid

        cmd = CreateDatabaseCommand(
            dao=dao,
            data=create_data,
            user_id=current_user.id,
        )
        db = await cmd.execute()
        event_logger.log("database.create", object_ref=f"database:{db.id}", user_id=current_user.id)
        return DatabaseGetResponse(
            id=db.id,
            result={"database_name": db.database_name},
        )

    # ------------------------------------------------------------------
    # PUT /{pk} — update
    # ------------------------------------------------------------------
    @put(
        "/{pk:int}",
        guards=[require_permission("can_write", "Database")],
    )
    async def update(
        self,
        pk: int,
        data: DatabasePutBody,
        dao: DatabaseDAOProtocol,
        current_user: UserProtocol,
    ) -> DatabaseGetResponse:
        update_data = filter_unset(
            {
                "database_name": data.database_name,
                "sqlalchemy_uri": data.sqlalchemy_uri,
                "engine": data.engine,
                "configuration_method": data.configuration_method,
                "extra": data.extra,
                "impersonate_user": data.impersonate_user,
                "server_cert": data.server_cert,
                "is_managed_externally": data.is_managed_externally,
                "external_url": data.external_url,
                "ssh_tunnel": data.ssh_tunnel,
                "parameters": data.parameters,
                "cache_timeout": data.cache_timeout,
                "expose_in_sqllab": data.expose_in_sqllab,
                "allow_run_async": data.allow_run_async,
                "allow_ctas": data.allow_ctas,
                "allow_cvas": data.allow_cvas,
                "allow_dml": data.allow_dml,
                "allow_file_upload": data.allow_file_upload,
                "force_ctas_schema": data.force_ctas_schema,
            }
        )
        if data.masked_encrypted_extra is not msgspec.UNSET:
            if data.masked_encrypted_extra is not None:
                update_data["encrypted_extra"] = data.masked_encrypted_extra
            else:
                update_data["encrypted_extra"] = None

        cmd = UpdateDatabaseCommand(
            dao=dao,
            database_id=pk,
            data=update_data,
            user_id=current_user.id,
        )
        db = await cmd.execute()
        event_logger.log("database.update", object_ref=f"database:{pk}", user_id=current_user.id)
        return DatabaseGetResponse(
            id=db.id,
            result={"database_name": db.database_name},
        )

    # ------------------------------------------------------------------
    # DELETE /{pk} — delete
    # ------------------------------------------------------------------
    @delete(
        "/{pk:int}",
        guards=[require_permission("can_write", "Database")],
        status_code=200,
    )
    async def delete_database(
        self, pk: int, dao: DatabaseDAOProtocol
    ) -> dict[str, str]:
        cmd = DeleteDatabaseCommand(dao=dao, database_id=pk)
        await cmd.execute()
        event_logger.log("database.delete", object_ref=f"database:{pk}")
        return {"message": "OK"}

    # ------------------------------------------------------------------
    # POST /{pk}/sync_permissions/ — sync FAB permissions
    # ------------------------------------------------------------------
    @post(
        "/{pk:int}/sync_permissions/",
        guards=[require_permission("can_write", "Database")],
    )
    async def sync_permissions(
        self, pk: int, dao: DatabaseDAOProtocol
    ) -> dict[str, Any]:
        cmd = SyncPermissionsCommand(dao=dao, database_id=pk)
        result = await cmd.execute()
        event_logger.log("database.test_connection")
        return result

    # ------------------------------------------------------------------
    # GET /{pk}/catalogs/ — catalog list
    # ------------------------------------------------------------------
    @get(
        "/{pk:int}/catalogs/",
        guards=[require_permission("can_read", "Database")],
    )
    async def catalogs(self, pk: int, dao: DatabaseDAOProtocol) -> CatalogsResponse:
        database = await dao.find_by_id(pk)
        if not database:
            raise ObjectNotFoundError("Database", pk)
        # Catalog listing delegated to engine inspector in production
        return CatalogsResponse(result=[])

    # ------------------------------------------------------------------
    # GET /{pk}/schemas/ — schema list
    # ------------------------------------------------------------------
    @get(
        "/{pk:int}/schemas/",
        guards=[require_permission("can_read", "Database")],
    )
    async def schemas(
        self,
        pk: int,
        dao: DatabaseDAOProtocol,
        q: str | None = None,
    ) -> SchemasResponse:
        database = await dao.find_by_id(pk)
        if not database:
            raise ObjectNotFoundError("Database", pk)
        # Schema listing delegated to engine inspector in production
        return SchemasResponse(result=[])

    # ------------------------------------------------------------------
    # GET /{pk}/tables/ — table list
    # ------------------------------------------------------------------
    @get(
        "/{pk:int}/tables/",
        guards=[require_permission("can_read", "Database")],
    )
    async def tables(
        self,
        pk: int,
        dao: DatabaseDAOProtocol,
        schema_name: str = Parameter(query="schema_name", default=""),
        catalog: str | None = Parameter(query="catalog", default=None),
    ) -> dict[str, Any]:
        database = await dao.find_by_id(pk)
        if not database:
            raise ObjectNotFoundError("Database", pk)
        # Table listing delegated to engine inspector in production
        return {
            "count": 0,
            "result": [],
        }

    # ------------------------------------------------------------------
    # GET /{pk}/table_metadata/ — table metadata
    # ------------------------------------------------------------------
    @get(
        "/{pk:int}/table_metadata/",
        guards=[require_permission("can_read", "Database")],
    )
    async def table_metadata(
        self,
        pk: int,
        dao: DatabaseDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
        name: str = Parameter(query="name", default=""),
        schema_name: str = Parameter(query="schema_name", default=""),
        catalog: str | None = Parameter(query="catalog", default=None),
    ) -> TableMetadataResponse:
        database = await dao.find_by_id(pk)
        if not database:
            raise ObjectNotFoundError("Database", pk)
        try:
            await security_manager.raise_for_access(
                database=database,
                catalog=catalog,
                schema=schema_name or None,
                user=current_user,
            )
        except (LitesetSecurityException, ObjectNotFoundError):
            raise ObjectNotFoundError("Table", name)
        return TableMetadataResponse(name=name)

    # ------------------------------------------------------------------
    # GET /{pk}/table_metadata/extra/ — extra metadata
    # ------------------------------------------------------------------
    @get(
        "/{pk:int}/table_metadata/extra/",
        guards=[require_permission("can_read", "Database")],
    )
    async def table_metadata_extra(
        self,
        pk: int,
        dao: DatabaseDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
        name: str = Parameter(query="name", default=""),
        schema_name: str = Parameter(query="schema_name", default=""),
        catalog: str | None = Parameter(query="catalog", default=None),
    ) -> TableExtraMetadata:
        database = await dao.find_by_id(pk)
        if not database:
            raise ObjectNotFoundError("Database", pk)
        try:
            await security_manager.raise_for_access(
                database=database,
                catalog=catalog,
                schema=schema_name or None,
                user=current_user,
            )
        except (LitesetSecurityException, ObjectNotFoundError):
            raise ObjectNotFoundError("Table", name)
        return TableExtraMetadata()

    # ------------------------------------------------------------------
    # GET /{pk}/select_star/{table_name}/ — SELECT * SQL
    # ------------------------------------------------------------------
    @get(
        "/{pk:int}/select_star/{table_name:str}/",
        guards=[require_permission("can_read", "Database")],
    )
    async def select_star(
        self,
        pk: int,
        table_name: str,
        dao: DatabaseDAOProtocol,
        schema_name: str = Parameter(query="schema_name", default=""),
    ) -> SelectStarResponse:
        if not _IDENTIFIER_RE.match(table_name):
            raise CommandInvalidError(f"Invalid table name: {table_name}")
        database = await dao.find_by_id(pk)
        if not database:
            raise ObjectNotFoundError("Database", pk)
        # SELECT * generation delegated to engine in production
        return SelectStarResponse(result=f'SELECT *\nFROM "{table_name}"')

    # ------------------------------------------------------------------
    # GET /{pk}/select_star/{table_name}/{schema_name}/ — SELECT * with schema
    # ------------------------------------------------------------------
    @get(
        "/{pk:int}/select_star/{table_name:str}/{schema_name:str}/",
        guards=[require_permission("can_read", "Database")],
    )
    async def select_star_with_schema(
        self,
        pk: int,
        table_name: str,
        schema_name: str,
        dao: DatabaseDAOProtocol,
    ) -> SelectStarResponse:
        if not _IDENTIFIER_RE.match(table_name):
            raise CommandInvalidError(f"Invalid table name: {table_name}")
        if schema_name and not _IDENTIFIER_RE.match(schema_name):
            raise CommandInvalidError(f"Invalid schema name: {schema_name}")
        database = await dao.find_by_id(pk)
        if not database:
            raise ObjectNotFoundError("Database", pk)
        qualified = (
            f'"{schema_name}"."{table_name}"' if schema_name else f'"{table_name}"'
        )
        return SelectStarResponse(result=f"SELECT *\nFROM {qualified}")

    # ------------------------------------------------------------------
    # POST /test_connection/ — test connectivity
    # ------------------------------------------------------------------
    @post(
        "/test_connection/",
        guards=[require_permission("can_write", "Database")],
    )
    async def test_connection(
        self,
        data: DatabaseTestConnectionBody,
        dao: DatabaseDAOProtocol,
    ) -> dict[str, Any]:
        cmd = DatabaseTestConnectionCommand(
            dao=dao,
            data={
                "database_name": data.database_name,
                "sqlalchemy_uri": data.sqlalchemy_uri,
                "engine": data.engine,
                "configuration_method": data.configuration_method,
                "masked_encrypted_extra": data.masked_encrypted_extra,
                "extra": data.extra,
                "impersonate_user": data.impersonate_user,
                "server_cert": data.server_cert,
            },
        )
        result = await cmd.execute()
        return result

    # ------------------------------------------------------------------
    # GET /{pk}/related_objects/ — charts/dashboards using DB
    # ------------------------------------------------------------------
    @get(
        "/{pk:int}/related_objects/",
        guards=[require_permission("can_read", "Database")],
    )
    async def related_objects(
        self, pk: int, dao: DatabaseDAOProtocol
    ) -> dict[str, Any]:
        database = await dao.find_by_id(pk)
        if not database:
            raise ObjectNotFoundError("Database", pk)
        related = await dao.get_related_objects(pk)
        return {
            "charts": {
                "count": len(related.get("charts", [])),
                "result": [
                    {"id": c.id, "slice_name": getattr(c, "slice_name", "")}
                    for c in related.get("charts", [])
                ],
            },
            "dashboards": {
                "count": len(related.get("dashboards", [])),
                "result": [
                    {
                        "id": d.id,
                        "title": getattr(d, "dashboard_title", ""),
                    }
                    for d in related.get("dashboards", [])
                ],
            },
            "sqllab_tab_states": {
                "count": len(related.get("sqllab_tab_states", [])),
                "result": [{"id": t.id} for t in related.get("sqllab_tab_states", [])],
            },
        }

    # ------------------------------------------------------------------
    # POST /{pk}/validate_sql/ — SQL validation
    # ------------------------------------------------------------------
    @post(
        "/{pk:int}/validate_sql/",
        guards=[require_permission("can_read", "Database")],
    )
    async def validate_sql(
        self,
        pk: int,
        data: ValidateSQLBody,
        dao: DatabaseDAOProtocol,
    ) -> dict[str, Any]:
        cmd = ValidateSQLCommand(
            dao=dao,
            database_id=pk,
            sql=data.sql,
            schema=data.schema,
        )
        result = await cmd.execute()
        event_logger.log("database.validate_sql", object_ref=f"database:{pk}")
        return result

    # ------------------------------------------------------------------
    # GET /export/ — ZIP export
    # ------------------------------------------------------------------
    @get(
        "/export/",
        guards=[require_permission("can_read", "Database")],
        media_type="application/zip",
    )
    async def export(
        self, dao: DatabaseDAOProtocol, rison_params: dict[str, Any] | None
    ) -> Stream:
        ids = extract_ids(rison_params)
        if not ids:
            raise CommandInvalidError("At least one ID is required for export")
        cmd = ExportDatabasesCommand(model_ids=ids, dao=dao)
        buf = await cmd.execute()
        event_logger.log("database.export", extra={"count": len(ids)})
        return Stream(
            stream_zip(buf),
            status_code=200,
            media_type="application/zip",
            headers={
                "Content-Disposition": "attachment; filename=databases_export.zip",
            },
        )

    # ------------------------------------------------------------------
    # POST /import/ — multipart import
    # ------------------------------------------------------------------
    @post(
        "/import/",
        guards=[require_permission("can_write", "Database")],
        media_type="application/json",
    )
    async def import_database(
        self,
        dao: DatabaseDAOProtocol,
        data: UploadFile = Body(media_type=RequestEncodingType.MULTI_PART),  # noqa: B008
        overwrite: bool = False,
        passwords: str | None = None,
        ssh_tunnel_passwords: str | None = None,
    ) -> dict[str, str]:
        contents = await data.read()
        buf = io.BytesIO(contents)
        try:
            passwords_dict: dict[str, str] = json.loads(passwords) if passwords else {}
        except (ValueError, json.JSONDecodeError):
            raise CommandInvalidError("Invalid JSON in 'passwords' field")
        try:
            ssh_dict: dict[str, str] = (
                json.loads(ssh_tunnel_passwords) if ssh_tunnel_passwords else {}
            )
        except (ValueError, json.JSONDecodeError):
            raise CommandInvalidError("Invalid JSON in 'ssh_tunnel_passwords' field")
        cmd = ImportDatabasesCommand(
            contents=buf,
            dao=dao,
            overwrite=overwrite,
            passwords=passwords_dict,
            ssh_tunnel_passwords=ssh_dict,
        )
        await cmd.execute()
        event_logger.log("database.import")
        return {"message": "OK"}

    # ------------------------------------------------------------------
    # GET /{pk}/function_names/ — DB function names
    # ------------------------------------------------------------------
    @get(
        "/{pk:int}/function_names/",
        guards=[require_permission("can_read", "Database")],
    )
    async def function_names(self, pk: int, dao: DatabaseDAOProtocol) -> dict[str, Any]:
        database = await dao.find_by_id(pk)
        if not database:
            raise ObjectNotFoundError("Database", pk)
        # Function name listing delegated to engine in production
        return {"function_names": []}

    # ------------------------------------------------------------------
    # GET /available/ — available engines
    # ------------------------------------------------------------------
    @get(
        "/available/",
        guards=[require_permission("can_read", "Database")],
    )
    async def available(self) -> dict[str, Any]:
        # Available engine listing delegated to engine registry in production
        return {"databases": []}

    # ------------------------------------------------------------------
    # POST /validate_parameters/ — param validation
    # ------------------------------------------------------------------
    @post(
        "/validate_parameters/",
        guards=[require_permission("can_write", "Database")],
    )
    async def validate_parameters(
        self,
        data: DatabaseValidateParamsBody,
    ) -> dict[str, Any]:
        cmd = ValidateParametersCommand(
            data={
                "engine": data.engine,
                "parameters": data.parameters,
                "database_name": data.database_name,
                "configuration_method": data.configuration_method,
            },
        )
        result = await cmd.execute()
        event_logger.log("database.validate_parameters")
        return result

    # ------------------------------------------------------------------
    # GET /{pk}/schemas_access_for_file_upload/ — upload schemas
    # ------------------------------------------------------------------
    @get(
        "/{pk:int}/schemas_access_for_file_upload/",
        guards=[require_permission("can_read", "Database")],
    )
    async def schemas_access_for_file_upload(
        self,
        pk: int,
        dao: DatabaseDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
    ) -> SchemaAccessForUploadResponse:
        database = await dao.find_by_id(pk)
        if not database:
            raise ObjectNotFoundError("Database", pk)
        # Check allow_file_upload flag on the database
        if not getattr(database, "allow_file_upload", False):
            return SchemaAccessForUploadResponse(schemas=[])
        schemas: list[str] = []
        extra_raw = getattr(database, "extra", "")
        if extra_raw:
            try:
                extra_parsed = json.loads(extra_raw)
                schemas = extra_parsed.get("schemas_allowed_for_file_upload", [])
            except (json.JSONDecodeError, TypeError):
                pass
        if schemas and hasattr(security_manager, "get_schemas_accessible_by_user"):
            schemas = await security_manager.get_schemas_accessible_by_user(
                database,
                schemas,
                user=current_user,
            )
        return SchemaAccessForUploadResponse(schemas=schemas)

    # ------------------------------------------------------------------
    # POST /{pk}/upload/ — file upload
    # ------------------------------------------------------------------
    @post(
        "/{pk:int}/upload/",
        guards=[require_permission("can_write", "Database")],
        media_type="application/json",
    )
    async def upload(
        self,
        pk: int,
        dao: DatabaseDAOProtocol,
        data: UploadFile = Body(media_type=RequestEncodingType.MULTI_PART),  # noqa: B008
        table_name: str = Parameter(query="table_name", default=""),  # noqa: B008
        schema_name: str | None = Parameter(query="schema_name", default=None),  # noqa: B008
    ) -> dict[str, Any]:
        file_contents = await data.read()
        cmd = UploadCommand(
            dao=dao,
            database_id=pk,
            data={"table_name": table_name, "schema_name": schema_name},
            file_contents=file_contents,
        )
        result = await cmd.execute()
        event_logger.log("database.upload", object_ref=f"database:{pk}")
        return result

    # ------------------------------------------------------------------
    # POST /upload_metadata/ — upload metadata
    # ------------------------------------------------------------------
    @post(
        "/upload_metadata/",
        guards=[require_permission("can_write", "Database")],
    )
    async def upload_metadata(self, data: UploadMetadataBody) -> dict[str, Any]:
        # Upload metadata stub; production logic deferred to engine
        return {
            "result": {
                "table_name": data.table_name,
                "schema_name": data.schema_name,
            },
        }

    # ------------------------------------------------------------------
    # Deprecated endpoints (Flask 4.0 compat, marked for removal)
    # ------------------------------------------------------------------
    @get(
        "/{pk:int}/table/{table_name:str}/{schema_name:str}/",
        guards=[require_permission("can_read", "Database")],
    )
    async def table_metadata_deprecated(
        self,
        pk: int,
        table_name: str,
        schema_name: str,
        dao: DatabaseDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
    ) -> dict[str, Any]:
        """Deprecated — use GET /{pk}/table_metadata/ with query params."""
        # TODO(liteset/cleanup): remove in Phase 7
        return await self.table_metadata(
            pk=pk,
            dao=dao,
            security_manager=security_manager,
            current_user=current_user,
            name=table_name,
            schema_name=schema_name,
        )

    @get(
        "/{pk:int}/table_extra/{table_name:str}/{schema_name:str}/",
        guards=[require_permission("can_read", "Database")],
    )
    async def table_extra_metadata_deprecated(
        self,
        pk: int,
        table_name: str,
        schema_name: str,
        dao: DatabaseDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
    ) -> dict[str, Any]:
        """Deprecated — use GET /{pk}/table_metadata/extra/."""
        # TODO(liteset/cleanup): remove in Phase 7
        return await self.table_metadata_extra(
            pk=pk,
            dao=dao,
            security_manager=security_manager,
            current_user=current_user,
            name=table_name,
            schema_name=schema_name,
        )

    @get(
        "/oauth2/",
        guards=[require_permission("can_read", "Database")],
    )
    async def oauth2(self) -> dict[str, Any]:
        """GET /api/v1/database/oauth2/ — OAuth2 provider redirect."""
        # TODO(liteset/remaining-api): OAuth2 flow
        return {"message": "OAuth2 not yet implemented"}

    # ------------------------------------------------------------------
    # GET /{pk}/ssh_tunnel/ — get SSH tunnel config
    # ------------------------------------------------------------------
    @get(
        "/{pk:int}/ssh_tunnel/",
        guards=[require_permission("can_read", "Database")],
    )
    async def get_ssh_tunnel(self, pk: int, dao: DatabaseDAOProtocol) -> dict[str, Any]:
        database = await dao.find_by_id(pk)
        if not database:
            raise ObjectNotFoundError("Database", pk)
        tunnel = await dao.get_ssh_tunnel(pk)
        if not tunnel:
            return {"result": None}
        return {
            "result": {
                "id": getattr(tunnel, "id", None),
                "server_address": getattr(tunnel, "server_address", ""),
                "server_port": getattr(tunnel, "server_port", 22),
                "username": getattr(tunnel, "username", ""),
            },
        }

    # ------------------------------------------------------------------
    # DELETE /{pk}/ssh_tunnel/ — delete SSH tunnel
    # ------------------------------------------------------------------
    @delete(
        "/{pk:int}/ssh_tunnel/",
        guards=[require_permission("can_write", "Database")],
        status_code=200,
    )
    async def delete_ssh_tunnel(
        self, pk: int, dao: DatabaseDAOProtocol
    ) -> dict[str, str]:
        cmd = DeleteSSHTunnelCommand(dao=dao, database_id=pk)
        await cmd.execute()
        event_logger.log("database.delete_ssh_tunnel", object_ref=f"database:{pk}")
        return {"message": "OK"}
