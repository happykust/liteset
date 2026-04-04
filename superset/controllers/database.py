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
import logging
import re
from typing import Any, cast, TYPE_CHECKING

import msgspec
from litestar import Controller, delete, get, post, put
from litestar.datastructures import UploadFile
from litestar.di import Provide
from litestar.enums import RequestEncodingType
from litestar.params import Body, Parameter
from litestar.response import Response, Stream

from superset.commands.database import (
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
from superset.controllers.base import (
    build_export_headers,
    build_rison_query_params,
    extract_ids,
    get_distinct_payload,
    get_info_payload,
    get_related_payload,
    serialize_list_response,
    stream_zip,
)
from superset.events import event_logger
from superset.exceptions import (
    CommandInvalidError,
    ObjectNotFoundError,
    SupersetSecurityException,
)
from superset.guards.rbac import require_permission
from superset.params.rison import provide_rison_query
from superset.providers import provide_database_dao
from superset.schemas.database import (
    CatalogsResponse,
    DatabaseConnectionResponse,
    DatabaseDetailResult,
    DatabaseGetResponse,
    DatabasePostSchema,
    DatabasePutSchema,
    DatabaseTestConnectionSchema,
    DatabaseValidateParamsSchema,
    SchemaAccessForUploadResponse,
    SchemasResponse,
    SelectStarResponse,
    TableExtraMetadata,
    TableMetadataColumn,
    TableMetadataIndex,
    TableMetadataResponse,
    UploadMetadataSchema,
    ValidateSQLSchema,
)
from superset.typing import DatabaseDAOProtocol, SecurityManagerProtocol, UserProtocol
from superset.utils import filter_none, filter_unset, mask_uri_password
from superset.utils.database import get_async_connection

if TYPE_CHECKING:
    from superset.db.daos.database import AsyncDatabaseDAO

_log = logging.getLogger(__name__)

_IDENTIFIER_RE = re.compile(r"^[a-zA-Z0-9_][a-zA-Z0-9_.$]*$")


# ---------------------------------------------------------------------------
# Sync helpers executed inside ``conn.run_sync`` for Inspector-based work
# ---------------------------------------------------------------------------


def _get_col_type(col: dict[str, Any]) -> str:
    """Stringify a column type, handling broken ``__str__`` impls."""
    try:
        dtype = f"{col['type']}"
    except Exception:  # noqa: BLE001
        dtype = col["type"].__class__.__name__
    return dtype


def _inspect_table_metadata(sync_conn: Any, table_name: str, schema: str | None) -> dict[str, Any]:
    """Run all Inspector calls synchronously and return the raw metadata dict.

    This function is designed to be called via ``async_conn.run_sync()``.
    It mirrors the original ``superset_old/databases/utils.py:get_table_metadata``
    logic exactly.
    """
    from sqlalchemy import inspect as sa_inspect, select, text

    inspector = sa_inspect(sync_conn)

    # --- columns -----------------------------------------------------------
    try:
        raw_columns = inspector.get_columns(table_name, schema=schema)
    except Exception:  # noqa: BLE001
        raw_columns = []

    # --- primary key -------------------------------------------------------
    try:
        pk_constraint = inspector.get_pk_constraint(table_name, schema=schema) or {}
    except Exception:  # noqa: BLE001
        pk_constraint = {}

    primary_key: dict[str, Any] = {}
    if pk_constraint and pk_constraint.get("constrained_columns"):
        primary_key = dict(pk_constraint)
        primary_key["column_names"] = primary_key.pop("constrained_columns")
        primary_key["type"] = "pk"

    # --- foreign keys ------------------------------------------------------
    try:
        raw_fks = inspector.get_foreign_keys(table_name, schema=schema)
    except Exception:  # noqa: BLE001
        raw_fks = []

    foreign_keys: list[dict[str, Any]] = []
    for fk in raw_fks:
        fk_entry = dict(fk)
        fk_entry["column_names"] = fk_entry.pop("constrained_columns", [])
        fk_entry["type"] = "fk"
        foreign_keys.append(fk_entry)

    # --- indexes -----------------------------------------------------------
    try:
        raw_indexes = inspector.get_indexes(table_name, schema=schema)
    except Exception:  # noqa: BLE001
        raw_indexes = []

    indexes: list[dict[str, Any]] = []
    for idx in raw_indexes:
        idx_entry = dict(idx)
        idx_entry["type"] = "index"
        indexes.append(idx_entry)

    # Aggregate keys list: pk + fks + indexes (matches original exactly)
    keys: list[dict[str, Any]] = []
    if primary_key:
        keys.append(primary_key)
    keys += foreign_keys + indexes

    # --- table comment -----------------------------------------------------
    table_comment: str | None = None
    try:
        comment_result = inspector.get_table_comment(table_name, schema=schema)
        if isinstance(comment_result, dict):
            table_comment = comment_result.get("text")
    except (NotImplementedError, Exception):  # noqa: BLE001
        pass

    # --- columns payload ---------------------------------------------------
    columns_payload: list[dict[str, Any]] = []
    for col in raw_columns:
        dtype = _get_col_type(col)
        col_name = col.get("name", col.get("column_name", ""))
        columns_payload.append({
            "name": col_name,
            "type": dtype.split("(")[0] if "(" in dtype else dtype,
            "longType": dtype,
            "keys": [
                k for k in keys if col_name in k.get("column_names", [])
            ],
            "comment": col.get("comment"),
        })

    # --- select star -------------------------------------------------------
    # Generate SELECT * using the connection's dialect for proper quoting.
    dialect = sync_conn.dialect
    quoted_table = dialect.identifier_preparer.quote_identifier(table_name)
    if schema:
        quoted_schema = dialect.identifier_preparer.quote_identifier(schema)
        full_name = f"{quoted_schema}.{quoted_table}"
    else:
        full_name = quoted_table

    qry = select(text("*")).select_from(text(full_name)).limit(100)
    select_star_sql = str(
        qry.compile(dialect=dialect, compile_kwargs={"literal_binds": True})
    )

    return {
        "name": table_name,
        "columns": columns_payload,
        "selectStar": select_star_sql,
        "primaryKey": primary_key,
        "foreignKeys": foreign_keys,
        "indexes": keys,
        "comment": table_comment,
    }


def _inspect_table_extra_metadata(
    sync_conn: Any,
    table_name: str,
    schema: str | None,
) -> dict[str, Any]:
    """Run extra metadata inspection synchronously.

    Designed to be called via ``async_conn.run_sync()``.
    Mirrors ``db_engine_spec.get_extra_table_metadata`` base implementation
    which returns empty dicts by default (engine-specific overrides may
    provide partitions, clustering info, etc.).
    """
    from sqlalchemy import inspect as sa_inspect

    result: dict[str, Any] = {
        "metadata": {},
        "partitions": {},
        "clustering": {},
    }

    inspector = sa_inspect(sync_conn)

    # Attempt to gather partition/clustering info from table comment or
    # options if the dialect supports it.  The base engine spec in the
    # original Superset simply returns ``{}`` — we replicate that and let
    # callers layer on engine-specific enrichment later.
    try:
        comment = inspector.get_table_comment(table_name, schema=schema)
        if isinstance(comment, dict) and comment.get("text"):
            result["metadata"]["comment"] = comment["text"]
    except (NotImplementedError, Exception):  # noqa: BLE001
        pass

    return result


def _build_database_result(db: Any) -> DatabaseDetailResult:
    """Build a full database result from a Database model instance.

    Used by create, update, and GET /{pk} to return a consistent,
    expanded response matching Superset's original API contract.
    """
    return DatabaseDetailResult.from_model(db, mask_uri=mask_uri_password)


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
        from sqlalchemy.orm import selectinload

        from superset.models.core import Database

        rison_filters, order_by, page, page_size = build_rison_query_params(
            Database,
            rison_params,
        )
        if not order_by:
            order_by = [Database.changed_on.desc()]

        databases = await dao.find_all(
            filters=rison_filters or None,
            page=page,
            page_size=page_size,
            order_by=order_by,
            options=[
                selectinload(Database.changed_by),
                selectinload(Database.created_by),
            ],
        )
        total = await dao.count(filters=rison_filters or None)
        event_logger.log("database.list")
        payload = serialize_list_response(
            databases,
            total,
            [
                "id",
                "uuid",
                "database_name",
                "backend",
                "expose_in_sqllab",
                "allow_run_async",
                "allow_file_upload",
                "allow_ctas",
                "allow_cvas",
                "allow_dml",
                "force_ctas_schema",
                "extra",
                "configuration_method",
                "is_managed_externally",
                "changed_on",
                "changed_on_delta_humanized",
                "changed_on_utc",
                "changed_by.first_name",
                "changed_by.last_name",
                "created_by.first_name",
                "created_by.last_name",
            ],
        )
        # Post-process: add computed columns that normally come from
        # engine_spec but are needed by the frontend to avoid undefined.
        for row in payload.get("result", []):
            allow_upload = row.get("allow_file_upload", False)
            row["allows_cost_estimate"] = False
            row["allows_subquery"] = True
            row["allows_virtual_table_explore"] = True
            row["explore_database_id"] = row.get("id")
            row["disable_data_preview"] = False
            row["disable_drill_to_detail"] = False
            row["allow_multi_catalog"] = False
            row["engine_information"] = {
                "supports_file_upload": bool(allow_upload),
                "disable_ssh_tunneling": False,
                "supports_dynamic_catalog": False,
                "supports_oauth2": False,
            }
        return payload

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
                "allow_ctas": getattr(database, "allow_ctas", False),
                "allow_cvas": getattr(database, "allow_cvas", False),
                "allow_dml": getattr(database, "allow_dml", False),
                "allow_run_async": getattr(database, "allow_run_async", False),
                "allow_file_upload": getattr(database, "allow_file_upload", False),
                "cache_timeout": getattr(database, "cache_timeout", None),
                "configuration_method": getattr(database, "configuration_method", None),
                "database_name": getattr(database, "database_name", ""),
                "driver": getattr(database, "driver", None),
                "expose_in_sqllab": getattr(database, "expose_in_sqllab", True),
                "extra": getattr(database, "extra", None),
                "force_ctas_schema": getattr(database, "force_ctas_schema", None),
                "impersonate_user": getattr(database, "impersonate_user", False),
                "is_managed_externally": getattr(
                    database, "is_managed_externally", False
                ),
                "masked_encrypted_extra": getattr(
                    database, "masked_encrypted_extra", None
                ),
                "parameters": getattr(database, "parameters", None) or {},
                "server_cert": getattr(database, "server_cert", None),
                "ssh_tunnel": getattr(database, "ssh_tunnel", None),
                "uuid": (
                    str(database.uuid) if getattr(database, "uuid", None) else None
                ),
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
            result=_build_database_result(database),
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
        data: DatabasePostSchema,
        dao: DatabaseDAOProtocol,
        current_user: UserProtocol,
    ) -> DatabaseGetResponse:
        create_data: dict[str, Any] = filter_none(
            {
                "database_name": data.database_name,
                "sqlalchemy_uri": data.sqlalchemy_uri or "",
                "configuration_method": data.configuration_method,
                "impersonate_user": data.impersonate_user,
                "is_managed_externally": data.is_managed_externally,
                "engine": data.engine,
                "driver": data.driver,
                "extra": data.extra,
                "server_cert": data.server_cert,
                "external_url": data.external_url,
                "uuid": data.uuid,
                "ssh_tunnel": data.ssh_tunnel,
                "parameters": data.parameters or None,
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
        if data.masked_encrypted_extra is not None:
            create_data["encrypted_extra"] = data.masked_encrypted_extra

        cmd = CreateDatabaseCommand(
            dao=cast("AsyncDatabaseDAO", dao),
            data=create_data,
            user_id=current_user.id,
        )
        db = await cmd.execute()
        db_id = int(db.id)
        event_logger.log(
            "database.create",
            object_ref=f"database:{db_id}",
            user_id=current_user.id,
        )
        return DatabaseGetResponse(
            id=db_id,
            result=_build_database_result(db),
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
        data: DatabasePutSchema,
        dao: DatabaseDAOProtocol,
        current_user: UserProtocol,
    ) -> DatabaseGetResponse:
        update_data = filter_unset(
            {
                "database_name": data.database_name,
                "sqlalchemy_uri": data.sqlalchemy_uri,
                "engine": data.engine,
                "driver": data.driver,
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
            dao=cast("AsyncDatabaseDAO", dao),
            database_id=pk,
            data=update_data,
            user_id=current_user.id,
        )
        db = await cmd.execute()
        event_logger.log(
            "database.update",
            object_ref=f"database:{pk}",
            user_id=current_user.id,
        )
        return DatabaseGetResponse(
            id=int(db.id),
            result=_build_database_result(db),
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
        cmd = DeleteDatabaseCommand(dao=cast("AsyncDatabaseDAO", dao), database_id=pk)
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
        cmd = SyncPermissionsCommand(dao=cast("AsyncDatabaseDAO", dao), database_id=pk)
        result = await cmd.execute()
        event_logger.log("database.sync_permissions")
        return result

    # ------------------------------------------------------------------
    # GET /{pk}/catalogs/ — catalog list
    # ------------------------------------------------------------------
    @get(
        "/{pk:int}/catalogs/",
        guards=[require_permission("can_read", "Database")],
    )
    async def catalogs(
        self,
        pk: int,
        dao: DatabaseDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
        q: str | None = None,
    ) -> CatalogsResponse:
        # Parse ``force`` from RISON query param ``q`` for API parity.
        # Async path always fetches live metadata, so ``force`` is a no-op.
        if q:
            try:
                _parsed = json.loads(q)
                _ = bool(_parsed.get("force", False))
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
        database = await dao.find_by_id(pk)
        if not database:
            raise ObjectNotFoundError("Database", pk)
        try:
            async with get_async_connection(database) as (conn, engine_spec):
                catalog_names = await engine_spec.get_catalog_names(conn)
            catalogs_list = await security_manager.get_catalogs_accessible_by_user(
                database,
                sorted(catalog_names),
                user=current_user,
            )
            return CatalogsResponse(result=catalogs_list)
        except Exception as exc:
            _log.warning("Failed to fetch catalogs for database %s: %s", pk, exc)
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
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
        q: str | None = None,
    ) -> SchemasResponse:
        database = await dao.find_by_id(pk)
        if not database:
            raise ObjectNotFoundError("Database", pk)
        catalog: str | None = None
        force: bool = False
        upload_allowed: bool = False
        if q:
            try:
                rison_parsed = json.loads(q)
                catalog = rison_parsed.get("catalog")
                force = bool(rison_parsed.get("force", False))
                upload_allowed = bool(rison_parsed.get("upload_allowed", False))
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
        # ``force`` is accepted for API parity (cache bypass); async path
        # always fetches live metadata so it is a no-op here.
        _ = force
        try:
            async with get_async_connection(database) as (conn, engine_spec):
                schema_names = await engine_spec.get_schema_names(conn, catalog=catalog)
            schemas_list = await security_manager.get_schemas_accessible_by_user(
                database,
                sorted(schema_names),
                catalog=catalog,
                user=current_user,
            )
            # Filter to only schemas that allow file upload
            if upload_allowed:
                allowed_schemas: set[str] = set()
                extra_raw = getattr(database, "extra", "")
                if extra_raw:
                    try:
                        extra_parsed = json.loads(extra_raw)
                        allowed_schemas = set(
                            extra_parsed.get("schemas_allowed_for_file_upload", [])
                        )
                    except (json.JSONDecodeError, TypeError):
                        pass
                if allowed_schemas:
                    schemas_list = [s for s in schemas_list if s in allowed_schemas]
            return SchemasResponse(result=schemas_list)
        except Exception as exc:
            _log.warning("Failed to fetch schemas for database %s: %s", pk, exc)
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
        force: bool = Parameter(query="force", default=False),
    ) -> dict[str, Any]:
        # ``force`` accepted for API parity (cache bypass); async path
        # always fetches live metadata so it is a no-op.
        _ = force
        database = await dao.find_by_id(pk)
        if not database:
            raise ObjectNotFoundError("Database", pk)
        schema = schema_name or None
        try:
            async with get_async_connection(database) as (conn, engine_spec):
                table_names = await engine_spec.get_table_names(conn, schema=schema)
                view_names = await engine_spec.get_view_names(conn, schema=schema)
            # Batch-fetch extra (certification info) from SqlaTable for
            # all discovered tables/views so the frontend gets it.
            all_names = set(table_names) | set(view_names)
            extra_lookup: dict[str, dict[str, Any]] = {}
            if all_names:
                from sqlalchemy import select as sa_select

                from superset.models.connectors import SqlaTable

                stmt = sa_select(SqlaTable.table_name, SqlaTable.extra).where(
                    SqlaTable.database_id == pk,
                    SqlaTable.table_name.in_(all_names),
                )
                if schema:
                    stmt = stmt.where(SqlaTable.schema == schema)
                rows = (await dao.session.execute(stmt)).all()
                for tbl_name, extra_raw in rows:
                    if extra_raw:
                        try:
                            extra_lookup[tbl_name] = json.loads(extra_raw)
                        except (json.JSONDecodeError, TypeError):
                            pass

            options: list[dict[str, Any]] = sorted(
                [
                    {
                        "value": str(t),
                        "type": "table",
                        "extra": extra_lookup.get(str(t), {}),
                    }
                    for t in table_names
                ]
                + [
                    {
                        "value": str(v),
                        "type": "view",
                        "extra": extra_lookup.get(str(v), {}),
                    }
                    for v in view_names
                ],
                key=lambda item: str(item["value"]),
            )
            return {
                "count": len(options),
                "result": options,
            }
        except Exception as exc:
            _log.warning("Failed to fetch tables for database %s: %s", pk, exc)
            return {"count": 0, "result": []}

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
        schema: str = Parameter(query="schema", default=""),
        catalog: str | None = Parameter(query="catalog", default=None),
    ) -> TableMetadataResponse:
        # Accept both ``schema`` (original Superset) and ``schema_name`` (alias)
        effective_schema = schema_name or schema or None
        database = await dao.find_by_id(pk)
        if not database:
            raise ObjectNotFoundError("Database", pk)
        try:
            await security_manager.raise_for_access(
                database=database,
                catalog=catalog,
                schema=effective_schema,
                user=current_user,
            )
        except (SupersetSecurityException, ObjectNotFoundError) as exc:
            raise ObjectNotFoundError("Table", name) from exc

        if not name:
            raise CommandInvalidError("Missing required parameter: name")

        try:
            async with get_async_connection(database) as (conn, _engine_spec):
                raw = await conn.run_sync(
                    _inspect_table_metadata,
                    name,
                    effective_schema,
                )
        except Exception as exc:
            _log.warning(
                "Failed to fetch table metadata for %s.%s on database %s: %s",
                effective_schema,
                name,
                pk,
                exc,
            )
            raise CommandInvalidError(
                f"Error fetching metadata for table '{name}': {exc}"
            ) from exc

        # Convert raw dicts into typed response structs
        columns = [
            TableMetadataColumn(
                name=c["name"],
                type=c.get("type", ""),
                long_type=c.get("longType"),
                keys=c.get("keys", []),
                comment=c.get("comment"),
            )
            for c in raw.get("columns", [])
        ]
        foreign_keys = [
            TableMetadataIndex(
                column_names=fk.get("column_names", []),
                name=fk.get("name"),
                type=fk.get("type", "fk"),
                options={
                    k: v
                    for k, v in fk.items()
                    if k not in ("column_names", "name", "type")
                },
            )
            for fk in raw.get("foreignKeys", [])
        ]
        indexes = [
            TableMetadataIndex(
                column_names=idx.get("column_names", []),
                name=idx.get("name"),
                type=idx.get("type", "index"),
                options={
                    k: v
                    for k, v in idx.items()
                    if k not in ("column_names", "name", "type")
                },
            )
            for idx in raw.get("indexes", [])
        ]

        return TableMetadataResponse(
            name=raw.get("name", name),
            columns=columns,
            foreign_keys=foreign_keys,
            indexes=indexes,
            primary_key=raw.get("primaryKey", {}),
            select_star=raw.get("selectStar"),
            comment=raw.get("comment"),
        )

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
        schema: str = Parameter(query="schema", default=""),
        catalog: str | None = Parameter(query="catalog", default=None),
    ) -> TableExtraMetadata:
        # Accept both ``schema`` (original Superset) and ``schema_name`` (alias)
        effective_schema = schema_name or schema or None
        database = await dao.find_by_id(pk)
        if not database:
            raise ObjectNotFoundError("Database", pk)
        try:
            await security_manager.raise_for_access(
                database=database,
                catalog=catalog,
                schema=effective_schema,
                user=current_user,
            )
        except (SupersetSecurityException, ObjectNotFoundError) as exc:
            raise ObjectNotFoundError("Table", name) from exc

        if not name:
            raise CommandInvalidError("Missing required parameter: name")

        try:
            async with get_async_connection(database) as (conn, _engine_spec):
                raw = await conn.run_sync(
                    _inspect_table_extra_metadata,
                    name,
                    effective_schema,
                )
        except Exception as exc:
            _log.warning(
                "Failed to fetch extra table metadata for %s.%s on database %s: %s",
                effective_schema,
                name,
                pk,
                exc,
            )
            # Return empty metadata on failure (matches original behaviour)
            return TableExtraMetadata()

        return TableExtraMetadata(
            metadata=raw.get("metadata", {}),
            partitions=raw.get("partitions", {}),
            clustering=raw.get("clustering", {}),
        )

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
        data: DatabaseTestConnectionSchema,
        dao: DatabaseDAOProtocol,
    ) -> dict[str, Any]:
        cmd = DatabaseTestConnectionCommand(
            dao=cast("AsyncDatabaseDAO", dao),
            data={
                "database_name": data.database_name,
                "sqlalchemy_uri": data.sqlalchemy_uri,
                "engine": data.engine,
                "driver": data.driver,
                "configuration_method": data.configuration_method,
                "masked_encrypted_extra": data.masked_encrypted_extra,
                "extra": data.extra,
                "impersonate_user": data.impersonate_user,
                "server_cert": data.server_cert,
                "ssh_tunnel": (
                    msgspec.structs.asdict(data.ssh_tunnel)
                    if data.ssh_tunnel is not None
                    else None
                ),
                "parameters": data.parameters,
                "catalog": data.catalog,
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
                    {
                        "id": c.id,
                        "slice_name": getattr(c, "slice_name", ""),
                        "viz_type": getattr(c, "viz_type", None),
                    }
                    for c in related.get("charts", [])
                ],
            },
            "dashboards": {
                "count": len(related.get("dashboards", [])),
                "result": [
                    {
                        "id": d.id,
                        "title": getattr(d, "dashboard_title", ""),
                        "json_metadata": getattr(d, "json_metadata", None),
                        "slug": getattr(d, "slug", None),
                    }
                    for d in related.get("dashboards", [])
                ],
            },
            "sqllab_tab_states": {
                "count": len(related.get("sqllab_tab_states", [])),
                "result": [
                    {
                        "id": t.id,
                        "label": getattr(t, "label", None),
                        "active": getattr(t, "active", None),
                    }
                    for t in related.get("sqllab_tab_states", [])
                ],
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
        data: ValidateSQLSchema,
        dao: DatabaseDAOProtocol,
    ) -> dict[str, Any]:
        cmd = ValidateSQLCommand(
            dao=cast("AsyncDatabaseDAO", dao),
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
        self,
        dao: DatabaseDAOProtocol,
        rison_params: dict[str, Any] | None,
        token: str | None = Parameter(query="token", default=None),
    ) -> Stream:
        ids = extract_ids(rison_params)
        if not ids:
            raise CommandInvalidError("At least one ID is required for export")
        cmd = ExportDatabasesCommand(model_ids=ids, dao=cast("AsyncDatabaseDAO", dao))
        buf = await cmd.execute()
        event_logger.log("database.export", extra={"count": len(ids)})
        return Stream(
            stream_zip(buf),
            status_code=200,
            media_type="application/zip",
            headers=build_export_headers("databases_export.zip", token=token),
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
        except (ValueError, json.JSONDecodeError) as exc:
            raise CommandInvalidError("Invalid JSON in 'passwords' field") from exc
        try:
            ssh_dict: dict[str, str] = (
                json.loads(ssh_tunnel_passwords) if ssh_tunnel_passwords else {}
            )
        except (ValueError, json.JSONDecodeError) as exc:
            raise CommandInvalidError(
                "Invalid JSON in 'ssh_tunnel_passwords' field"
            ) from exc
        cmd = ImportDatabasesCommand(
            contents=buf,
            dao=cast("AsyncDatabaseDAO", dao),
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
        from superset.db.engine_specs import _get_sync_spec_map, _NATIVE_SPECS

        databases: list[dict[str, Any]] = []

        # 1. Native async engine specs
        for engine_key, spec_cls in sorted(_NATIVE_SPECS.items()):
            databases.append(
                {
                    "name": getattr(spec_cls, "engine_name", engine_key),
                    "engine": engine_key,
                    "preferred": True,
                    "available_drivers": [
                        getattr(spec_cls, "default_driver", "") or engine_key
                    ],
                    "default_driver": getattr(spec_cls, "default_driver", ""),
                    "sqlalchemy_uri_placeholder": (
                        f"{engine_key}+{getattr(spec_cls, 'default_driver', '')}://"
                    ),
                    "parameters": {
                        "properties": {
                            "host": {"type": "string"},
                            "port": {"type": "integer"},
                            "username": {"type": "string"},
                            "password": {"type": "string"},
                            "database": {"type": "string"},
                        },
                        "required": ["host", "database"],
                    },
                    "engine_information": {
                        "supports_file_upload": False,
                        "disable_ssh_tunneling": False,
                        "supports_dynamic_catalog": getattr(
                            spec_cls, "supports_dynamic_catalog", False
                        ),
                        "supports_oauth2": getattr(spec_cls, "supports_oauth2", False),
                    },
                }
            )

        # 2. Sync fallback engine specs (from superset.db_engine_specs)
        native_engines = set(_NATIVE_SPECS.keys())
        sync_specs = _get_sync_spec_map()
        for engine_key, spec_cls in sorted(sync_specs.items()):
            if engine_key in native_engines:
                continue
            engine_name = getattr(spec_cls, "engine_name", engine_key)
            default_driver = getattr(spec_cls, "default_driver", "")
            databases.append(
                {
                    "name": engine_name,
                    "engine": engine_key,
                    "preferred": False,
                    "available_drivers": [default_driver or engine_key],
                    "default_driver": default_driver,
                    "sqlalchemy_uri_placeholder": (
                        f"{engine_key}+{default_driver}://"
                        if default_driver
                        else f"{engine_key}://"
                    ),
                    "parameters": {
                        "properties": {
                            "host": {"type": "string"},
                            "port": {"type": "integer"},
                            "username": {"type": "string"},
                            "password": {"type": "string"},
                            "database": {"type": "string"},
                        },
                        "required": ["host", "database"],
                    },
                    "engine_information": {
                        "supports_file_upload": getattr(
                            spec_cls, "supports_file_upload", False
                        ),
                        "disable_ssh_tunneling": getattr(
                            spec_cls, "disable_ssh_tunneling", False
                        ),
                        "supports_dynamic_catalog": getattr(
                            spec_cls, "supports_dynamic_catalog", False
                        ),
                        "supports_oauth2": getattr(spec_cls, "supports_oauth2", False),
                    },
                }
            )

        return {"databases": databases}

    # ------------------------------------------------------------------
    # POST /validate_parameters/ — param validation
    # ------------------------------------------------------------------
    @post(
        "/validate_parameters/",
        guards=[require_permission("can_write", "Database")],
    )
    async def validate_parameters(
        self,
        data: DatabaseValidateParamsSchema,
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
            dao=cast("AsyncDatabaseDAO", dao),
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
    async def upload_metadata(self, data: UploadMetadataSchema) -> dict[str, Any]:
        # Upload metadata stub; production logic deferred to engine
        return {"result": {}}

    @get(
        "/oauth2/",
        guards=[require_permission("can_read", "Database")],
    )
    async def oauth2(
        self,
        dao: DatabaseDAOProtocol,
        oauth_state: str = Parameter(query="state", default=""),
        code: str = Parameter(query="code", default=""),
    ) -> "Response[Any]":
        """GET /api/v1/database/oauth2/ — OAuth2 provider redirect.

        Decodes the ``state`` parameter to recover the originating
        ``database_id`` and ``tab_id``, looks up the database, and
        returns a self-closing HTML page that posts a message back to
        the opener window with the authorization code.
        """
        if not oauth_state:
            return Response(
                content={
                    "message": (
                        "OAuth2 endpoint. Provide 'state' and 'code' query parameters."
                    )
                },
                status_code=200,
            )

        from superset.utils.oauth2 import decode_oauth2_state

        try:
            decoded = decode_oauth2_state(oauth_state)
        except ValueError:
            return Response(
                content="<html><body>Invalid OAuth2 state</body></html>",
                status_code=400,
                media_type="text/html",
            )

        database_id: Any = decoded.get("database_id")
        tab_id = decoded.get("tab_id", "")

        if database_id is not None:
            database = await dao.find_by_id(int(str(database_id)))
            if database is None:
                return Response(
                    content="<html><body>Database not found</body></html>",
                    status_code=404,
                    media_type="text/html",
                )

        html = (
            "<html><body><script>"
            "if (window.opener) {"
            "  window.opener.postMessage("
            f'    {{"type": "oauth2_redirect", '
            f'"database_id": {json.dumps(database_id)}, '
            f'"tab_id": {json.dumps(tab_id)}, '
            f'"code": {json.dumps(code)}}}, '
            "    window.location.origin"
            "  );"
            "}"
            "window.close();"
            "</script></body></html>"
        )
        return Response(
            content=html,
            status_code=200,
            media_type="text/html",
        )

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
        cmd = DeleteSSHTunnelCommand(dao=cast("AsyncDatabaseDAO", dao), database_id=pk)
        await cmd.execute()
        event_logger.log("database.delete_ssh_tunnel", object_ref=f"database:{pk}")
        return {"message": "OK"}
