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
"""SavedQuery controller — CRUD + bulk delete, export, import."""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast, TYPE_CHECKING

from litestar import Controller, delete, get, post, put, Request
from litestar.di import Provide
from litestar.params import Parameter
from litestar.response import Stream

from superset.commands.query.create import CreateSavedQueryCommand
from superset.commands.query.delete import (
    BulkDeleteSavedQueriesCommand,
    DeleteSavedQueryCommand,
)
from superset.commands.query.export import ExportSavedQueriesCommand
from superset.commands.query.importers.v1 import ImportSavedQueriesCommand
from superset.commands.query.update import UpdateSavedQueryCommand
from superset.controllers.base import (
    build_export_headers,
    build_rison_query_params,
    extract_ids,
    extract_ids_required,
    get_distinct_payload,
    get_info_payload,
    get_related_payload,
    parse_import_request,
    serialize_list_response,
    stream_zip,
)
from superset.events import event_logger
from superset.exceptions import CommandInvalidError, ObjectNotFoundError
from superset.guards.rbac import require_permission
from superset.params.rison import provide_rison_query
from superset.providers import provide_saved_query_dao
from superset.schemas.query import (
    SavedQueryDetailResult,
    SavedQueryGetResponse,
    SavedQueryPostSchema,
    SavedQueryPutSchema,
)
from superset.typing import CRUDDAOProtocol, SecurityManagerProtocol, UserProtocol
from superset.utils import filter_unset

if TYPE_CHECKING:
    from superset.db.daos.query import AsyncSavedQueryDAO


def _filter_is_fav(current_user: Any, model_cls: Any, value: Any) -> Any:
    from sqlalchemy import select as sa_select

    from superset.models.core import FavStar

    user_id = getattr(current_user, "id", None)
    if user_id is None:
        return None
    fav_subq = sa_select(FavStar.obj_id).where(
        FavStar.class_name == "query",
        FavStar.user_id == user_id,
    )
    if value:
        return model_cls.id.in_(fav_subq)
    return ~model_cls.id.in_(fav_subq)


def _filter_all_text(model_cls: Any, value: Any) -> Any:
    """Full-text OR-search across schema, label, description and sql."""
    if not value:
        return None
    from sqlalchemy import or_

    ilike_value = f"%{value}%"
    return or_(
        model_cls.schema.ilike(ilike_value),
        model_cls.label.ilike(ilike_value),
        model_cls.description.ilike(ilike_value),
        model_cls.sql.ilike(ilike_value),
    )


def _filter_tags(model_cls: Any, value: Any) -> Any:
    """Filter saved queries by tag name (substring match).

    Joins via the ``TaggedObject`` association table so this works across
    both the legacy M2M via ``SavedQuery.tags`` and the newer
    ``TaggedObject`` approach.
    """
    if not value:
        return None
    from sqlalchemy import select as sa_select

    from superset.models.tags import Tag, TaggedObject

    ilike_value = f"%{value}%"
    tag_id_subq = sa_select(Tag.id).where(Tag.name.ilike(ilike_value))
    tagged_subq = sa_select(TaggedObject.object_id).where(
        TaggedObject.object_type == "query",
        TaggedObject.tag_id.in_(tag_id_subq),
    )
    return model_cls.id.in_(tagged_subq)


def _filter_tag_id(model_cls: Any, value: Any) -> Any:
    """Filter saved queries by tag ID."""
    if value is None:
        return None
    from sqlalchemy import select as sa_select

    from superset.models.tags import TaggedObject

    try:
        tag_id_int = int(value)
    except (TypeError, ValueError):
        return None
    tagged_subq = sa_select(TaggedObject.object_id).where(
        TaggedObject.object_type == "query",
        TaggedObject.tag_id == tag_id_int,
    )
    return model_cls.id.in_(tagged_subq)


def _saved_query_custom_filters(current_user: Any) -> dict[str, Any]:
    return {
        "saved_query_is_fav": lambda m, v: _filter_is_fav(current_user, m, v),
        "all_text": _filter_all_text,
        "saved_query_tags": _filter_tags,
        "saved_query_tag_id": _filter_tag_id,
    }


