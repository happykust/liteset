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
"""Row Level Security controller — CRUD endpoints for RLS filters."""

from __future__ import annotations

from typing import Any

from litestar import Controller, delete, get, post, put
from litestar.di import Provide

from superset.commands.rls import (
    BulkDeleteRLSCommand,
    CreateRLSCommand,
    DeleteRLSCommand,
    UpdateRLSCommand,
)
from superset.controllers.base import (
    extract_ids_required,
    extract_pagination,
    serialize_list_response,
)
from superset.events import event_logger
from superset.exceptions import ObjectNotFoundError
from superset.guards.rbac import require_permission
from superset.params.rison import provide_rison_query
from superset.providers import provide_rls_dao
from superset.schemas.rls import RLSPostSchema, RLSPutSchema
from superset.typing import CRUDDAOProtocol
from superset.utils import filter_unset


class RLSController(Controller):
    path = "/api/v1/rowlevelsecurity"
    tags = ["Row Level Security"]
    dependencies = {
        "dao": Provide(provide_rls_dao, sync_to_thread=False),
        "rison_params": Provide(provide_rison_query),
    }

    # ------------------------------------------------------------------
    # GET — list RLS filters
    # ------------------------------------------------------------------
    @get(
        "/",
        guards=[require_permission("can_read", "RowLevelSecurity")],
    )
    async def get_list(
        self,
        dao: CRUDDAOProtocol,
        rison_params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """GET /api/v1/rowlevelsecurity/ — list RLS filters."""
        page, page_size = extract_pagination(rison_params)
        items = await dao.find_all(page=page, page_size=page_size)
        total = await dao.count()
        event_logger.log("rls.list")
        return serialize_list_response(
            items, total, ["id", "name", "filter_type", "clause", "group_key"]
        )

    # ------------------------------------------------------------------
    # GET — single RLS filter
    # ------------------------------------------------------------------
    @get(
        "/{pk:int}",
        guards=[require_permission("can_read", "RowLevelSecurity")],
    )
    async def get_single(
        self,
        dao: CRUDDAOProtocol,
        pk: int,
    ) -> dict[str, Any]:
        """GET /api/v1/rowlevelsecurity/{pk} — get a single RLS filter."""
        item = await dao.find_by_id(pk)
        if item is None:
            raise ObjectNotFoundError("RowLevelSecurityFilter", pk)
        event_logger.log("rls.show", object_ref=str(pk))
        return {"id": pk, "result": item}

    # ------------------------------------------------------------------
    # POST — create RLS filter
    # ------------------------------------------------------------------
    @post(
        "/",
        guards=[require_permission("can_write", "RowLevelSecurity")],
    )
    async def create(
        self,
        dao: CRUDDAOProtocol,
        data: RLSPostSchema,
    ) -> dict[str, Any]:
        """POST /api/v1/rowlevelsecurity/ — create a new RLS filter."""
        payload = msgspec_to_dict(data)
        cmd = CreateRLSCommand(dao=dao, data=payload)
        item = await cmd.execute()
        event_logger.log("rls.create")
        return {"id": getattr(item, "id", None), "result": item}

    # ------------------------------------------------------------------
    # PUT — update RLS filter
    # ------------------------------------------------------------------
    @put(
        "/{pk:int}",
        guards=[require_permission("can_write", "RowLevelSecurity")],
    )
    async def update(
        self,
        dao: CRUDDAOProtocol,
        pk: int,
        data: RLSPutSchema,
    ) -> dict[str, Any]:
        """PUT /api/v1/rowlevelsecurity/{pk} — update an RLS filter."""
        payload = filter_unset(msgspec_to_dict(data))
        cmd = UpdateRLSCommand(dao=dao, pk=pk, data=payload)
        item = await cmd.execute()
        event_logger.log("rls.update", object_ref=str(pk))
        return {"id": pk, "result": item}

    # ------------------------------------------------------------------
    # DELETE — single RLS filter
    # ------------------------------------------------------------------
    @delete(
        "/{pk:int}",
        guards=[require_permission("can_write", "RowLevelSecurity")],
        status_code=200,
    )
    async def delete_single(
        self,
        dao: CRUDDAOProtocol,
        pk: int,
    ) -> dict[str, str]:
        """DELETE /api/v1/rowlevelsecurity/{pk} — delete an RLS filter."""
        cmd = DeleteRLSCommand(dao=dao, pk=pk)
        await cmd.execute()
        event_logger.log("rls.delete", object_ref=str(pk))
        return {"message": "OK"}

    # ------------------------------------------------------------------
    # DELETE — bulk delete RLS filters
    # ------------------------------------------------------------------
    @delete(
        "/",
        guards=[require_permission("can_write", "RowLevelSecurity")],
        status_code=200,
    )
    async def bulk_delete(
        self,
        dao: CRUDDAOProtocol,
        rison_params: dict[str, Any] | None,
    ) -> dict[str, str]:
        """DELETE /api/v1/rowlevelsecurity/ — bulk delete RLS filters."""
        ids = extract_ids_required(rison_params)
        cmd = BulkDeleteRLSCommand(dao=dao, ids=ids)
        deleted = await cmd.execute()
        event_logger.log("rls.bulk_delete", extra={"count": deleted})
        return {"message": f"Deleted {deleted} filters"}


def msgspec_to_dict(obj: Any) -> dict[str, Any]:
    """Convert a msgspec Struct to a plain dict."""

    return {
        f: getattr(obj, f)
        for f in obj.__struct_fields__
    }
