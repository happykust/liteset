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

import io
from typing import Any

from litestar import Controller, delete, get, post, put
from litestar.datastructures import UploadFile
from litestar.di import Provide
from litestar.enums import RequestEncodingType
from litestar.params import Body
from litestar.response import Stream

from liteset.commands.query import (
    BulkDeleteSavedQueriesCommand,
    CreateSavedQueryCommand,
    DeleteSavedQueryCommand,
    ExportSavedQueriesCommand,
    ImportSavedQueriesCommand,
    UpdateSavedQueryCommand,
)
from liteset.controllers.base import (
    extract_ids,
    extract_ids_required,
    extract_pagination,
    get_distinct_payload,
    get_info_payload,
    get_related_payload,
    serialize_list_response,
    stream_zip,
)
from liteset.exceptions import CommandInvalidError, ObjectNotFoundError
from liteset.guards.rbac import require_permission
from liteset.params.rison import provide_rison_query
from liteset.providers import provide_saved_query_dao
from liteset.schemas.query import (
    SavedQueryGetResponse,
    SavedQueryPostSchema,
    SavedQueryPutSchema,
)
from liteset.typing import CRUDDAOProtocol, SecurityManagerProtocol, UserProtocol
from liteset.events import event_logger
from liteset.utils import filter_unset


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
        from liteset.db.filters import saved_query_access_filters

        page, page_size = extract_pagination(rison_params)
        base_filters = await saved_query_access_filters(
            security_manager, current_user
        )
        queries = await dao.find_all(
            filters=base_filters or None, page=page, page_size=page_size
        )
        total = await dao.count(filters=base_filters or None)
        event_logger.log("saved_query.list", user_id=current_user.id)
        return serialize_list_response(
            queries,
            total,
            ["id", "label", "schema", "sql", "db_id", "description"],
        )

    @get(
        "/_info",
        guards=[require_permission("can_read", "SavedQuery")],
    )
    async def info(self, dao: CRUDDAOProtocol) -> dict[str, Any]:
        """GET /api/v1/saved_query/_info — API metadata for frontend."""
        return await get_info_payload(
            dao=dao,
            model_name="SavedQuery",
            permissions=["can_read", "can_write"],
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
        from liteset.db.filters import saved_query_access_filters

        base_filters = await saved_query_access_filters(
            security_manager, current_user
        )
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
        from liteset.db.filters import saved_query_access_filters

        base_filters = await saved_query_access_filters(
            security_manager, current_user
        )
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
        query = await dao.find_by_id(pk)
        if not query:
            raise ObjectNotFoundError("SavedQuery", pk)
        # Verify object-level access
        from liteset.db.filters import saved_query_access_filters

        base_filters = await saved_query_access_filters(
            security_manager, current_user
        )
        if base_filters:
            from sqlalchemy import select as sa_select

            model_cls = getattr(dao, "model_cls", None)
            if model_cls is not None:
                stmt = sa_select(model_cls.id).where(
                    model_cls.id == query.id, *base_filters
                )
                result = await dao.session.scalar(stmt)
                if result is None:
                    raise ObjectNotFoundError("SavedQuery", pk)
        return SavedQueryGetResponse(
            id=query.id,
            result={
                "label": getattr(query, "label", ""),
                "schema": getattr(query, "schema", None),
                "sql": getattr(query, "sql", ""),
                "db_id": getattr(query, "db_id", None),
                "description": getattr(query, "description", None),
                "template_params": getattr(query, "template_params", None),
            },
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
            dao=dao,
            data={
                "label": data.label,
                "sql": data.sql,
                "db_id": data.db_id,
                "schema": data.schema,
                "description": data.description,
                "template_params": data.template_params,
                "catalog": data.catalog,
            },
            user_id=current_user.id,
        )
        query = await cmd.execute()
        event_logger.log("saved_query.create", object_ref=f"saved_query:{query.id}", user_id=current_user.id)
        return SavedQueryGetResponse(
            id=query.id,
            result={"label": query.label, "sql": query.sql},
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
                "template_params": data.template_params,
                "catalog": data.catalog,
            }
        )
        cmd = UpdateSavedQueryCommand(
            dao=dao,
            query_id=pk,
            data=update_data,
            user_id=current_user.id,
        )
        query = await cmd.execute()
        event_logger.log("saved_query.update", object_ref=f"saved_query:{pk}", user_id=current_user.id)
        return SavedQueryGetResponse(
            id=query.id,
            result={"label": query.label, "sql": query.sql},
        )

    @delete(
        "/{pk:int}",
        guards=[require_permission("can_write", "SavedQuery")],
        status_code=200,
    )
    async def delete_saved_query(self, pk: int, dao: CRUDDAOProtocol) -> dict[str, str]:
        """DELETE /api/v1/saved_query/<pk> — delete a single saved query."""
        cmd = DeleteSavedQueryCommand(dao=dao, query_id=pk)
        await cmd.execute()
        event_logger.log("saved_query.delete", object_ref=f"saved_query:{pk}")
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
        rison_params: dict[str, Any] | None,
    ) -> dict[str, str]:
        ids = extract_ids_required(rison_params)
        cmd = BulkDeleteSavedQueriesCommand(
            dao=dao,
            ids=ids,
            security_manager=security_manager,
            user_id=current_user.id,
        )
        await cmd.execute()
        event_logger.log("saved_query.bulk_delete", extra={"count": len(ids)})
        return {"message": "OK"}

    @get(
        "/export/",
        guards=[require_permission("can_read", "SavedQuery")],
        media_type="application/zip",
    )
    async def export(
        self, dao: CRUDDAOProtocol, rison_params: dict[str, Any] | None
    ) -> Stream:
        ids = extract_ids(rison_params)
        if not ids:
            raise CommandInvalidError("At least one ID is required for export")
        cmd = ExportSavedQueriesCommand(model_ids=ids, dao=dao)
        buf = await cmd.execute()
        event_logger.log("saved_query.export", extra={"count": len(ids)})
        return Stream(
            stream_zip(buf),
            status_code=200,
            media_type="application/zip",
            headers={
                "Content-Disposition": "attachment; filename=saved_queries_export.zip"
            },
        )

    @post(
        "/import/",
        guards=[require_permission("can_write", "SavedQuery")],
    )
    async def import_queries(
        self,
        dao: CRUDDAOProtocol,
        data: UploadFile = Body(media_type=RequestEncodingType.MULTI_PART),  # noqa: B008
        overwrite: bool = False,
        passwords: str | None = None,
        ssh_tunnel_passwords: str | None = None,
    ) -> dict[str, str]:
        import json as _json

        contents = await data.read()
        buf = io.BytesIO(contents)
        try:
            passwords_dict: dict[str, str] = _json.loads(passwords) if passwords else {}
        except (ValueError, _json.JSONDecodeError):
            raise CommandInvalidError("Invalid JSON in 'passwords' field")
        try:
            ssh_dict: dict[str, str] = (
                _json.loads(ssh_tunnel_passwords) if ssh_tunnel_passwords else {}
            )
        except (ValueError, _json.JSONDecodeError):
            raise CommandInvalidError("Invalid JSON in 'ssh_tunnel_passwords' field")
        cmd = ImportSavedQueriesCommand(
            contents=buf,
            dao=dao,
            overwrite=overwrite,
            passwords=passwords_dict,
            ssh_tunnel_passwords=ssh_dict,
        )
        await cmd.execute()
        event_logger.log("saved_query.import")
        return {"message": "OK"}