def _saved_query_sql_tables(query: Any) -> list[dict[str, Any]]:
    """Best-effort extraction of SQL tables referenced by a saved query.

    Parses SQL via Jinja + sqlglot and returns the referenced tables, falling
    back to ``[]`` on any parse/security/template error. Serialises each
    ``Table`` dataclass to ``{table, schema, catalog}`` matching the JSON
    shape the frontend expects.
    """
    sql = getattr(query, "sql", None)
    database = getattr(query, "database", None)
    if not sql or database is None:
        return []
    try:
        from jinja2.exceptions import TemplateError

        from superset.exceptions import SupersetParseError, SupersetSecurityException
        from superset.sql.parse import process_jinja_sql

        tables = process_jinja_sql(sql, database).tables
    except (SupersetSecurityException, SupersetParseError, TemplateError):
        return []
    return [
        {"table": t.table, "schema": t.schema, "catalog": t.catalog} for t in tables
    ]


class SavedQueryController(Controller):
    path = "/api/v1/saved_query"
    tags = ["Saved Queries"]
    dependencies = {
        "dao": Provide(provide_saved_query_dao, sync_to_thread=False),
        "rison_params": Provide(provide_rison_query),
    }

    @get(
        "/",
        guards=[require_permission("can_read", "SavedQuery")],
    )
    async def get_list(
        self,
        dao: CRUDDAOProtocol,
        rison_params: dict[str, Any] | None,
        current_user: UserProtocol,
        security_manager: Any,
    ) -> dict[str, Any]:
        """GET /api/v1/saved_query/ — list saved queries with optional pagination."""
        from sqlalchemy.orm import selectinload

        from superset.db.filters import saved_query_access_filters
        from superset.models.sql_lab import SavedQuery

        rison_filters, order_by, page, page_size = build_rison_query_params(
            SavedQuery,
            rison_params,
            custom_filters=_saved_query_custom_filters(current_user),
        )
        if not order_by:
            order_by = [SavedQuery.changed_on.desc()]

        base_filters = await saved_query_access_filters(security_manager, current_user)
        all_filters = (base_filters or []) + rison_filters

        queries = await dao.find_all(
            filters=all_filters or None,
            page=page,
            page_size=page_size,
            order_by=order_by,
            options=[
                selectinload(SavedQuery.changed_by),
                selectinload(SavedQuery.created_by),
                selectinload(SavedQuery.database),
                selectinload(SavedQuery.tags),
            ],
        )
        total = await dao.count(filters=all_filters or None)
        await event_logger.alog_with_context(
            "saved_query.list", user_id=current_user.id
        )
        payload = serialize_list_response(
            queries,
            total,
            [
                "id",
                "uuid",
                "label",
                "schema",
                "sql",
                "db_id",
                "description",
                "extra",
                "catalog",
                "rows",
                "created_on",
                "created_on_delta_humanized",
                "changed_on",
                "changed_on_delta_humanized",
                "changed_on_utc",
                "database.database_name",
                "database.id",
                "changed_by.first_name",
                "changed_by.id",
                "changed_by.last_name",
                "created_by.first_name",
                "created_by.id",
                "created_by.last_name",
                "tags.id",
                "tags.name",
                "tags.type",
            ],
            list_title="List Saved Query",
        )
        # Post-process: add computed properties the frontend expects.
        from datetime import datetime as _dt

        import humanize as _humanize

        now = _dt.now()
        last_run_map: dict[int, Any] = {}
        for q in queries:
            last_run_map[q.id] = getattr(q, "last_run", None)

        for row, q in zip(payload.get("result", []), queries, strict=True):
            last_run = last_run_map.get(row.get("id"))
            row["last_run_delta_humanized"] = (
                _humanize.naturaltime(now - last_run) if last_run else ""
            )
            row["sql_tables"] = _saved_query_sql_tables(q)
        return payload

    @get(
        "/_info",
        guards=[require_permission("can_read", "SavedQuery")],
    )
    async def info(
        self,
        dao: CRUDDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
    ) -> dict[str, Any]:
        """GET /api/v1/saved_query/_info — API metadata for frontend."""
        return await get_info_payload(
            dao=dao,
            model_name="SavedQuery",
            permissions=["can_read", "can_write", "can_export"],
            security_manager=security_manager,
            current_user=current_user,
            class_permission_name="SavedQuery",
        )

    @get(
        "/related/{column_name:str}",
        guards=[require_permission("can_read", "SavedQuery")],
    )
    async def related(
        self,
        column_name: str,
        dao: CRUDDAOProtocol,
        security_manager: Any,
        current_user: UserProtocol,
        rison_params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """GET /api/v1/saved_query/related/{column_name}"""
        from superset.db.filters import saved_query_access_filters

        base_filters = await saved_query_access_filters(security_manager, current_user)
        return await get_related_payload(
            dao=dao,
            column_name=column_name,
            rison_params=rison_params,
            allowed_fields=frozenset({"database", "changed_by", "created_by"}),
            base_filters=base_filters or None,
        )

    @get(
        "/distinct/{column_name:str}",
        guards=[require_permission("can_read", "SavedQuery")],
    )
    async def distinct(
        self,
        column_name: str,
        dao: CRUDDAOProtocol,
        security_manager: Any,
        current_user: UserProtocol,
        rison_params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """GET /api/v1/saved_query/distinct/{column_name}"""
        from superset.db.filters import saved_query_access_filters

        base_filters = await saved_query_access_filters(security_manager, current_user)
        return await get_distinct_payload(
            dao=dao,
            column_name=column_name,
            rison_params=rison_params,
            allowed_fields=frozenset({"catalog", "schema"}),
            base_filters=base_filters or None,
        )

    @get(
        "/{pk:int}",
        guards=[require_permission("can_read", "SavedQuery")],
    )
    async def get_saved_query(
        self,
        pk: int,
        dao: CRUDDAOProtocol,
        security_manager: Any,
        current_user: UserProtocol,
    ) -> SavedQueryGetResponse:
        """GET /api/v1/saved_query/<pk> — get a single saved query."""
        from sqlalchemy.orm import selectinload

        from superset.models.sql_lab import SavedQuery

        results = await dao.find_all(
            filters=[SavedQuery.id == pk],
            page=0,
            page_size=1,
            options=[
                selectinload(SavedQuery.changed_by),
                selectinload(SavedQuery.created_by),
                selectinload(SavedQuery.database),
            ],
        )
        if not results:
            raise ObjectNotFoundError("SavedQuery", pk)
        query = results[0]
        # Verify object-level access
        from superset.db.filters import saved_query_access_filters

        base_filters = await saved_query_access_filters(security_manager, current_user)
        if base_filters:
            accessible = await dao.count(
                filters=[SavedQuery.id == query.id, *base_filters]
            )
            if not accessible:
                raise ObjectNotFoundError("SavedQuery", pk)
        return SavedQueryGetResponse(
            id=query.id,
            result=SavedQueryDetailResult.from_model(
                query,
                sql_tables=_saved_query_sql_tables(query),
            ),
        )

    @post(
        "/",
        guards=[require_permission("can_write", "SavedQuery")],
        status_code=201,
    )
    async def create(
        self,
        data: SavedQueryPostSchema,
        dao: CRUDDAOProtocol,
        current_user: UserProtocol,
    ) -> SavedQueryGetResponse:
        """POST /api/v1/saved_query/ — create a saved query."""
        cmd = CreateSavedQueryCommand(
            dao=cast("AsyncSavedQueryDAO", dao),
            data={
                "label": data.label,
                "sql": data.sql,
                "db_id": data.db_id,
                "schema": data.schema,
                "description": data.description,
                "template_parameters": data.template_parameters,
                "extra_json": data.extra_json,
                "catalog": data.catalog,
            },
            user_id=current_user.id,
        )
        query = await cmd.execute()
        await event_logger.alog_with_context(
            "saved_query.create",
            object_ref=f"saved_query:{query.id}",
            user_id=current_user.id,
        )
        # Full show_columns representation — eager-load the relationships
        # that from_model reads synchronously.
        await cast("AsyncSavedQueryDAO", dao).session.refresh(
            query, ["changed_by", "created_by", "database"]
        )
        return SavedQueryGetResponse(
            id=int(query.id),
            result=SavedQueryDetailResult.from_model(
                query,
                sql_tables=_saved_query_sql_tables(query),
            ),
        )

    @put(
        "/{pk:int}",
        guards=[require_permission("can_write", "SavedQuery")],
    )
    async def update(
        self,
        pk: int,
        data: SavedQueryPutSchema,
        dao: CRUDDAOProtocol,
        security_manager: Any,
        current_user: UserProtocol,
    ) -> SavedQueryGetResponse:
        """PUT /api/v1/saved_query/<pk> — update a saved query."""
        update_data = filter_unset(
            {
                "label": data.label,
                "sql": data.sql,
                "db_id": data.db_id,
                "schema": data.schema,
                "description": data.description,
                "template_parameters": data.template_parameters,
                "extra_json": data.extra_json,
                "catalog": data.catalog,
            }
        )
        cmd = UpdateSavedQueryCommand(
            dao=cast("AsyncSavedQueryDAO", dao),
            query_id=pk,
            data=update_data,
            user_id=current_user.id,
            security_manager=security_manager,
            user=current_user,
        )
        query = await cmd.execute()
        await event_logger.alog_with_context(
            "saved_query.update",
            object_ref=f"saved_query:{pk}",
            user_id=current_user.id,
        )
        # Full show_columns representation — see create() above.
        await cast("AsyncSavedQueryDAO", dao).session.refresh(
            query, ["changed_by", "created_by", "database"]
        )
        return SavedQueryGetResponse(
            id=int(query.id),
            result=SavedQueryDetailResult.from_model(
                query,
                sql_tables=_saved_query_sql_tables(query),
            ),
        )

    @delete(
        "/{pk:int}",
        guards=[require_permission("can_write", "SavedQuery")],
        status_code=200,
    )
    async def delete_saved_query(
        self,
        pk: int,
        dao: CRUDDAOProtocol,
        security_manager: Any,
        current_user: UserProtocol,
    ) -> dict[str, str]:
        """DELETE /api/v1/saved_query/<pk> — delete a single saved query."""
        cmd = DeleteSavedQueryCommand(
            dao=cast("AsyncSavedQueryDAO", dao),
            query_id=pk,
            security_manager=security_manager,
            user=current_user,
        )
        await cmd.execute()
        await event_logger.alog_with_context(
            "saved_query.delete", object_ref=f"saved_query:{pk}"
        )
        return {"message": "OK"}

    @delete(
        "/",
        guards=[require_permission("can_write", "SavedQuery")],
        status_code=200,
    )
    async def bulk_delete(
        self,
        dao: CRUDDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
        rison_params: list[int] | dict[str, Any] | None,
    ) -> dict[str, str]:
        ids = extract_ids_required(rison_params)
        cmd = BulkDeleteSavedQueriesCommand(
            dao=cast("AsyncSavedQueryDAO", dao),
            ids=ids,
            security_manager=security_manager,
            user_id=current_user.id,
        )
        await cmd.execute()
        await event_logger.alog_with_context(
            "saved_query.bulk_delete", extra={"count": len(ids)}
        )
        # Locale-aware ngettext keyed on len(item_ids).
        from superset.i18n import ngettext

        msg = ngettext(
            "Deleted %(num)d saved query",
            "Deleted %(num)d saved queries",
            num=len(ids),
        )
        return {"message": msg}

    @get(
        "/export/",
        guards=[require_permission("can_export", "SavedQuery")],
        media_type="application/zip",
    )
    async def export(
        self,
        dao: CRUDDAOProtocol,
        security_manager: Any,
        current_user: UserProtocol,
        rison_params: list[int] | dict[str, Any] | None,
        token: str | None = Parameter(query="token", default=None),
    ) -> Stream:
        ids = extract_ids(rison_params)
        if not ids:
            raise CommandInvalidError("At least one ID is required for export")
        # Root dir: ``saved_query_export_{timestamp}``; entries written as
        # ``{root}/{file_name}``; the importer's ``remove_root`` strips it on re-import.
        timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        root = f"saved_query_export_{timestamp}"
        cmd = ExportSavedQueriesCommand(
            model_ids=ids,
            dao=dao,
            security_manager=security_manager,
            user=current_user,
        )
        cmd._root = root  # noqa: SLF001
        buf = await cmd.execute()
        await event_logger.alog_with_context(
            "saved_query.export", extra={"count": len(ids)}
        )
        return Stream(
            stream_zip(buf),
            status_code=200,
            media_type="application/zip",
            headers=build_export_headers(f"{root}.zip", token=token),
        )

    @post(
        "/import/",
        guards=[require_permission("can_write", "SavedQuery")],
        status_code=200,
    )
    async def import_queries(
        self,
        request: Request[Any, Any, Any],
        dao: CRUDDAOProtocol,
        security_manager: SecurityManagerProtocol,
    ) -> dict[str, str]:
        # Read the multipart body manually (see parse_import_request): the
        # ``data: UploadFile = Body(MULTI_PART)`` injection 500'd when no file
        # field was present (Litestar StopIteration). Missing upload → 4xx.
        (
            buf,
            _filename,
            overwrite,
            passwords_dict,
            ssh_dict,
            ssh_private_keys_dict,
            ssh_private_key_passwords_dict,
        ) = await parse_import_request(request)
        cmd = ImportSavedQueriesCommand(
            contents=buf,
            dao=dao,
            security_manager=security_manager,
            overwrite=overwrite,
            passwords=passwords_dict,
            ssh_tunnel_passwords=ssh_dict,
            ssh_tunnel_private_keys=ssh_private_keys_dict,
            ssh_tunnel_private_key_passwords=ssh_private_key_passwords_dict,
        )
        await cmd.execute()
        await event_logger.alog_with_context("saved_query.import")
        return {"message": "OK"}
