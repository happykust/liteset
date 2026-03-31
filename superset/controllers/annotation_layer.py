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
"""Annotation Layer controller — CRUD endpoints for annotation layers."""

from __future__ import annotations

from typing import Any

from litestar import Controller, delete, get, post, put
from litestar.di import Provide

from superset.commands.annotation import (
    BulkDeleteAnnotationLayerCommand,
    CreateAnnotationLayerCommand,
    DeleteAnnotationLayerCommand,
    UpdateAnnotationLayerCommand,
)
from superset.controllers.base import (
    extract_ids_required,
    extract_pagination,
    get_info_payload,
    get_related_payload,
    serialize_list_response,
)
from superset.events import event_logger
from superset.exceptions import ObjectNotFoundError
from superset.guards.rbac import require_permission
from superset.params.rison import provide_rison_query
from superset.providers import provide_annotation_layer_dao
from superset.schemas.annotation import (
    AnnotationLayerPostSchema,
    AnnotationLayerPutSchema,
)
from superset.utils import filter_unset


class AnnotationLayerController(Controller):
    path = "/api/v1/annotation_layer"
    tags = ["Annotation Layers"]
    dependencies = {
        "dao": Provide(provide_annotation_layer_dao, sync_to_thread=False),
        "rison_params": Provide(provide_rison_query),
    }

    # ------------------------------------------------------------------
    # GET — list annotation layers
    # ------------------------------------------------------------------
    @get(
        "/",
        guards=[require_permission("can_read", "AnnotationLayer")],
    )
    async def get_list(
        self,
        dao: Any,
        rison_params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """GET /api/v1/annotation_layer/ — list annotation layers."""
        page, page_size = extract_pagination(rison_params)
        items = await dao.find_all(page=page, page_size=page_size)
        total = await dao.count()
        event_logger.log("annotation_layer.list")
        return serialize_list_response(items, total, ["id", "name", "descr"])

    # ------------------------------------------------------------------
    # GET — single annotation layer
    # ------------------------------------------------------------------
    @get(
        "/{pk:int}",
        guards=[require_permission("can_read", "AnnotationLayer")],
    )
    async def get_single(
        self,
        pk: int,
        dao: Any,
    ) -> dict[str, Any]:
        """GET /api/v1/annotation_layer/<pk> — get single annotation layer."""
        layer = await dao.find_by_id(pk)
        if not layer:
            raise ObjectNotFoundError("AnnotationLayer", pk)
        changed_on = getattr(layer, "changed_on", None)
        created_on = getattr(layer, "created_on", None)
        event_logger.log(
            "annotation_layer.get", object_ref=f"annotation_layer:{pk}"
        )
        return {
            "id": layer.id,
            "result": {
                "name": layer.name,
                "descr": getattr(layer, "descr", "") or "",
                "created_on": created_on.isoformat() if created_on else None,
                "changed_on": changed_on.isoformat() if changed_on else None,
            },
        }

    # ------------------------------------------------------------------
    # POST — create annotation layer
    # ------------------------------------------------------------------
    @post(
        "/",
        guards=[require_permission("can_write", "AnnotationLayer")],
        status_code=201,
    )
    async def create(
        self,
        data: AnnotationLayerPostSchema,
        dao: Any,
    ) -> dict[str, Any]:
        """POST /api/v1/annotation_layer/ — create annotation layer."""
        cmd = CreateAnnotationLayerCommand(
            dao=dao,
            data={"name": data.name, "descr": data.descr},
        )
        layer = await cmd.execute()
        event_logger.log(
            "annotation_layer.create", object_ref=f"annotation_layer:{layer.id}"
        )
        return {
            "id": layer.id,
            "result": {
                "name": layer.name,
                "descr": getattr(layer, "descr", "") or "",
            },
        }

    # ------------------------------------------------------------------
    # PUT — update annotation layer
    # ------------------------------------------------------------------
    @put(
        "/{pk:int}",
        guards=[require_permission("can_write", "AnnotationLayer")],
    )
    async def update(
        self,
        pk: int,
        data: AnnotationLayerPutSchema,
        dao: Any,
    ) -> dict[str, Any]:
        """PUT /api/v1/annotation_layer/<pk> — update annotation layer."""
        update_data = filter_unset(
            {"name": data.name, "descr": data.descr}
        )
        cmd = UpdateAnnotationLayerCommand(dao=dao, pk=pk, data=update_data)
        layer = await cmd.execute()
        changed_on = getattr(layer, "changed_on", None)
        created_on = getattr(layer, "created_on", None)
        event_logger.log(
            "annotation_layer.update", object_ref=f"annotation_layer:{pk}"
        )
        return {
            "id": layer.id,
            "result": {
                "name": layer.name,
                "descr": getattr(layer, "descr", "") or "",
                "created_on": created_on.isoformat() if created_on else None,
                "changed_on": changed_on.isoformat() if changed_on else None,
            },
        }

    # ------------------------------------------------------------------
    # DELETE — single annotation layer
    # ------------------------------------------------------------------
    @delete(
        "/{pk:int}",
        guards=[require_permission("can_write", "AnnotationLayer")],
        status_code=200,
    )
    async def delete_layer(
        self,
        pk: int,
        dao: Any,
    ) -> dict[str, str]:
        """DELETE /api/v1/annotation_layer/<pk> — delete annotation layer."""
        cmd = DeleteAnnotationLayerCommand(dao=dao, pk=pk)
        await cmd.execute()
        event_logger.log(
            "annotation_layer.delete", object_ref=f"annotation_layer:{pk}"
        )
        return {"message": "OK"}

    # ------------------------------------------------------------------
    # DELETE — bulk delete annotation layers
    # ------------------------------------------------------------------
    @delete(
        "/",
        guards=[require_permission("can_write", "AnnotationLayer")],
        status_code=200,
    )
    async def bulk_delete(
        self,
        dao: Any,
        rison_params: dict[str, Any] | None,
    ) -> dict[str, str]:
        """DELETE /api/v1/annotation_layer/?q=(...) — bulk delete layers."""
        ids = extract_ids_required(rison_params)
        cmd = BulkDeleteAnnotationLayerCommand(dao=dao, ids=ids)
        await cmd.execute()
        event_logger.log(
            "annotation_layer.bulk_delete", extra={"count": len(ids)}
        )
        return {"message": "OK"}

    @get(
        "/_info",
        guards=[require_permission("can_read", "AnnotationLayer")],
    )
    async def info(self, dao: Any) -> dict[str, Any]:
        """GET /api/v1/annotation_layer/_info -- API metadata for frontend."""
        return await get_info_payload(
            dao=dao,
            model_name="AnnotationLayer",
            permissions=["can_read", "can_write"],
        )

    @get(
        "/related/{column_name:str}",
        guards=[require_permission("can_read", "AnnotationLayer")],
    )
    async def related(
        self,
        column_name: str,
        dao: Any,
        rison_params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """GET /api/v1/annotation_layer/related/{column_name}."""
        return await get_related_payload(
            dao=dao,
            column_name=column_name,
            rison_params=rison_params,
            allowed_fields=frozenset({"created_by", "changed_by"}),
        )
