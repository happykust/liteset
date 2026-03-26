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

from liteset.commands.annotation import (
    BulkDeleteAnnotationCommand,
    CreateAnnotationCommand,
    DeleteAnnotationCommand,
    UpdateAnnotationCommand,
)
from liteset.controllers.base import (
    extract_ids_required,
    extract_pagination,
)
from liteset.events import event_logger
from liteset.exceptions import ObjectNotFoundError
from liteset.guards.rbac import require_permission
from liteset.params.rison import provide_rison_query
from liteset.providers import provide_annotation_dao, provide_annotation_layer_dao
from liteset.schemas.annotation import AnnotationPostSchema, AnnotationPutSchema
from liteset.utils import filter_unset


class AnnotationController(Controller):
    path = "/api/v1/annotation_layer/{layer_pk:int}/annotation"
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
        layer_pk: int,
        ann_dao: Any,
        layer_dao: Any,
        rison_params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """GET /api/v1/annotation_layer/<layer_pk>/annotation/ — list annotations."""
        # Verify layer exists
        layer = await layer_dao.find_by_id(layer_pk)
        if not layer:
            raise ObjectNotFoundError("AnnotationLayer", layer_pk)

        from liteset.models.annotations import Annotation

        page, page_size = extract_pagination(rison_params)
        layer_filter = [Annotation.layer_id == layer_pk]
        items = await ann_dao.find_all(
            filters=layer_filter, page=page, page_size=page_size
        )
        total = await ann_dao.count(filters=layer_filter)
        event_logger.log("annotation.list", extra={"layer_id": layer_pk})
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
        "/{pk:int}",
        guards=[require_permission("can_read", "Annotation")],
    )
    async def get_single(
        self,
        layer_pk: int,
        pk: int,
        ann_dao: Any,
        layer_dao: Any,
    ) -> dict[str, Any]:
        """GET /api/v1/annotation_layer/<layer_pk>/annotation/<pk>."""
        layer = await layer_dao.find_by_id(layer_pk)
        if not layer:
            raise ObjectNotFoundError("AnnotationLayer", layer_pk)
        annotation = await ann_dao.find_by_id(pk)
        if not annotation or getattr(annotation, "layer_id", None) != layer_pk:
            raise ObjectNotFoundError("Annotation", pk)
        changed_on = getattr(annotation, "changed_on", None)
        created_on = getattr(annotation, "created_on", None)
        event_logger.log("annotation.get", object_ref=f"annotation:{pk}")
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
        layer_pk: int,
        data: AnnotationPostSchema,
        ann_dao: Any,
        layer_dao: Any,
    ) -> dict[str, Any]:
        """POST /api/v1/annotation_layer/<layer_pk>/annotation/ — create."""
        cmd = CreateAnnotationCommand(
            dao=ann_dao,
            layer_dao=layer_dao,
            layer_pk=layer_pk,
            data={
                "short_descr": data.short_descr,
                "long_descr": data.long_descr,
                "start_dttm": data.start_dttm,
                "end_dttm": data.end_dttm,
                "json_metadata": data.json_metadata,
            },
        )
        annotation = await cmd.execute()
        event_logger.log(
            "annotation.create",
            object_ref=f"annotation:{annotation.id}",
            extra={"layer_id": layer_pk},
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
        "/{pk:int}",
        guards=[require_permission("can_write", "Annotation")],
    )
    async def update(
        self,
        layer_pk: int,
        pk: int,
        data: AnnotationPutSchema,
        ann_dao: Any,
        layer_dao: Any,
    ) -> dict[str, Any]:
        """PUT /api/v1/annotation_layer/<layer_pk>/annotation/<pk>."""
        # Verify layer exists
        layer = await layer_dao.find_by_id(layer_pk)
        if not layer:
            raise ObjectNotFoundError("AnnotationLayer", layer_pk)

        update_data = filter_unset(
            {
                "short_descr": data.short_descr,
                "long_descr": data.long_descr,
                "start_dttm": data.start_dttm,
                "end_dttm": data.end_dttm,
                "json_metadata": data.json_metadata,
            }
        )
        cmd = UpdateAnnotationCommand(dao=ann_dao, pk=pk, data=update_data)
        annotation = await cmd.execute()
        changed_on = getattr(annotation, "changed_on", None)
        created_on = getattr(annotation, "created_on", None)
        event_logger.log(
            "annotation.update", object_ref=f"annotation:{pk}"
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
        "/{pk:int}",
        guards=[require_permission("can_write", "Annotation")],
        status_code=200,
    )
    async def delete_annotation(
        self,
        layer_pk: int,
        pk: int,
        ann_dao: Any,
        layer_dao: Any,
    ) -> dict[str, str]:
        """DELETE /api/v1/annotation_layer/<layer_pk>/annotation/<pk>."""
        layer = await layer_dao.find_by_id(layer_pk)
        if not layer:
            raise ObjectNotFoundError("AnnotationLayer", layer_pk)
        cmd = DeleteAnnotationCommand(dao=ann_dao, pk=pk)
        await cmd.execute()
        event_logger.log(
            "annotation.delete",
            object_ref=f"annotation:{pk}",
            extra={"layer_id": layer_pk},
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
        layer_pk: int,
        ann_dao: Any,
        layer_dao: Any,
        rison_params: dict[str, Any] | None,
    ) -> dict[str, str]:
        """DELETE /api/v1/annotation_layer/<layer_pk>/annotation/?q=(...)."""
        layer = await layer_dao.find_by_id(layer_pk)
        if not layer:
            raise ObjectNotFoundError("AnnotationLayer", layer_pk)
        ids = extract_ids_required(rison_params)
        cmd = BulkDeleteAnnotationCommand(dao=ann_dao, ids=ids)
        await cmd.execute()
        event_logger.log(
            "annotation.bulk_delete",
            extra={"layer_id": layer_pk, "count": len(ids)},
        )
        return {"message": "OK"}
