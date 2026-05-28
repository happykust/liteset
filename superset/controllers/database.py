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

import asyncio
import io
import json
import logging
import re
from typing import Any, cast, TYPE_CHECKING

import msgspec
from litestar import Controller, delete, get, post, put
from litestar.connection import Request
from litestar.datastructures import UploadFile
from litestar.di import Provide
from litestar.enums import RequestEncodingType
from litestar.params import Body, Parameter
from litestar.response import Response, Stream, Template

from superset.commands.database.create import CreateDatabaseCommand
from superset.commands.database.delete import DeleteDatabaseCommand
from superset.commands.database.export import ExportDatabasesCommand
from superset.commands.database.importers.v1 import ImportDatabasesCommand
from superset.commands.database.ssh_tunnel.delete import DeleteSSHTunnelCommand
from superset.commands.database.sync_permissions import SyncPermissionsCommand
from superset.commands.database.test_connection import DatabaseTestConnectionCommand
from superset.commands.database.update import UpdateDatabaseCommand
from superset.commands.database.uploaders.base import UploadCommand
from superset.commands.database.validate import ValidateParametersCommand
from superset.commands.database.validate_sql import ValidateSQLCommand
from superset.config import SupersetSettings

# DAO imports moved to provider functions
from superset.controllers.base import (
    build_export_headers,
    build_rison_query_params,
    extract_ids,
    get_info_payload,
    get_related_payload,
    serialize_list_response,
    stream_zip,
)
from superset.events import event_logger
from superset.exceptions import (
    CommandInvalidError,
    ObjectNotFoundError,
    SupersetException,
    SupersetSecurityException,
)
from superset.guards.rbac import require_permission
from superset.params.rison import provide_rison_query
from superset.providers import (
    provide_database_dao,
    provide_database_user_oauth2_tokens_dao,
)
from superset.schemas.database import (
    CatalogsResponse,
    DatabaseConnectionResponse,
    DatabaseDetailResult,
    DatabaseGetResponse,
    DatabasePostSchema,
    DatabasePutSchema,
    DatabaseTestConnectionSchema,
    DatabaseValidateParamsSchema,
    FileMetadataItem,
    FileMetadataResponse,
    rename_encrypted_extra,
    SchemaAccessForUploadResponse,
    SchemasResponse,
    SelectStarResponse,
    TableMetadataColumn,
    TableMetadataIndex,
    TableMetadataResponse,
    ValidateSQLSchema,
)
from superset.sql.parse import Table
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


