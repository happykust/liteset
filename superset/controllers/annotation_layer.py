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

from superset.commands.annotation_layer.create import CreateAnnotationLayerCommand
from superset.commands.annotation_layer.delete import (
    BulkDeleteAnnotationLayerCommand,
    DeleteAnnotationLayerCommand,
)
from superset.commands.annotation_layer.update import UpdateAnnotationLayerCommand
from superset.controllers.base import (
    build_rison_query_params,
    extract_ids_required,
    get_info_payload,
    get_related_payload,
    serialize_list_response,
)
from superset.events import event_logger
from superset.exceptions import ObjectNotFoundError
from superset.guards.rbac import require_permission
from superset.i18n import gettext as _
from superset.params.rison import provide_rison_query
from superset.providers import provide_annotation_layer_dao
from superset.schemas.annotation import (
    AnnotationLayerPostSchema,
    AnnotationLayerPutSchema,
)
from superset.typing import SecurityManagerProtocol, UserProtocol
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
        guards=[require_permission("can_read", "Annotation")],
    )
    async def get_list(
        self,
        dao: Any,
        rison_params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """GET /api/v1/annotation_layer/ — list annotation layers."""
        from sqlalchemy import or_
        from sqlalchemy.orm import selectinload

        from superset.models.annotations import AnnotationLayer

        def _annotation_layer_all_text(model: Any, value: Any) -> Any:
            """``AnnotationLayerAllTextFilter`` — free-text over name + descr
            (1:1 upstream)."""
            if not value:
                return None
            ilike = f"%{value}%"
            return or_(model.name.ilike(ilike), model.descr.ilike(ilike))

        rison_filters, order_by, page, page_size = build_rison_query_params(
            AnnotationLayer,
            rison_params,
            custom_filters={"annotation_layer_all_text": _annotation_layer_all_text},
        )
        items = await dao.find_all(
            filters=rison_filters or None,
            page=page,
            page_size=page_size,
            order_by=order_by,
            options=[
                selectinload(AnnotationLayer.changed_by),
                selectinload(AnnotationLayer.created_by),
            ],
        )
        total = await dao.count(filters=rison_filters or None)
        await event_logger.alog_with_context("annotation_layer.list")
        return serialize_list_response(
            items,
            total,
            [
                "id",
                "name",
                "descr",
                "created_on",
                "changed_on",
                "changed_on_delta_humanized",
                "changed_by.first_name",
                "changed_by.last_name",
                "created_by.first_name",
                "created_by.last_name",
            ],
            list_title="List Annotation Layer",
            order_columns=[
                "name",
                "descr",
                "created_by.first_name",
                "changed_by.first_name",
                "changed_on",
                "changed_on_delta_humanized",
                "created_on",
            ],
        )

    # ------------------------------------------------------------------
    # GET — single annotation layer
    # ------------------------------------------------------------------
    @get(
        "/{pk:int}",
        guards=[require_permission("can_read", "Annotation")],
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
        await event_logger.alog_with_context(
            "annotation_layer.get", object_ref=f"annotation_layer:{pk}"
        )
        # Upstream show_columns = ["id", "name", "descr"] — no timestamps.
        # ``descr`` is nullable; FAB serializes NULL as JSON null (no ""
        # coercion).
        return {
            "id": layer.id,
            "result": {
                "id": layer.id,
                "name": layer.name,
                "descr": getattr(layer, "descr", None),
            },
        }

    # ------------------------------------------------------------------
    # POST — create annotation layer
    # ------------------------------------------------------------------
    @post(
        "/",
        guards=[require_permission("can_write", "Annotation")],
        status_code=201,
    )
    async def create(
        self,
        data: AnnotationLayerPostSchema,
        dao: Any,
    ) -> dict[str, Any]:
        """POST /api/v1/annotation_layer/ — create annotation layer."""
        # Absent ``descr`` stays out of the create payload so the column keeps
        # its SQL default (NULL); the 201 body echoes the loaded request dict.
        create_data = filter_unset({"name": data.name, "descr": data.descr})
        cmd = CreateAnnotationLayerCommand(
            dao=dao,
            data=create_data,
        )
        layer = await cmd.execute()
        await event_logger.alog_with_context(
            "annotation_layer.create", object_ref=f"annotation_layer:{layer.id}"
        )
        return {
            "id": layer.id,
            "result": dict(create_data),
        }

    # ------------------------------------------------------------------
    # PUT — update annotation layer
    # ------------------------------------------------------------------
    @put(
        "/{pk:int}",
        guards=[require_permission("can_write", "Annotation")],
    )
    async def update(
        self,
        pk: int,
        data: AnnotationLayerPutSchema,
        dao: Any,
    ) -> dict[str, Any]:
        """PUT /api/v1/annotation_layer/<pk> — update annotation layer."""
        update_data = filter_unset({"name": data.name, "descr": data.descr})
        cmd = UpdateAnnotationLayerCommand(dao=dao, pk=pk, data=update_data)
        layer = await cmd.execute()
        await event_logger.alog_with_context(
            "annotation_layer.update", object_ref=f"annotation_layer:{pk}"
        )
        # PUT returns result=item where item["layer"] = pk was added before returning.
        result_item = dict(update_data)
        result_item["layer"] = pk
        return {
            "id": layer.id,
            "result": result_item,
        }

    # ------------------------------------------------------------------
    # DELETE — single annotation layer
    # ------------------------------------------------------------------
    @delete(
        "/{pk:int}",
        guards=[require_permission("can_write", "Annotation")],
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
        await event_logger.alog_with_context(
            "annotation_layer.delete", object_ref=f"annotation_layer:{pk}"
        )
        return {"message": "OK"}

    # ------------------------------------------------------------------
    # DELETE — bulk delete annotation layers
    # ------------------------------------------------------------------
    @delete(
        "/",
        guards=[require_permission("can_write", "Annotation")],
        status_code=200,
    )
    async def bulk_delete(
        self,
        dao: Any,
        rison_params: list[int] | dict[str, Any] | None,
    ) -> dict[str, str]:
        """DELETE /api/v1/annotation_layer/?q=(...) — bulk delete layers."""
        ids = extract_ids_required(rison_params)
        cmd = BulkDeleteAnnotationLayerCommand(dao=dao, ids=ids)
        await cmd.execute()
        await event_logger.alog_with_context(
            "annotation_layer.bulk_delete", extra={"count": len(ids)}
        )
        num = len(ids)
        message = (
            _("Deleted %(num)d annotation layer", num=num)
            if num == 1
            else _("Deleted %(num)d annotation layers", num=num)
        )
        return {"message": message}

    @get(
        "/_info",
        guards=[require_permission("can_read", "Annotation")],
    )
    async def info(
        self,
        dao: Any,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
    ) -> dict[str, Any]:
        """GET /api/v1/annotation_layer/_info -- API metadata for frontend."""
        return await get_info_payload(
            dao=dao,
            model_name="AnnotationLayer",
            permissions=["can_read", "can_write"],
            security_manager=security_manager,
            current_user=current_user,
            class_permission_name="Annotation",
        )

    @get(
        "/related/{column_name:str}",
        guards=[require_permission("can_read", "Annotation")],
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
