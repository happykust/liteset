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
"""Annotation controller — CRUD endpoints for annotations nested under layers."""

from __future__ import annotations

from typing import Any

from litestar import Controller, delete, get, post, put
from litestar.di import Provide

from superset.commands.annotation_layer.annotation.create import (
    CreateAnnotationCommand,
)
from superset.commands.annotation_layer.annotation.delete import (
    BulkDeleteAnnotationCommand,
    DeleteAnnotationCommand,
)
from superset.commands.annotation_layer.annotation.update import (
    UpdateAnnotationCommand,
)
from superset.controllers.base import (
    extract_ids_required,
    extract_pagination,
)
from superset.events import event_logger
from superset.exceptions import ObjectNotFoundError
from superset.guards.rbac import require_permission
from superset.params.rison import provide_rison_query
from superset.providers import provide_annotation_dao, provide_annotation_layer_dao
from superset.schemas.annotation import AnnotationPostSchema, AnnotationPutSchema
from superset.utils import filter_unset


class AnnotationController(Controller):
    path = "/api/v1/annotation_layer/{pk:int}/annotation"
    tags = ["Annotations"]
    dependencies = {
        "ann_dao": Provide(provide_annotation_dao, sync_to_thread=False),
        "layer_dao": Provide(provide_annotation_layer_dao, sync_to_thread=False),
        "rison_params": Provide(provide_rison_query),
    }

    # ------------------------------------------------------------------
    # GET — list annotations for a layer
    # ------------------------------------------------------------------
    @get(
        "/",
        guards=[require_permission("can_read", "Annotation")],
    )
    async def get_list(
        self,
        pk: int,
        ann_dao: Any,
        layer_dao: Any,
        rison_params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """GET /api/v1/annotation_layer/<pk>/annotation/ — list annotations."""
        # Verify layer exists
        layer = await layer_dao.find_by_id(pk)
        if not layer:
            raise ObjectNotFoundError("AnnotationLayer", pk)

        from superset.models.annotations import Annotation

        page, page_size = extract_pagination(rison_params)
        layer_filter = [Annotation.layer_id == pk]
        items = await ann_dao.find_all(
            filters=layer_filter, page=page, page_size=page_size
        )
        total = await ann_dao.count(filters=layer_filter)
        await event_logger.alog_with_context("annotation.list", extra={"layer_id": pk})
        return {
            "result": [
                {
                    "id": item.id,
                    "short_descr": getattr(item, "short_descr", ""),
                    "long_descr": getattr(item, "long_descr", "") or "",
                    "start_dttm": (
                        item.start_dttm.isoformat()
                        if getattr(item, "start_dttm", None)
                        else None
                    ),
                    "end_dttm": (
                        item.end_dttm.isoformat()
                        if getattr(item, "end_dttm", None)
                        else None
                    ),
                    "layer_id": getattr(item, "layer_id", None),
                }
                for item in (items or [])
            ],
            "count": total,
        }

    # ------------------------------------------------------------------
    # GET — single annotation
    # ------------------------------------------------------------------
    @get(
        "/{annotation_id:int}",
        guards=[require_permission("can_read", "Annotation")],
    )
    async def get_single(
        self,
        pk: int,
        annotation_id: int,
        ann_dao: Any,
        layer_dao: Any,
    ) -> dict[str, Any]:
        """GET /api/v1/annotation_layer/<pk>/annotation/<annotation_id>."""
        layer = await layer_dao.find_by_id(pk)
        if not layer:
            raise ObjectNotFoundError("AnnotationLayer", pk)
        annotation = await ann_dao.find_by_id(annotation_id)
        if not annotation or getattr(annotation, "layer_id", None) != pk:
            raise ObjectNotFoundError("Annotation", annotation_id)
        changed_on = getattr(annotation, "changed_on", None)
        created_on = getattr(annotation, "created_on", None)
        await event_logger.alog_with_context(
            "annotation.get", object_ref=f"annotation:{annotation_id}"
        )
        return {
            "id": annotation.id,
            "result": {
                "short_descr": getattr(annotation, "short_descr", ""),
                "long_descr": getattr(annotation, "long_descr", "") or "",
                "start_dttm": (
                    annotation.start_dttm.isoformat()
                    if getattr(annotation, "start_dttm", None)
                    else None
                ),
                "end_dttm": (
                    annotation.end_dttm.isoformat()
                    if getattr(annotation, "end_dttm", None)
                    else None
                ),
                "layer_id": getattr(annotation, "layer_id", None),
                "json_metadata": getattr(annotation, "json_metadata", None),
                "created_on": created_on.isoformat() if created_on else None,
                "changed_on": changed_on.isoformat() if changed_on else None,
            },
        }

    # ------------------------------------------------------------------
    # POST — create annotation
    # ------------------------------------------------------------------
    @post(
        "/",
        guards=[require_permission("can_write", "Annotation")],
        status_code=201,
    )
    async def create(
        self,
        pk: int,
        data: AnnotationPostSchema,
        ann_dao: Any,
        layer_dao: Any,
    ) -> dict[str, Any]:
        """POST /api/v1/annotation_layer/<pk>/annotation/ — create."""
        cmd = CreateAnnotationCommand(
            dao=ann_dao,
            layer_dao=layer_dao,
            layer_pk=pk,
            data={
                "short_descr": data.short_descr,
                "long_descr": data.long_descr,
                "start_dttm": data.start_dttm,
                "end_dttm": data.end_dttm,
                "json_metadata": data.json_metadata,
            },
        )
        annotation = await cmd.execute()
        await event_logger.alog_with_context(
            "annotation.create",
            object_ref=f"annotation:{annotation.id}",
            extra={"layer_id": pk},
        )
        return {
            "id": annotation.id,
            "result": {
                "short_descr": getattr(annotation, "short_descr", ""),
                "long_descr": getattr(annotation, "long_descr", "") or "",
                "start_dttm": (
                    annotation.start_dttm.isoformat()
                    if getattr(annotation, "start_dttm", None)
                    else None
                ),
                "end_dttm": (
                    annotation.end_dttm.isoformat()
                    if getattr(annotation, "end_dttm", None)
                    else None
                ),
                "layer_id": getattr(annotation, "layer_id", None),
            },
        }

    # ------------------------------------------------------------------
    # PUT — update annotation
    # ------------------------------------------------------------------
    @put(
        "/{annotation_id:int}",
        guards=[require_permission("can_write", "Annotation")],
    )
    async def update(
        self,
        pk: int,
        annotation_id: int,
        data: AnnotationPutSchema,
        ann_dao: Any,
        layer_dao: Any,
    ) -> dict[str, Any]:
        """PUT /api/v1/annotation_layer/<pk>/annotation/<annotation_id>."""
        # Verify layer exists
        layer = await layer_dao.find_by_id(pk)
        if not layer:
            raise ObjectNotFoundError("AnnotationLayer", pk)

        update_data = filter_unset(
            {
                "short_descr": data.short_descr,
                "long_descr": data.long_descr,
                "start_dttm": data.start_dttm,
                "end_dttm": data.end_dttm,
                "json_metadata": data.json_metadata,
            }
        )
        cmd = UpdateAnnotationCommand(dao=ann_dao, pk=annotation_id, data=update_data)
        annotation = await cmd.execute()
        changed_on = getattr(annotation, "changed_on", None)
        created_on = getattr(annotation, "created_on", None)
        await event_logger.alog_with_context(
            "annotation.update", object_ref=f"annotation:{annotation_id}"
        )
        return {
            "id": annotation.id,
            "result": {
                "short_descr": getattr(annotation, "short_descr", ""),
                "long_descr": getattr(annotation, "long_descr", "") or "",
                "start_dttm": (
                    annotation.start_dttm.isoformat()
                    if getattr(annotation, "start_dttm", None)
                    else None
                ),
                "end_dttm": (
                    annotation.end_dttm.isoformat()
                    if getattr(annotation, "end_dttm", None)
                    else None
                ),
                "layer_id": getattr(annotation, "layer_id", None),
                "json_metadata": getattr(annotation, "json_metadata", None),
                "created_on": created_on.isoformat() if created_on else None,
                "changed_on": changed_on.isoformat() if changed_on else None,
            },
        }

    # ------------------------------------------------------------------
    # DELETE — single annotation
    # ------------------------------------------------------------------
    @delete(
        "/{annotation_id:int}",
        guards=[require_permission("can_write", "Annotation")],
        status_code=200,
    )
    async def delete_annotation(
        self,
        pk: int,
        annotation_id: int,
        ann_dao: Any,
        layer_dao: Any,
    ) -> dict[str, str]:
        """DELETE /api/v1/annotation_layer/<pk>/annotation/<annotation_id>."""
        layer = await layer_dao.find_by_id(pk)
        if not layer:
            raise ObjectNotFoundError("AnnotationLayer", pk)
        cmd = DeleteAnnotationCommand(dao=ann_dao, pk=annotation_id)
        await cmd.execute()
        await event_logger.alog_with_context(
            "annotation.delete",
            object_ref=f"annotation:{annotation_id}",
            extra={"layer_id": pk},
        )
        return {"message": "OK"}

    # ------------------------------------------------------------------
    # DELETE — bulk delete annotations
    # ------------------------------------------------------------------
    @delete(
        "/",
        guards=[require_permission("can_write", "Annotation")],
        status_code=200,
    )
    async def bulk_delete(
        self,
        pk: int,
        ann_dao: Any,
        layer_dao: Any,
        rison_params: list[int] | dict[str, Any] | None,
    ) -> dict[str, str]:
        """DELETE /api/v1/annotation_layer/<pk>/annotation/?q=(...)."""
        layer = await layer_dao.find_by_id(pk)
        if not layer:
            raise ObjectNotFoundError("AnnotationLayer", pk)
        ids = extract_ids_required(rison_params)
        cmd = BulkDeleteAnnotationCommand(dao=ann_dao, ids=ids)
        await cmd.execute()
        await event_logger.alog_with_context(
            "annotation.bulk_delete",
            extra={"layer_id": pk, "count": len(ids)},
        )
        return {"message": "OK"}