def _inspect_table_metadata(  # noqa: C901
    sync_conn: Any,
    table_name: str,
    schema: str | None,
) -> dict[str, Any]:
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
    except (NotImplementedError, Exception):  # noqa: BLE001, S110
        pass  # Some engines don't support get_table_comment

    # --- columns payload ---------------------------------------------------
    columns_payload: list[dict[str, Any]] = []
    for col in raw_columns:
        dtype = _get_col_type(col)
        col_name = col.get("name", col.get("column_name", ""))
        columns_payload.append(
            {
                "name": col_name,
                "type": dtype.split("(")[0] if "(" in dtype else dtype,
                "longType": dtype,
                "keys": [k for k in keys if col_name in k.get("column_names", [])],
                "comment": col.get("comment"),
            }
        )

    # --- select star -------------------------------------------------------
    # Generate SELECT * using the connection's dialect for proper quoting.
    # Matches original ``database.select_star(table, indent=True, cols=columns,
    # latest_partition=True)`` as closely as possible without access to the
    # Database model's ``select_star`` method.  The original uses
    # ``BaseEngineSpec.select_star`` which requires ``database.get_columns``
    # and ``database.compile_sqla_query`` — methods not yet ported.
    dialect = sync_conn.dialect
    quoted_table = dialect.identifier_preparer.quote_identifier(table_name)
    if schema:
        quoted_schema = dialect.identifier_preparer.quote_identifier(schema)
        full_name = f"{quoted_schema}.{quoted_table}"
    else:
        full_name = quoted_table

    qry = select(text("*")).select_from(text(full_name)).limit(100)
    raw_sql = str(qry.compile(dialect=dialect, compile_kwargs={"literal_binds": True}))
    # Apply simple indentation to match ``indent=True`` from original
    select_star_sql = raw_sql.replace(" FROM ", "\nFROM ").replace(
        " \n LIMIT ", "\nLIMIT "
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

    # NOTE: _inspect_table_extra_metadata was removed.  Both the deprecated
    # and non-deprecated extra-metadata endpoints now delegate to
    # ``database.db_engine_spec.get_extra_table_metadata()`` which matches
    # the original Superset implementation.  The base engine spec returns
    # ``{}`` and engine-specific overrides (e.g. BigQuery) provide
    # partition/clustering info.


def _build_database_result(db: Any) -> DatabaseDetailResult:
    """Build a full database result from a Database model instance.

    Used by create, update, and GET /{pk} to return a consistent,
    expanded response matching Superset's original API contract.
    """
    return DatabaseDetailResult.from_model(db, mask_uri=mask_uri_password)


async def _decode_connection_body(
    request: Request[Any, Any, Any], struct_cls: Any
) -> Any:
    """Decode a JSON body into ``struct_cls`` with the legacy alias applied.

    The POST / PUT / TestConnection / ValidateParameters request bodies must
    accept the legacy ``encrypted_extra`` key as an alias for
    ``masked_encrypted_extra`` (1:1 with the original
    ``rename_encrypted_extra`` ``@pre_load`` hook).  Litestar's typed-param
    injection would silently drop the unknown legacy key, so we read the raw
    body, normalize it, then ``msgspec.convert`` into the target struct (which
    runs its ``__post_init__`` validation, e.g. URI safety + JSON validity).

    The body value is a credential and is never logged here. Malformed JSON
    or schema mismatches surface as Litestar ``ValidationException`` (mapped
    to 422 by the global handler), matching upstream's ``response_400`` /
    ``response_422`` for the same conditions. NB:
    ``msgspec.ValidationError`` IS-A ``msgspec.DecodeError`` — catch the
    narrower one first or you lose the field-level message.
    """
    from litestar.exceptions import ValidationException

    raw = await request.body()
    try:
        decoded: Any = msgspec.json.decode(raw) if raw else {}
    except msgspec.DecodeError as ex:
        raise ValidationException(detail=f"Body is not valid JSON: {ex}") from ex
    if isinstance(decoded, dict):
        decoded = rename_encrypted_extra(decoded)
    try:
        return msgspec.convert(decoded, type=struct_cls)
    except msgspec.ValidationError as ex:
        raise ValidationException(detail=f"Request is incorrect: {ex}") from ex


# ---------------------------------------------------------------------------
# Engine-spec-aware ``SELECT *`` generation
# ---------------------------------------------------------------------------


def _engine_select_star_sync(database: Any, table: Table) -> str:
    """Generate engine-correct ``SELECT *`` SQL via the engine spec.

    Mirrors original ``Database.select_star`` /
    ``BaseEngineSpec.select_star`` — quoting matches the engine's
    dialect (backticks for MySQL, brackets for MSSQL, double-quotes
    for Postgres / Trino / Snowflake / Redshift / BigQuery, etc.) and
    a partition-aware ``WHERE`` is applied for Hive / Presto / Trino
    partitioned tables.

    Runs in a worker thread (called via :func:`asyncio.to_thread`)
    because :func:`Database.get_sqla_engine` opens a sync SQLAlchemy
    Engine.
    """
    db_engine_spec = getattr(database, "db_engine_spec", None)
    if db_engine_spec is None or not hasattr(db_engine_spec, "select_star"):
        if table.schema:
            return f'SELECT *\nFROM "{table.schema}"."{table.table}"'
        return f'SELECT *\nFROM "{table.table}"'

    catalog = getattr(database, "get_default_catalog", lambda: None)()
    qualified = Table(table.table, table.schema, table.catalog or catalog)

    # The /select_star/ endpoint always asks for ``show_cols=False`` and
    # the partition rewrite only fires when ``latest_partition=True``;
    # ``BaseEngineSpec.select_star`` only calls ``database.get_columns``
    # when *either* is true (``if (show_cols or latest_partition) and not
    # cols``). So with both off we can skip the pre-fetch entirely —
    # eliminates the spurious ``failed to introspect columns`` warnings
    # for non-existent tables (upstream's API behaves the same: returns
    # plain ``SELECT * FROM table LIMIT 100`` without any introspection).
    try:
        with database.get_sqla_engine(  # type: ignore[attr-defined]
            catalog=qualified.catalog,
            schema=qualified.schema,
        ) as engine:
            return db_engine_spec.select_star(
                database,
                qualified,
                engine,
                limit=100,
                show_cols=False,
                indent=True,
                latest_partition=False,
                cols=None,
            )
    except Exception:  # noqa: BLE001
        if hasattr(db_engine_spec, "quote_table"):
            try:
                from sqlalchemy.dialects import registry as _registry

                dialect_name = (
                    str(getattr(database, "sqlalchemy_uri", "") or "")
                    .split("://")[0]
                    .split("+")[0]
                )
                dialect_cls = _registry.load(dialect_name)
                full = db_engine_spec.quote_table(qualified, dialect_cls())
                return f"SELECT *\nFROM {full}\nLIMIT 100"
            except Exception:  # noqa: BLE001, S110
                pass
        if qualified.schema:
            return (
                f'SELECT *\nFROM "{qualified.schema}"."{qualified.table}"\nLIMIT 100'
            )
        return f'SELECT *\nFROM "{qualified.table}"\nLIMIT 100'


async def _engine_select_star(database: Any, table: Table) -> str:
    """Async wrapper around :func:`_engine_select_star_sync`."""
    return await asyncio.to_thread(_engine_select_star_sync, database, table)


# ---------------------------------------------------------------------------
# OAuth2 token purge — mirrors ``Database.purge_oauth2_tokens``
# (``superset_old/models/core.py:1189``).
# ---------------------------------------------------------------------------

_OAUTH2_PURGE_KEYS: frozenset[str] = frozenset(
    {"id", "scope", "authorization_request_uri", "token_request_uri"}
)


def _extract_oauth2_client_info(raw: str | None) -> dict[str, Any]:
    """Parse ``encrypted_extra`` JSON and return ``oauth2_client_info``."""
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    info = payload.get("oauth2_client_info")
    return info if isinstance(info, dict) else {}


async def _purge_oauth2_tokens(
    dao: "DatabaseDAOProtocol", database_id: int
) -> None:
    """Delete every ``DatabaseUserOAuth2Tokens`` row for ``database_id``.

    Mirrors ``Database.purge_oauth2_tokens`` at
    ``superset_old/models/core.py:1189-1199`` (with the column-name
    bug from the original — which filtered on ``id`` rather than
    ``database_id`` — corrected).
    """
    from sqlalchemy import delete as sa_delete

    from superset.models.core import DatabaseUserOAuth2Tokens

    session = getattr(dao, "session", None)
    if session is None:
        return
    stmt = sa_delete(DatabaseUserOAuth2Tokens).where(
        DatabaseUserOAuth2Tokens.database_id == database_id
    )
    await session.execute(stmt)
    await session.flush()


async def _purge_oauth2_tokens_if_needed(
    *,
    dao: "DatabaseDAOProtocol",
    database: Any,
    new_encrypted_extra: str | None,
) -> None:
    """Purge OAuth2 tokens iff the OAuth2 client config rotated.

    Mirrors ``UpdateDatabaseCommand._handle_oauth2`` at
    ``superset_old/commands/database/update.py:128-159``:

    - When ``encrypted_extra`` becomes ``None`` (OAuth2 disabled),
      purge unconditionally.
    - Otherwise we conservatively purge whenever ``oauth2_client_info``
      is set in the new payload.  The original implementation diffs
      against the pre-update encrypted_extra; in the async/setattr
      flow the previous value is no longer accessible at this point,
      so we err on the side of safety (idempotent DELETE — no-op if
      no tokens exist).
    """
    if new_encrypted_extra is None:
        await _purge_oauth2_tokens(dao, int(database.id))
        return

    new_config = _extract_oauth2_client_info(new_encrypted_extra)
    if not new_config:
        return

    await _purge_oauth2_tokens(dao, int(database.id))


# ---------------------------------------------------------------------------
# SSH tunnel CRUD wrappers
# ---------------------------------------------------------------------------


async def _create_ssh_tunnel(
    *,
    dao: "DatabaseDAOProtocol",
    database: Any,
    payload: dict[str, Any],
) -> Any:
    """Run :class:`CreateSSHTunnelCommand` for ``database`` + ``payload``.

    Mirrors original ``CreateDatabaseCommand.run`` lines 88-99 — the
    SSH tunnel row is inserted in a second step after the database
    row is created.
    """
    from superset.commands.database.ssh_tunnel.create import CreateSSHTunnelCommand
    from superset.commands.database.ssh_tunnel.exceptions import (
        SSHTunnelingNotEnabledError,
    )
    from superset.db.daos.database import AsyncSSHTunnelDAO
    from superset.utils.feature_flags import feature_flag_manager

    if not feature_flag_manager.is_feature_enabled("SSH_TUNNELING"):
        raise SSHTunnelingNotEnabledError()

    ssh_dao = AsyncSSHTunnelDAO(dao.session)  # type: ignore[attr-defined]
    cmd = CreateSSHTunnelCommand(
        dao=ssh_dao,
        database=database,
        data=dict(payload),
    )
    return await cmd.execute()


async def _sync_ssh_tunnel(
    *,
    dao: "DatabaseDAOProtocol",
    database: Any,
    payload: dict[str, Any] | None,
) -> Any:
    """Create / update / delete the SSH tunnel row.

    Mirrors original ``UpdateDatabaseCommand._handle_ssh_tunnel`` lines
    161-185:

    - ``payload is None`` (or ``{}``): delete current tunnel if present.
    - ``payload`` + no current tunnel: ``CreateSSHTunnelCommand``.
    - ``payload`` + current tunnel: ``UpdateSSHTunnelCommand``.
    """
    from superset.commands.database.ssh_tunnel.delete import DeleteSSHTunnelCommand
    from superset.commands.database.ssh_tunnel.exceptions import (
        SSHTunnelingNotEnabledError,
    )
    from superset.commands.database.ssh_tunnel.update import UpdateSSHTunnelCommand
    from superset.db.daos.database import AsyncSSHTunnelDAO
    from superset.utils.feature_flags import feature_flag_manager

    if not feature_flag_manager.is_feature_enabled("SSH_TUNNELING"):
        raise SSHTunnelingNotEnabledError()

    ssh_dao = AsyncSSHTunnelDAO(dao.session)  # type: ignore[attr-defined]
    current = await ssh_dao.get_by_database_id(int(database.id))

    if payload is None or payload == {}:  # noqa: PLC1901  # explicit empty-dict check
        if current is not None:
            del_cmd = DeleteSSHTunnelCommand(
                dao=cast("AsyncDatabaseDAO", dao),
                database_id=int(database.id),
            )
            await del_cmd.execute()
        return None

    if current is None:
        return await _create_ssh_tunnel(
            dao=dao,
            database=database,
            payload=payload,
        )

    upd_cmd = UpdateSSHTunnelCommand(
        dao=ssh_dao,
        model_id=current.id,
        data=dict(payload),
    )
    return await upd_cmd.execute()


def _serialise_ssh_tunnel(tunnel: Any) -> dict[str, Any]:
    """Return ``tunnel.data`` for the API response.

    Mirrors original ``superset_old/databases/api.py:post`` /
    ``put`` handlers which echo ``mask_password_info(model.ssh_tunnel)``
    back into the response (lines 466-467 / 553-555).  The new
    ``SSHTunnel`` model embeds masking in its ``data`` property so we
    can reuse it directly.
    """
    if tunnel is None:
        return {}
    if hasattr(tunnel, "data"):
        return dict(tunnel.data)
    return {
        "id": getattr(tunnel, "id", None),
        "server_address": getattr(tunnel, "server_address", ""),
        "server_port": getattr(tunnel, "server_port", 22),
        "username": getattr(tunnel, "username", ""),
    }


def _coerce_ssh_tunnel_payload(value: Any) -> dict[str, Any] | None:
    """Convert a msgspec struct / dict into a plain dict (or ``None``)."""
    if value is None or value is msgspec.UNSET:
        return None
    if hasattr(value, "__struct_fields__"):
        return msgspec.structs.asdict(value)
    if isinstance(value, dict):
        return dict(value)
    return None


async def _database_is_accessible(
    security_manager: Any,
    user: Any,
    database: Any,
) -> bool:
    """Return whether ``user`` may view ``database``.

    In-Python evaluation of the same predicate encoded by
    ``superset.db.filters.database_access_filters`` (1:1 with the original
    ``DatabaseFilter``): ``all_database_access`` holders see everything;
    everyone else needs a ``database_access`` perm on this database OR its
    name appearing in a ``catalog_access`` / ``schema_access`` /
    ``datasource_access`` view-menu permission.  Used for GET-by-id so an
    inaccessible database returns 404 (existence hidden), matching FAB's
    ``get_headless`` base-filter behaviour.
    """
    from superset.db.filters import _databases_from_view_menus

    if await security_manager.can_access_all_databases(user=user):
        return True

    database_perms = await security_manager.user_view_menu_names(
        "database_access", user=user
    )
    if getattr(database, "perm", None) in database_perms:
        return True

    catalog_access = await security_manager.user_view_menu_names(
        "catalog_access", user=user
    )
    schema_access = await security_manager.user_view_menu_names(
        "schema_access", user=user
    )
    datasource_access = await security_manager.user_view_menu_names(
        "datasource_access", user=user
    )
    database_names = (
        _databases_from_view_menus(catalog_access)
        | _databases_from_view_menus(schema_access)
        | _databases_from_view_menus(datasource_access)
    )
    return getattr(database, "database_name", None) in database_names


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
        current_user: UserProtocol,
        security_manager: SecurityManagerProtocol,
    ) -> dict[str, Any]:
        from sqlalchemy.orm import selectinload

        from superset.db.filters import database_access_filters
        from superset.models.core import Database

        # Anonymous callers see an empty list — mirrors original
        # ``DatabaseFilter`` (databases/filters.py) which restricts the query
        # to databases the caller is allowed to read.  ``can_read`` on the menu
        # is satisfied by Public role permissions but data-level access
        # (``database_access`` / ``all_database_access``) is required to see
        # actual rows.
        if not getattr(current_user, "is_authenticated", False):
            return {
                "count": 0,
                "ids": [],
                "result": [],
                "label_columns": {},
                "list_columns": [],
                "list_title": "",
                "description_columns": {},
            }

        rison_filters, order_by, page, page_size = build_rison_query_params(
            Database,
            rison_params,
        )
        if not order_by:
            order_by = [Database.changed_on.desc()]

        # Apply the RBAC base filter (1:1 with the original ``DatabaseFilter``
        # base_filter): non-``all_database_access`` users only see databases
        # they hold ``database_access`` for, or whose name appears in a
        # catalog/schema/datasource_access permission.
        base_filters = await database_access_filters(security_manager, current_user)
        combined_filters = (rison_filters or []) + (base_filters or [])

        databases = await dao.find_all(
            filters=combined_filters or None,
            page=page,
            page_size=page_size,
            order_by=order_by,
            options=[
                selectinload(Database.changed_by),
                selectinload(Database.created_by),
            ],
        )
        total = await dao.count(filters=combined_filters or None)
        await event_logger.alog_with_context("database.list")
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
            list_title="List Database",
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
            permissions=["can_upload", "can_read", "can_write", "can_export"],
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
    # GET /{pk}/connection — connection info
    # ------------------------------------------------------------------
    @get(
        "/{pk:int}/connection",
        # The original ``constants.MODEL_API_RW_METHOD_PERMISSION_MAP`` maps
        # ``get_connection`` to ``write`` — this endpoint returns the full
        # connection info (incl. masked URI, parameters_schema, ssh_tunnel),
        # so it requires ``can_write`` rather than ``can_read``.
        guards=[require_permission("can_write", "Database")],
    )
    async def get_connection(
        self, pk: int, dao: DatabaseDAOProtocol
    ) -> DatabaseConnectionResponse:
        database = await dao.find_by_id(pk)
        if not database:
            raise ObjectNotFoundError("Database", pk)
        # Mirrors ``DatabaseConnectionSchema`` from
        # ``superset_old.databases.schemas`` (via
        # ``DatabaseRestApi.get_connection``).  Fields ``id``,
        # ``engine_information`` and ``parameters_schema`` are required
        # for the frontend's edit modal — without ``id`` it falls back
        # to POST and trips the duplicate-name validator; without
        # ``parameters_schema`` the dynamic-form connector renders empty.
        result: dict[str, Any] = {
            "id": database.id,
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
            "engine_information": getattr(database, "engine_information", None) or {},
            "expose_in_sqllab": getattr(database, "expose_in_sqllab", True),
            "extra": getattr(database, "extra", None),
            "force_ctas_schema": getattr(database, "force_ctas_schema", None),
            "impersonate_user": getattr(database, "impersonate_user", False),
            "is_managed_externally": getattr(
                database, "is_managed_externally", False
            ),
            # Original returns ``""`` (empty string) when no encrypted
            # extra is set, not ``null`` — the form's JSON-editor binds
            # to a string and chokes on null.
            "masked_encrypted_extra": (
                getattr(database, "masked_encrypted_extra", None) or ""
            ),
            "parameters": getattr(database, "parameters", None) or {},
            "parameters_schema": getattr(database, "parameters_schema", None) or {},
            "server_cert": getattr(database, "server_cert", None),
            "uuid": (
                str(database.uuid) if getattr(database, "uuid", None) else None
            ),
        }
        # ssh_tunnel is added separately — original
        # ``superset_old/databases/api.py:get_connection`` calls
        # ``DatabaseDAO.get_ssh_tunnel(pk)`` and only attaches the field
        # when a tunnel exists.  Mirror that conditional behaviour so
        # the response has the same shape for both connector states.
        tunnel = await dao.get_ssh_tunnel(pk)
        if tunnel is not None:
            result["ssh_tunnel"] = _serialise_ssh_tunnel(tunnel) or None
        return DatabaseConnectionResponse(id=database.id, result=result)

    # ------------------------------------------------------------------
    # GET /{pk} — get database
    # ------------------------------------------------------------------
    @get(
        "/{pk:int}",
        guards=[require_permission("can_read", "Database")],
    )
    async def get_database(
        self,
        pk: int,
        dao: DatabaseDAOProtocol,
        current_user: UserProtocol,
        security_manager: SecurityManagerProtocol,
    ) -> DatabaseGetResponse:
        database = await dao.find_by_id(pk)
        if not database:
            raise ObjectNotFoundError("Database", pk)
        # Enforce the RBAC base filter on GET-by-id, mirroring the original
        # ``DatabaseRestApi.get`` which fetches via ``get_headless`` and so
        # applies ``base_filters = [["id", DatabaseFilter, ...]]`` — an
        # inaccessible database returns 404 (existence is hidden).
        if not await _database_is_accessible(security_manager, current_user, database):
            raise ObjectNotFoundError("Database", pk)
        result = _build_database_result(database)
        # Merge SSH tunnel data into response — matches original
        # superset_old/databases/api.py:get (lines 397-403) which calls
        # DatabaseDAO.get_ssh_tunnel(pk) and adds payload["result"]["ssh_tunnel"].
        tunnel = await dao.get_ssh_tunnel(pk)
        if tunnel is not None:
            result.ssh_tunnel = _serialise_ssh_tunnel(tunnel) or None
        return DatabaseGetResponse(
            id=database.id,
            result=result,
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
        request: Request[Any, Any, Any],
        dao: DatabaseDAOProtocol,
        current_user: UserProtocol,
    ) -> DatabaseGetResponse:
        # Bind the current user to the request-scoped ContextVar so downstream
        # commands resolving ``get_current_user()`` (e.g. ``SyncPermissionsCommand``
        # invoked from ``UpdateDatabaseCommand._sync_permissions``) find the
        # logged-in user; without this they raise ``UserNotFoundInSessionError``.
        from superset.utils.core import set_current_user

        set_current_user(current_user)
        # Normalize the legacy ``encrypted_extra`` key -> ``masked_encrypted_extra``
        # before validation (1:1 with the original ``rename_encrypted_extra``
        # ``@pre_load`` hook) so older API clients keep working.
        data: DatabasePostSchema = await _decode_connection_body(
            request, DatabasePostSchema
        )
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

        # Extract ssh_tunnel before stripping it from create_data — the
        # CreateDatabaseCommand only creates the Database row; the SSH tunnel
        # is inserted in a separate step via CreateSSHTunnelCommand, matching
        # superset_old/commands/database/create.py:88-102.
        ssh_tunnel_payload = _coerce_ssh_tunnel_payload(
            create_data.pop("ssh_tunnel", None)
        )

        cmd = CreateDatabaseCommand(
            dao=cast("AsyncDatabaseDAO", dao),
            data=create_data,
            user_id=current_user.id,
        )
        db = await cmd.execute()
        db_id = int(db.id)

        # Create SSH tunnel row if the request included one and the feature is enabled.
        # Mirrors original CreateDatabaseCommand.run() lines 88-102.
        tunnel: Any | None = None
        if ssh_tunnel_payload:
            try:
                tunnel = await _create_ssh_tunnel(
                    dao=dao,
                    database=db,
                    payload=ssh_tunnel_payload,
                )
            except Exception as _ssh_exc:  # noqa: BLE001
                # Log and re-raise so the client sees a clear error —
                # matches original superset_old error propagation.
                _log.warning(
                    "Failed to create SSH tunnel for database %s: %s",
                    db_id,
                    _ssh_exc,
                )
                raise

        await event_logger.alog_with_context(
            "database.create",
            object_ref=f"database:{db_id}",
            user_id=current_user.id,
        )
        result = _build_database_result(db)
        # Include masked SSH tunnel in response, matching original line 466-467.
        if tunnel is not None:
            result.ssh_tunnel = _serialise_ssh_tunnel(tunnel) or None
        return DatabaseGetResponse(
            id=db_id,
            result=result,
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
        request: Request[Any, Any, Any],
        dao: DatabaseDAOProtocol,
        current_user: UserProtocol,
    ) -> DatabaseGetResponse:
        # Bind the current user to the request-scoped ContextVar so downstream
        # commands resolving ``get_current_user()`` (e.g. ``SyncPermissionsCommand``
        # invoked from ``UpdateDatabaseCommand._sync_permissions``) find the
        # logged-in user; without this they raise ``UserNotFoundInSessionError``.
        from superset.utils.core import set_current_user

        set_current_user(current_user)
        # Normalize the legacy ``encrypted_extra`` key -> ``masked_encrypted_extra``
        # before validation (1:1 with the original ``rename_encrypted_extra``
        # ``@pre_load`` hook) so older API clients keep working.
        data: DatabasePutSchema = await _decode_connection_body(
            request, DatabasePutSchema
        )
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
        # Pass ``masked_encrypted_extra`` THROUGH to the command unchanged
        # (do NOT pre-rename to ``encrypted_extra``).  The command's unmask
        # branch — mirroring ``superset_old/commands/database/update.py:70-77``
        # — resolves the ``XXXXXXXXXX`` placeholders against the existing
        # database's stored secret via
        # ``db_engine_spec.unmask_encrypted_extra`` (which uses
        # ``superset.utils.json.reveal_sensitive``) and only then writes the
        # real value to ``encrypted_extra``.  Renaming the key here would skip
        # that step and persist the masked placeholders, destroying the real
        # OAuth2/encrypted credentials.
        if data.masked_encrypted_extra is not msgspec.UNSET:
            update_data["masked_encrypted_extra"] = data.masked_encrypted_extra

        # Extract ssh_tunnel before passing data to UpdateDatabaseCommand.
        # The command only updates Database model columns; SSH tunnel is handled
        # separately via _sync_ssh_tunnel (create/update/delete), matching
        # superset_old/commands/database/update.py:98 + _handle_ssh_tunnel.
        # Use UNSET sentinel to distinguish "not sent" from "sent as null".
        ssh_tunnel_in_body = "ssh_tunnel" in update_data
        ssh_tunnel_payload_raw = update_data.pop("ssh_tunnel", msgspec.UNSET)
        ssh_tunnel_payload: dict[str, Any] | None = (
            _coerce_ssh_tunnel_payload(ssh_tunnel_payload_raw)
            if ssh_tunnel_in_body
            else msgspec.UNSET  # type: ignore[assignment]
        )

        cmd = UpdateDatabaseCommand(
            dao=cast("AsyncDatabaseDAO", dao),
            database_id=pk,
            data=update_data,
            user_id=current_user.id,
        )
        db = await cmd.execute()

        # -----------------------------------------------------------------
        # OAuth2 token purge — mirrors UpdateDatabaseCommand._handle_oauth2.
        # Must run AFTER the database row is updated so we compare against
        # the NEW encrypted_extra value.
        # -----------------------------------------------------------------
        new_encrypted_extra = update_data.get("encrypted_extra", msgspec.UNSET)
        if new_encrypted_extra is not msgspec.UNSET:
            await _purge_oauth2_tokens_if_needed(
                dao=dao,
                database=db,
                new_encrypted_extra=new_encrypted_extra,
            )

        # -----------------------------------------------------------------
        # SSH tunnel CRUD — mirrors UpdateDatabaseCommand._handle_ssh_tunnel.
        # Only runs when the request body included an `ssh_tunnel` key.
        # -----------------------------------------------------------------
        tunnel: Any | None = None
        if ssh_tunnel_in_body and ssh_tunnel_payload is not msgspec.UNSET:
            try:
                tunnel = await _sync_ssh_tunnel(
                    dao=dao,
                    database=db,
                    payload=ssh_tunnel_payload,
                )
            except Exception as _ssh_exc:  # noqa: BLE001
                _log.warning(
                    "Failed to sync SSH tunnel for database %s: %s",
                    pk,
                    _ssh_exc,
                )
                raise

        await event_logger.alog_with_context(
            "database.update",
            object_ref=f"database:{pk}",
            user_id=current_user.id,
        )
        result = _build_database_result(db)
        # Echo masked SSH tunnel in response when it was provided, matching
        # original lines 554-555.
        if tunnel is not None:
            result.ssh_tunnel = _serialise_ssh_tunnel(tunnel) or None
        return DatabaseGetResponse(
            id=int(db.id),
            result=result,
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
        await event_logger.alog_with_context(
            "database.delete", object_ref=f"database:{pk}"
        )
        return {"message": "OK"}

    # ------------------------------------------------------------------
    # POST /{pk}/sync_permissions/ — sync FAB permissions
    # ------------------------------------------------------------------
    @post(
        "/{pk:int}/sync_permissions/",
        guards=[require_permission("can_write", "Database")],
        status_code=200,
    )
    async def sync_permissions(
        self,
        pk: int,
        dao: DatabaseDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
    ) -> Response[dict[str, Any]]:
        # Mirrors superset_old/databases/api.py:sync_permissions verbatim: run
        # the command (which dispatches the Celery task in async mode, else runs
        # inline) then return 202/200 with the original message.
        username: str | None = getattr(current_user, "username", None)
        await SyncPermissionsCommand(
            dao=cast("AsyncDatabaseDAO", dao),
            database_id=pk,
            security_manager=security_manager,
            username=username,
        ).execute()
        await event_logger.alog_with_context("database.sync_permissions")
        if SupersetSettings().sync_db_permissions_in_async_mode:  # type: ignore[call-arg]
            return Response(
                {"message": "Async task created to sync permissions"},
                status_code=202,
                media_type="application/json",
            )
        return Response(
            {"message": "Permissions successfully synced"},
            status_code=200,
            media_type="application/json",
        )

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
        rison_params: dict[str, Any] | None,
        schema_name: str = Parameter(query="schema_name", default=""),
        catalog: str | None = Parameter(query="catalog", default=None),
        force: bool = Parameter(query="force", default=False),
    ) -> dict[str, Any]:
        # Original Superset sends params inside Rison ``q`` parameter.
        # Fall back to direct query params for backward compat.
        rison = rison_params or {}
        effective_force = rison.get("force", force)
        effective_schema = rison.get("schema_name", schema_name) or None
        _effective_catalog = rison.get("catalog_name", catalog)
        _ = effective_force  # async path always fetches live
        # Original API requires ``schema_name`` (passed via Rison ``q``).
        # Without it the request can't be served — return 400 to mirror
        # Marshmallow validation behaviour.
        if not effective_schema:
            from litestar.exceptions import ClientException

            raise ClientException(
                detail="schema_name is required in the Rison query parameter",
                status_code=400,
            )
        database = await dao.find_by_id(pk)
        if not database:
            raise ObjectNotFoundError("Database", pk)
        schema = effective_schema
        try:
            async with get_async_connection(database) as (conn, engine_spec):
                table_names = await engine_spec.get_table_names(conn, schema=schema)
                view_names = await engine_spec.get_view_names(conn, schema=schema)
            # Batch-fetch extra (certification info) from SqlaTable for
            # all discovered tables/views so the frontend gets it.
            all_names = set(table_names) | set(view_names)
            extra_lookup = await dao.get_table_extra_lookup(
                database_id=pk,
                table_names=all_names,
                schema=schema,
            )

            # Mirror ``superset_old/commands/database/tables.py:119-136``:
            # only TABLE entries carry an ``extra`` key (defaulting to
            # ``None``); VIEW entries are emitted without ``extra``.
            options: list[dict[str, Any]] = sorted(
                [
                    {
                        "value": str(t),
                        "type": "table",
                        "extra": extra_lookup.get(str(t), None),
                    }
                    for t in table_names
                ]
                + [
                    {
                        "value": str(v),
                        "type": "view",
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
        """GET /api/v1/database/{pk}/table_metadata/

        Mirrors the original ``DatabaseRestApi.table_metadata`` which uses
        ``security_manager.raise_for_access(database=database, table=table)``
        for TABLE-level permission checks.
        """
        await event_logger.alog_with_context(
            "database.table_metadata.init",
            object_ref=f"database:{pk}",
        )
        # Accept both ``schema`` (original Superset) and ``schema_name`` (alias)
        effective_schema = schema_name or schema or None
        database = await dao.find_by_id(pk)
        if not database:
            raise ObjectNotFoundError("Database", pk)

        if not name:
            raise CommandInvalidError("Missing required parameter: name")

        table = Table(name, effective_schema, catalog)
        try:
            await security_manager.raise_for_access(
                database=database,
                table=table,
                user=current_user,
            )
        except SupersetSecurityException as exc:
            # Match original: raise 404 to hide table existence
            raise ObjectNotFoundError("Table", name) from exc

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
    ) -> dict[str, Any]:
        """GET /api/v1/database/{pk}/table_metadata/extra/

        Mirrors the original ``DatabaseRestApi.table_extra_metadata``
        which delegates to ``database.db_engine_spec.get_extra_table_metadata()``.
        """
        await event_logger.alog_with_context(
            "database.table_extra_metadata.init",
            object_ref=f"database:{pk}",
        )
        # Accept both ``schema`` (original Superset) and ``schema_name`` (alias)
        effective_schema = schema_name or schema or None
        database = await dao.find_by_id(pk)
        if not database:
            raise ObjectNotFoundError("Database", pk)

        if not name:
            raise CommandInvalidError("Missing required parameter: name")

        table = Table(name, effective_schema, catalog)
        try:
            await security_manager.raise_for_access(
                database=database,
                table=table,
                user=current_user,
            )
        except SupersetSecurityException as exc:
            # Match original: raise 404 to hide table existence
            raise ObjectNotFoundError("Table", name) from exc

        # Delegate to engine spec — matches original:
        #   database.db_engine_spec.get_extra_table_metadata(database, table)
        db_engine_spec = getattr(database, "db_engine_spec", None)
        if db_engine_spec and hasattr(db_engine_spec, "get_extra_table_metadata"):
            payload = await asyncio.to_thread(
                db_engine_spec.get_extra_table_metadata,
                database,
                table,
            )
        else:
            payload = {}

        return payload

    # ------------------------------------------------------------------
    # GET /{pk}/table/{table_name}/{schema_name}/ — table metadata (deprecated path)
    # ------------------------------------------------------------------
    @get(
        "/{pk:int}/table/{table_name:path}/{schema_name:str}/",
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
    ) -> TableMetadataResponse | Response[Any]:
        """Get database table metadata (deprecated path).

        Deprecated in favour of ``GET /{pk}/table_metadata/`` which uses
        query parameters and supports catalogs (SIP-95).  This path-based
        variant is kept for backward compatibility with older API clients.

        Mirrors the original ``DatabaseRestApi.table_metadata_deprecated``
        which uses the ``@check_table_access`` decorator for TABLE-level
        permission checks and delegates to ``get_table_metadata()`` from
        ``superset/databases/utils.py``.
        """
        from urllib.parse import unquote_plus

        from sqlalchemy.exc import SQLAlchemyError

        await event_logger.alog_with_context(
            "database.table_metadata_deprecated.init",
            object_ref=f"database:{pk}",
        )

        # Parse JS-style URI path items (mirrors parse_js_uri_path_item)
        parsed_schema: str | None = schema_name
        if schema_name in ("null", "undefined"):
            parsed_schema = None
        else:
            parsed_schema = unquote_plus(schema_name)

        parsed_table = unquote_plus(table_name)
        if not parsed_table:
            return Response(
                content={"message": "Table name undefined"},
                status_code=422,
            )

        database = await dao.find_by_id(pk)
        if not database:
            await event_logger.alog_with_context(
                "database.table_metadata_deprecated.error",
                object_ref=f"database:{pk}",
            )
            raise ObjectNotFoundError("Database", pk)

        # Table-level access check — matches original @check_table_access
        table = Table(parsed_table, parsed_schema)
        try:
            await security_manager.raise_for_access(
                database=database,
                table=table,
                user=current_user,
            )
        except SupersetSecurityException as exc:
            await event_logger.alog_with_context(
                "database.table_metadata_deprecated.error",
                object_ref=f"database:{pk}",
            )
            _log.warning(
                "Permission denied for user %s on table: %s schema: %s",
                current_user,
                parsed_table,
                parsed_schema,
            )
            raise ObjectNotFoundError("Table", parsed_table) from exc

        try:
            async with get_async_connection(database) as (conn, _engine_spec):
                raw = await conn.run_sync(
                    _inspect_table_metadata,
                    parsed_table,
                    parsed_schema,
                )
        except SQLAlchemyError as exc:
            await event_logger.alog_with_context(
                "database.table_metadata_deprecated.error",
                object_ref=f"database:{pk}",
            )
            return Response(
                content={"message": str(exc)},
                status_code=422,
            )
        except SupersetException as exc:
            await event_logger.alog_with_context(
                "database.table_metadata_deprecated.error",
                object_ref=f"database:{pk}",
            )
            return Response(
                content={"message": exc.message},
                status_code=exc.status_code,
            )

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

        await event_logger.alog_with_context(
            "database.table_metadata_deprecated.success",
            object_ref=f"database:{pk}",
        )
        return TableMetadataResponse(
            name=raw.get("name", parsed_table),
            columns=columns,
            foreign_keys=foreign_keys,
            indexes=indexes,
            primary_key=raw.get("primaryKey", {}),
            select_star=raw.get("selectStar"),
            comment=raw.get("comment"),
        )

    # ------------------------------------------------------------------
    # GET /{pk}/table_extra/{table_name}/{schema_name}/ — extra metadata (deprecated)
    # ------------------------------------------------------------------
    @get(
        "/{pk:int}/table_extra/{table_name:path}/{schema_name:str}/",
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
        """Get table extra metadata (deprecated path).

        Deprecated in 4.0 in favour of ``GET /{pk}/table_metadata/extra/``
        which uses query parameters and supports catalogs (SIP-95).  This
        path-based variant is kept for backward compatibility.

        Mirrors the original ``DatabaseRestApi.table_extra_metadata_deprecated``
        which uses ``@check_table_access`` for TABLE-level permission checks
        and delegates to ``database.db_engine_spec.get_extra_table_metadata()``.
        """
        from urllib.parse import unquote_plus

        await event_logger.alog_with_context(
            "database.table_extra_metadata_deprecated.init",
            object_ref=f"database:{pk}",
        )

        parsed_schema: str | None = schema_name
        if schema_name in ("null", "undefined"):
            parsed_schema = None
        else:
            parsed_schema = unquote_plus(schema_name)

        parsed_table = unquote_plus(table_name)
        if not parsed_table:
            raise CommandInvalidError("Table name undefined")

        database = await dao.find_by_id(pk)
        if not database:
            await event_logger.alog_with_context(
                "database.table_extra_metadata_deprecated.error",
                object_ref=f"database:{pk}",
            )
            raise ObjectNotFoundError("Database", pk)

        # Table-level access check — matches original @check_table_access
        table = Table(parsed_table, parsed_schema)
        try:
            await security_manager.raise_for_access(
                database=database,
                table=table,
                user=current_user,
            )
        except SupersetSecurityException as exc:
            await event_logger.alog_with_context(
                "database.table_extra_metadata_deprecated.error",
                object_ref=f"database:{pk}",
            )
            _log.warning(
                "Permission denied for user %s on table: %s schema: %s",
                current_user,
                parsed_table,
                parsed_schema,
            )
            raise ObjectNotFoundError("Table", parsed_table) from exc

        # Delegate to engine spec — matches original:
        #   database.db_engine_spec.get_extra_table_metadata(database, table)
        # The base implementation returns {}, engine-specific overrides
        # (e.g. BigQuery) return partition/clustering info.
        db_engine_spec = getattr(database, "db_engine_spec", None)
        if db_engine_spec and hasattr(db_engine_spec, "get_extra_table_metadata"):
            payload = await asyncio.to_thread(
                db_engine_spec.get_extra_table_metadata,
                database,
                table,
            )
        else:
            payload = {}

        await event_logger.alog_with_context(
            "database.table_extra_metadata_deprecated.success",
            object_ref=f"database:{pk}",
        )
        return payload

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
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
        schema_name: str = Parameter(query="schema_name", default=""),
    ) -> SelectStarResponse:
        if not _IDENTIFIER_RE.match(table_name):
            raise CommandInvalidError(f"Invalid table name: {table_name}")
        database = await dao.find_by_id(pk)
        if not database:
            raise ObjectNotFoundError("Database", pk)

        # Table-level access check — mirrors the original @check_table_access
        # decorator from superset_old/databases/decorators.py which calls
        # security_manager.can_access_table(database, Table(...))
        effective_schema = schema_name or None
        table = Table(table_name, effective_schema)
        try:
            await security_manager.raise_for_access(
                database=database,
                table=table,
                user=current_user,
            )
        except SupersetSecurityException as exc:
            raise ObjectNotFoundError("Table", table_name) from exc

        # Use engine-spec-aware SELECT * generation — matches original
        # Database.select_star → BaseEngineSpec.select_star with dialect-
        # correct quoting and optional partition probing.
        sql = await _engine_select_star(database, table)
        return SelectStarResponse(result=sql)

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
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
    ) -> SelectStarResponse:
        if not _IDENTIFIER_RE.match(table_name):
            raise CommandInvalidError(f"Invalid table name: {table_name}")
        if schema_name and not _IDENTIFIER_RE.match(schema_name):
            raise CommandInvalidError(f"Invalid schema name: {schema_name}")
        database = await dao.find_by_id(pk)
        if not database:
            raise ObjectNotFoundError("Database", pk)

        # Table-level access check — mirrors the original @check_table_access
        # decorator from superset_old/databases/decorators.py
        table = Table(table_name, schema_name or None)
        try:
            await security_manager.raise_for_access(
                database=database,
                table=table,
                user=current_user,
            )
        except SupersetSecurityException as exc:
            raise ObjectNotFoundError("Table", table_name) from exc

        # Use engine-spec-aware SELECT * generation — matches original
        # Database.select_star → BaseEngineSpec.select_star with dialect-
        # correct quoting and optional partition probing.
        table_with_schema = Table(table_name, schema_name or None)
        sql = await _engine_select_star(database, table_with_schema)
        return SelectStarResponse(result=sql)

    # ------------------------------------------------------------------
    # POST /test_connection/ — test connectivity
    # ------------------------------------------------------------------
    @post(
        "/test_connection/",
        guards=[require_permission("can_write", "Database")],
        status_code=200,
    )
    async def test_connection(
        self,
        request: Request[Any, Any, Any],
        dao: DatabaseDAOProtocol,
    ) -> dict[str, Any]:
        # Normalize the legacy ``encrypted_extra`` key -> ``masked_encrypted_extra``
        # before validation (1:1 with the original ``rename_encrypted_extra``
        # ``@pre_load`` hook) so older API clients keep working.
        data: DatabaseTestConnectionSchema = await _decode_connection_body(
            request, DatabaseTestConnectionSchema
        )
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
        status_code=200,
    )
    async def validate_sql(
        self,
        pk: int,
        data: ValidateSQLSchema,
        dao: DatabaseDAOProtocol,
    ) -> dict[str, Any]:
        # Mirrors ``superset_old/databases/api.py:validate_sql`` — the command
        # returns a list of SQL-error annotations, which the API wraps as
        # ``{"result": [...]}``.  When the engine has no validator configured
        # the command raises ``NoValidatorConfigFoundError`` (422) /
        # ``NoValidatorFoundError`` (422) rather than returning 200 — the
        # global SupersetException handler renders the SIP-40 error.
        cmd = ValidateSQLCommand(
            dao=cast("AsyncDatabaseDAO", dao),
            database_id=pk,
            sql=data.sql,
            schema=data.schema,
            catalog=getattr(data, "catalog", None),
        )
        validator_errors = await cmd.execute()
        await event_logger.alog_with_context(
            "database.validate_sql", object_ref=f"database:{pk}"
        )
        return {"result": validator_errors}

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
        rison_params: list[int] | dict[str, Any] | None,
        token: str | None = Parameter(query="token", default=None),
    ) -> Stream:
        ids = extract_ids(rison_params)
        if not ids:
            raise CommandInvalidError("At least one ID is required for export")
        # 1:1 with ``superset_old/databases/api.py:1515-1539``: build
        # ``root = f"database_export_{timestamp}"`` (timestamp =
        # ``datetime.now().strftime("%Y%m%dT%H%M%S")``), nest every ZIP entry
        # under ``f"{root}/{file_name}"``, and name the download
        # ``f"{root}.zip"``. The importer strips the root via ``remove_root``
        # (``parts[1:]``) so re-import still works — exports without a root
        # produce a flat ZIP that ``remove_root`` reduces to "." and breaks.
        from datetime import datetime as _datetime

        timestamp = _datetime.now().strftime("%Y%m%dT%H%M%S")
        root = f"database_export_{timestamp}"
        cmd = ExportDatabasesCommand(model_ids=ids, dao=cast("AsyncDatabaseDAO", dao))
        cmd._root = root  # noqa: SLF001
        buf = await cmd.execute()
        await event_logger.alog_with_context(
            "database.export", extra={"count": len(ids)}
        )
        return Stream(
            stream_zip(buf),
            status_code=200,
            media_type="application/zip",
            headers=build_export_headers(f"{root}.zip", token=token),
        )

    # ------------------------------------------------------------------
    # POST /import/ — multipart import
    # ------------------------------------------------------------------
    @post(
        "/import/",
        guards=[require_permission("can_write", "Database")],
        media_type="application/json",
        # Upstream returns 200 "OK" (databases/api.py import_); align.
        status_code=200,
    )
    async def import_database(
        self,
        dao: DatabaseDAOProtocol,
        data: UploadFile = Body(media_type=RequestEncodingType.MULTI_PART),  # noqa: B008
        overwrite: bool = False,
        passwords: str | None = None,
        ssh_tunnel_passwords: str | None = None,
        ssh_tunnel_private_keys: str | None = None,
        ssh_tunnel_private_key_passwords: str | None = None,
    ) -> dict[str, str]:
        """Import database(s) from a ZIP bundle.

        Mirrors ``superset_old/databases/api.py:import_`` (lines 1620-1662).
        Accepts four credential maps so SSH-tunnel key-pair databases can be
        re-imported without losing private-key credentials:

        - ``passwords`` — URI passwords, keyed by ``databases/<Name>.yaml``
        - ``ssh_tunnel_passwords`` — tunnel password, keyed by file name
        - ``ssh_tunnel_private_keys`` — PEM private key, keyed by file name
        - ``ssh_tunnel_private_key_passwords`` — private key passphrase
        """
        contents = await data.read()
        buf = io.BytesIO(contents)

        def _parse_json_field(raw: str | None, field_name: str) -> dict[str, str]:
            if not raw:
                return {}
            try:
                return json.loads(raw)
            except (ValueError, json.JSONDecodeError) as exc:
                raise CommandInvalidError(
                    f"Invalid JSON in '{field_name}' field"
                ) from exc

        passwords_dict = _parse_json_field(passwords, "passwords")
        ssh_dict = _parse_json_field(ssh_tunnel_passwords, "ssh_tunnel_passwords")
        ssh_private_keys = _parse_json_field(
            ssh_tunnel_private_keys, "ssh_tunnel_private_keys"
        )
        ssh_private_key_passwords = _parse_json_field(
            ssh_tunnel_private_key_passwords, "ssh_tunnel_private_key_passwords"
        )

        cmd = ImportDatabasesCommand(
            contents=buf,
            dao=cast("AsyncDatabaseDAO", dao),
            overwrite=overwrite,
            passwords=passwords_dict,
            ssh_tunnel_passwords=ssh_dict,
            ssh_tunnel_private_keys=ssh_private_keys,
            ssh_tunnel_private_key_passwords=ssh_private_key_passwords,
        )
        await cmd.execute()
        await event_logger.alog_with_context("database.import")
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
        # Delegate to Database.function_names which calls
        # db_engine_spec.get_function_names(database) — matches original
        # superset_old/databases/api.py:function_names line 1826-1828.
        names: list[str] = []
        try:
            raw = getattr(database, "function_names", None)
            if callable(raw):
                raw = raw()
            names = list(raw) if raw is not None else []
        except Exception:  # noqa: BLE001
            _log.warning(
                "Failed to get function names for database %s", pk, exc_info=True
            )
        return {"function_names": names}

    # ------------------------------------------------------------------
    # GET /available/ — available engines
    # ------------------------------------------------------------------
    @get(
        "/available/",
        guards=[require_permission("can_read", "Database")],
    )
    async def available(self) -> dict[str, Any]:
        """GET /api/v1/database/available/ — list engine specs.

        Mirrors ``superset_old/databases/api.py::available``. The
        ``preferred`` flag is True only for engines whose ``engine_name``
        appears in the ``PREFERRED_DATABASES`` config list, and the
        response is sorted so preferred engines come first — in the order
        they appear in the config — followed by the rest sorted
        alphabetically by display name.
        """
        from superset.db.engine_specs import _get_sync_spec_map, _NATIVE_SPECS

        settings_obj = SupersetSettings()  # type: ignore[call-arg]
        preferred_names: list[str] = list(
            getattr(settings_obj, "preferred_databases", [])
        )
        preferred_set: set[str] = set(preferred_names)
        preferred_index: dict[str, int] = {
            name: idx for idx, name in enumerate(preferred_names)
        }

        databases: list[dict[str, Any]] = []

        def _build_payload(
            engine_key: str,
            spec_cls: Any,
            *,
            sync_fallback: bool,
        ) -> dict[str, Any]:
            engine_name = getattr(spec_cls, "engine_name", engine_key)
            default_driver = getattr(spec_cls, "default_driver", "") or ""
            placeholder = (
                f"{engine_key}+{default_driver}://"
                if default_driver
                else f"{engine_key}://"
            )
            return {
                "name": engine_name,
                "engine": engine_key,
                "preferred": engine_name in preferred_set,
                "available_drivers": [default_driver or engine_key],
                "default_driver": default_driver,
                "sqlalchemy_uri_placeholder": placeholder,
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
                    )
                    if sync_fallback
                    else False,
                    "disable_ssh_tunneling": getattr(
                        spec_cls, "disable_ssh_tunneling", False
                    )
                    if sync_fallback
                    else False,
                    "supports_dynamic_catalog": getattr(
                        spec_cls, "supports_dynamic_catalog", False
                    ),
                    "supports_oauth2": getattr(spec_cls, "supports_oauth2", False),
                },
            }

        # 1. Native async engine specs
        for engine_key, spec_cls in _NATIVE_SPECS.items():
            if not getattr(spec_cls, "engine_name", None):
                # Skip abstract base specs with no engine_name; mirrors the
                # original "if not drivers: continue" filter.
                continue
            databases.append(_build_payload(engine_key, spec_cls, sync_fallback=False))

        # 2. Sync fallback engine specs (from superset.db_engine_specs)
        native_engines = set(_NATIVE_SPECS.keys())
        sync_specs = _get_sync_spec_map()
        for engine_key, spec_cls in sync_specs.items():
            if engine_key in native_engines:
                continue
            if not getattr(spec_cls, "engine_name", None):
                continue
            databases.append(_build_payload(engine_key, spec_cls, sync_fallback=True))

        # Sort: preferred first (in config order), then the rest alphabetically.
        # ``name`` can be ``None`` for custom specs that don't define
        # ``engine_name``; coerce to empty string for stable ordering.
        preferred = sorted(
            (db for db in databases if db["preferred"]),
            key=lambda d: preferred_index.get(d["name"] or "", len(preferred_names)),
        )
        others = sorted(
            (db for db in databases if not db["preferred"]),
            key=lambda d: d["name"] or "",
        )
        return {"databases": preferred + others}

    # ------------------------------------------------------------------
    # POST /validate_parameters/ — param validation
    # ------------------------------------------------------------------
    @post(
        "/validate_parameters/",
        guards=[require_permission("can_write", "Database")],
        status_code=200,
    )
    async def validate_parameters(
        self,
        request: Request[Any, Any, Any],
    ) -> dict[str, Any]:
        # Normalize the legacy ``encrypted_extra`` key -> ``masked_encrypted_extra``
        # before validation (1:1 with the original ``rename_encrypted_extra``
        # ``@pre_load`` hook) so older API clients keep working.
        data: DatabaseValidateParamsSchema = await _decode_connection_body(
            request, DatabaseValidateParamsSchema
        )
        cmd = ValidateParametersCommand(
            data={
                "engine": data.engine,
                "parameters": data.parameters,
                "database_name": data.database_name,
                "configuration_method": data.configuration_method,
            },
        )
        result = await cmd.execute()
        await event_logger.alog_with_context("database.validate_parameters")
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
        data: dict[str, Any] = Body(media_type=RequestEncodingType.MULTI_PART),  # noqa: B008
    ) -> dict[str, Any]:
        """Upload a CSV / Excel / Columnar file to a database table.

        Mirrors ``superset_old/databases/api.py:upload`` (lines 1728-1787).
        The original uses ``UploadPostSchema().load(request.form.to_dict())``
        to parse and coerce all form fields.  Here we use the async-compatible
        :func:`superset.utils.upload.parse_upload_form` which is the 1:1 port
        of that schema.

        Key changes vs the previous stub:
        1. All 17+ ``UploadPostSchema`` fields are now read from the multipart
           form (previously only ``table_name`` and ``schema_name`` were passed,
           and ``schema_name`` was stored under the wrong key ``"schema_name"``
           while ``UploadCommand.run()`` reads ``"schema"``).
        2. ``validate_file_extension`` is called so invalid extensions are
           rejected before any DB work is done.
        3. ``parse_upload_form`` coerces booleans, integers, and
           ``column_data_types`` JSON — previously all were silently dropped.
        """
        from superset.utils.upload import parse_upload_form, validate_file_extension

        # Extract the UploadFile object from the multipart dict.
        # Litestar puts named file fields as UploadFile instances in the dict.
        file_field: UploadFile | None = data.get("file")  # type: ignore[assignment]
        if file_field is None:
            raise CommandInvalidError("'file' field is required")

        filename = file_field.filename or ""
        if not validate_file_extension(filename):
            raise CommandInvalidError(
                f"Invalid file extension for '{filename}'. "
                "Allowed: csv, tsv, xls, xlsx, parquet, zip"
            )

        file_contents = await file_field.read()

        # Build a plain str→str dict of the non-file form fields so
        # parse_upload_form can coerce them the same way Marshmallow did.
        form_dict: dict[str, Any] = {
            k: v for k, v in data.items() if k != "file"
        }

        # parse_upload_form mirrors UploadPostSchema field-by-field:
        # bools, ints, delimited lists, column_data_types JSON.
        # The canonical field name for the target schema is "schema"
        # (UploadPostSchema line 1122 in superset_old/databases/schemas.py).
        parsed = parse_upload_form(form_dict)

        cmd = UploadCommand(
            dao=cast("AsyncDatabaseDAO", dao),
            database_id=pk,
            data=parsed,
            file_contents=file_contents,
        )
        result = await cmd.execute()
        await event_logger.alog_with_context(
            "database.upload", object_ref=f"database:{pk}"
        )
        return result

    # ------------------------------------------------------------------
    # POST /upload_metadata/ — upload metadata
    # ------------------------------------------------------------------
    @post(
        "/upload_metadata/",
        # The original ``constants.MODEL_API_RW_METHOD_PERMISSION_MAP`` maps
        # ``upload_metadata`` to ``upload`` (not ``write``) — mirror that so
        # users with ``can_upload`` but not ``can_write`` retain access.
        guards=[require_permission("can_upload", "Database")],
        media_type="application/json",
    )
    async def upload_metadata(
        self,
        data: UploadFile = Body(media_type=RequestEncodingType.MULTI_PART),  # noqa: B008
        type: str = Parameter(query="type", default="csv"),  # noqa: A002, B008
        delimiter: str = Parameter(query="delimiter", default=","),  # noqa: B008
        header_row: int = Parameter(query="header_row", default=0),  # noqa: B008
    ) -> dict[str, Any]:
        """Upload a file and return file metadata (column names per sheet).

        Mirrors ``superset_old/databases/api.py:upload_metadata`` (lines
        1704-1718) verbatim: parse the ``type`` / ``delimiter`` / ``header_row``
        options (``UploadFileMetadataPostSchema``), instantiate the matching
        per-format reader and delegate to its ``file_metadata(file)``. The
        readers honour every option the original passed (delimiter, header_row)
        and apply identical parsing/error semantics — previously this endpoint
        used an inline parser that silently dropped reader options.

        Supported file types (via the ``type`` query parameter):
        - ``csv``  -- comma/delimiter-separated values
        - ``excel`` -- .xlsx / .xls
        - ``columnar`` -- Apache Parquet (single file or ZIP of Parquet files)
        """
        from superset.commands.database.uploaders.base import (
            BaseDataReader,
            UploadFileType,
        )
        from superset.commands.database.uploaders.columnar_reader import ColumnarReader
        from superset.commands.database.uploaders.csv_reader import CSVReader
        from superset.commands.database.uploaders.excel_reader import ExcelReader

        # Mirror ``UploadFileMetadataPostSchema``: only ``delimiter`` and
        # ``header_row`` are forwarded into the reader options.
        options: dict[str, Any] = {
            "delimiter": delimiter,
            "header_row": header_row,
        }

        file_type = type.lower()
        reader: BaseDataReader
        if file_type == UploadFileType.CSV.value:
            reader = CSVReader(options)  # type: ignore[arg-type]
        elif file_type == UploadFileType.EXCEL.value:
            reader = ExcelReader(options)  # type: ignore[arg-type]
        elif file_type == UploadFileType.COLUMNAR.value:
            reader = ColumnarReader(options)  # type: ignore[arg-type]
        else:
            return Response(  # type: ignore[return-value]
                content={"message": f"Unsupported file type: {type}"},
                status_code=400,
            )

        # The readers accept Litestar ``UploadFile`` directly (normalised via
        # ``_to_stream``); the columnar reader additionally reads ``filename``
        # for ZIP/extension detection — also present on ``UploadFile``.
        metadata = await asyncio.to_thread(reader.file_metadata, data)
        result = FileMetadataResponse(
            items=[
                FileMetadataItem(
                    column_names=item.get("column_names", []),
                    sheet_name=item.get("sheet_name"),
                )
                for item in metadata.get("items", [])
            ]
        )
        return {"result": msgspec.to_builtins(result)}

    @get(
        "/oauth2/",
        dependencies={
            "oauth2_dao": Provide(
                provide_database_user_oauth2_tokens_dao, sync_to_thread=False
            ),
        },
        # No auth guard: the OAuth2 provider redirects the user's browser
        # back here with no Superset session cookie attached.  Identity is
        # carried inside the signed ``state`` parameter (validated by
        # :func:`decode_oauth2_state`).  Mirrors original ``oauth2()`` in
        # ``superset_old/databases/api.py`` which is registered without any
        # ``@protect()`` decorator.
    )
    async def oauth2(
        self,
        oauth2_dao: Any,
        oauth_state: str = Parameter(query="state", default=""),
        code: str = Parameter(query="code", default=""),
        oauth_scope: str = Parameter(query="scope", default=""),  # noqa: ARG002
        error: str = Parameter(query="error", default=""),
    ) -> "Response[Any]":
        """GET /api/v1/database/oauth2/ — OAuth2 provider redirect.

        Exchanges the authorization ``code`` for access/refresh tokens via
        :class:`OAuth2StoreTokenCommand`, persists them in
        ``database_user_oauth2_tokens``, and renders a self-closing HTML
        page that notifies the opener tab.

        Mirrors ``superset_old/databases/api.py:oauth2`` (lines 1413-1469).
        """
        from superset.commands.database.oauth2 import OAuth2StoreTokenCommand
        from superset.exceptions import OAuth2Error
        from superset.utils.oauth2 import decode_oauth2_state

        if not oauth_state:
            return Response(
                content={
                    "message": (
                        "OAuth2 endpoint. Provide 'state' and 'code' query parameters."
                    )
                },
                status_code=200,
            )

        # Run the store-token command — exchanges the code for tokens and
        # writes them to ``database_user_oauth2_tokens``.
        parameters = {"state": oauth_state, "code": code, "error": error}
        try:
            command = OAuth2StoreTokenCommand(oauth2_dao, parameters)
            await command.execute()
        except OAuth2Error as ex:
            _log.warning("OAuth2 token exchange failed: %s", ex)
            return Response(
                content=f"<html><body>OAuth2 error: {ex.message}</body></html>",
                status_code=400,
                media_type="text/html",
            )
        except ObjectNotFoundError:
            return Response(
                content="<html><body>Database not found</body></html>",
                status_code=404,
                media_type="text/html",
            )

        # Decode the state again so we can render the close-the-window
        # template with the originating tab_id.  At this point the state
        # has already been validated by the command above, so this cannot
        # fail in practice.
        decoded = decode_oauth2_state(oauth_state)
        tab_id = decoded.get("tab_id", "")

        # Render the self-closing HTML page that notifies the opener tab,
        # then closes itself.  1:1 with
        # ``superset_old/templates/superset/oauth2.html`` — the frontend
        # listens for the ``{ tabId }`` ``postMessage`` payload to re-run
        # the original query, so the byte shape of the script must match
        # the original exactly.
        return Template(
            template_name="superset/oauth2.html",
            context={"tab_id": tab_id},
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
        await event_logger.alog_with_context(
            "database.delete_ssh_tunnel", object_ref=f"database:{pk}"
        )
        return {"message": "OK"}
