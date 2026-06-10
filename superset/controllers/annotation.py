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
    build_rison_query_params,
    extract_ids_required,
    serialize_list_response,
)
from superset.events import event_logger
from superset.exceptions import ObjectNotFoundError
from superset.guards.rbac import require_permission
from superset.i18n import gettext as _
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

        from sqlalchemy import or_
        from sqlalchemy.orm import selectinload

        from superset.models.annotations import Annotation

        def _annotation_all_text(model: Any, value: Any) -> Any:
            """``AnnotationAllTextFilter`` — free-text search over short_descr
            and long_descr (1:1 with superset_old/.../annotations/filters.py)."""
            if not value:
                return None
            ilike = f"%{value}%"
            return or_(model.short_descr.ilike(ilike), model.long_descr.ilike(ilike))

        rison_filters, order_by, page, page_size = build_rison_query_params(
            Annotation,
            rison_params,
            custom_filters={"annotation_all_text": _annotation_all_text},
            default_page_size=20,
        )
        # Always scope to the parent layer.
        layer_filter = [Annotation.layer_id == pk]
        all_filters = layer_filter + (rison_filters or [])
        order_column = (rison_params or {}).get("order_column")
        order_direction = (rison_params or {}).get("order_direction", "asc")

        joins = None
        if order_column in ("changed_by.first_name", "created_by.first_name"):
            from sqlalchemy import asc, desc

            from superset.models.security import User

            is_desc = order_direction == "desc"
            sort_expr = desc(User.first_name) if is_desc else asc(User.first_name)
            order_by = [sort_expr]
            if order_column == "changed_by.first_name":
                joins = [(User, Annotation.changed_by)]
            else:
                joins = [(User, Annotation.created_by)]

        # Eager-load the audit-user relationships so the async serializer can
        # read changed_by/created_by without a lazy-load MissingGreenlet.
        items = await ann_dao.find_all(
            filters=all_filters,
            page=page,
            page_size=page_size,
            order_by=order_by,
            joins=joins,
            options=[
                selectinload(Annotation.changed_by),
                selectinload(Annotation.created_by),
            ],
        )
        total = await ann_dao.count(filters=all_filters)
        await event_logger.alog_with_context("annotation.list", extra={"layer_id": pk})
        # Mirror upstream ``AnnotationRestApi.list_columns``
        # (superset_old/annotation_layers/annotations/api.py:78-89).
        _list_columns = [
            "id",
            "changed_by.first_name",
            "changed_by.id",
            "changed_on_delta_humanized",
            "created_by.first_name",
            "created_by.id",
            "end_dttm",
            "long_descr",
            "short_descr",
            "start_dttm",
        ]
        # Mirror upstream ``AnnotationRestApi.order_columns``
        # (superset_old/annotation_layers/annotations/api.py:100-108).
        _order_columns = [
            "changed_by.first_name",
            "changed_on_delta_humanized",
            "created_by.first_name",
            "end_dttm",
            "long_descr",
            "short_descr",
            "start_dttm",
        ]
        return serialize_list_response(
            items,
            total,
            _list_columns,
            list_title="List Annotation",
            order_columns=_order_columns,
        )

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
        from sqlalchemy.orm import selectinload

        from superset.models.annotations import Annotation

        # Eager-load the ``layer`` relationship so we can return the nested
        # ``layer: {id, name}`` shape that the original show_columns specify
        # (superset_old/annotation_layers/annotations/api.py:68-77).
        # Scope by layer pk: the original appends a ``layer == pk`` rison
        # filter via ``_apply_layered_relation_to_rison`` (api.py:241), so an
        # annotation requested under the wrong layer is a 404.
        items = await ann_dao.find_all(
            filters=[
                Annotation.id == annotation_id,
                Annotation.layer_id == pk,
            ],
            options=[selectinload(Annotation.layer)],
        )
        annotation = items[0] if items else None
        if not annotation:
            raise ObjectNotFoundError("Annotation", annotation_id)

        ann_layer = getattr(annotation, "layer", None)
        await event_logger.alog_with_context(
            "annotation.get", object_ref=f"annotation:{annotation_id}"
        )
        return {
            "id": annotation.id,
            "result": {
                "id": annotation.id,
                "short_descr": getattr(annotation, "short_descr", ""),
                # Nullable column — the original serializes None as JSON null
                # (no "" coercion).
                "long_descr": getattr(annotation, "long_descr", None),
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
                "json_metadata": getattr(annotation, "json_metadata", None),
                "layer": (
                    {
                        "id": ann_layer.id,
                        "name": getattr(ann_layer, "name", ""),
                    }
                    if ann_layer is not None
                    else None
                ),
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
        import msgspec as _msgspec

        cmd_data: dict[str, Any] = {
            "short_descr": data.short_descr,
            "start_dttm": data.start_dttm,
            "end_dttm": data.end_dttm,
        }
        if data.long_descr is not _msgspec.UNSET:
            cmd_data["long_descr"] = data.long_descr
        if data.json_metadata is not _msgspec.UNSET:
            cmd_data["json_metadata"] = data.json_metadata

        cmd = CreateAnnotationCommand(
            dao=ann_dao,
            layer_dao=layer_dao,
            layer_pk=pk,
            data=dict(cmd_data),
        )
        from litestar.exceptions import ClientException

        try:
            annotation = await cmd.execute()
        except ObjectNotFoundError as ex:
            if "AnnotationLayer" in str(ex):
                raise ClientException(status_code=400, detail=str(ex)) from ex
            raise
        await event_logger.alog_with_context(
            "annotation.create",
            object_ref=f"annotation:{annotation.id}",
            extra={"layer_id": pk},
        )
        # Echo exactly the submitted keys — 1:1 with the original
        # ``result=item`` (the Marshmallow-loaded request dict plus
        # ``item["layer"] = pk``, superset_old/annotation_layers/annotations/
        # api.py:289-297). Unsubmitted optional fields are ABSENT, and a
        # submitted ``long_descr: null`` echoes as ``null`` (no "" coercion).
        result: dict[str, Any] = {
            "short_descr": data.short_descr,
            "start_dttm": data.start_dttm.isoformat(),
            "end_dttm": data.end_dttm.isoformat(),
        }
        if data.long_descr is not _msgspec.UNSET:
            result["long_descr"] = data.long_descr
        if data.json_metadata is not _msgspec.UNSET:
            result["json_metadata"] = data.json_metadata
        result["layer"] = pk
        return {
            "id": annotation.id,
            "result": result,
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
        # Save submitted fields *before* adding the FK so we can echo only
        # what the client sent in the response (mirrors the original where
        # ``item = edit_model_schema.load(request.json)`` contains only the
        # keys present in the request body, then ``item["layer"] = pk`` is
        # appended — superset_old/annotation_layers/annotations/api.py:364-371).
        submitted_data: dict[str, Any] = dict(update_data)
        # Mirror original: item["layer"] = pk is always set before the command
        # (superset_old/annotation_layers/annotations/api.py:365), so the
        # annotation's layer FK is always (re-)assigned to the URL layer pk.
        update_data["layer_id"] = pk
        cmd = UpdateAnnotationCommand(dao=ann_dao, pk=annotation_id, data=update_data)
        annotation = await cmd.execute()
        await event_logger.alog_with_context(
            "annotation.update", object_ref=f"annotation:{annotation_id}"
        )
        # Mirror original 200 result: only submitted fields + layer int pk.
        # ``item`` in the original is the partial Marshmallow-loaded dict
        # (only keys present in the request body) plus item["layer"] = pk.
        # We replicate that from submitted_data so a client sending only
        # short_descr does NOT see the other fields in the response.
        result: dict[str, Any] = {}
        for key in ("short_descr", "long_descr", "json_metadata"):
            if key in submitted_data:
                result[key] = submitted_data[key]
        for key in ("start_dttm", "end_dttm"):
            if key in submitted_data:
                val = submitted_data[key]
                result[key] = val.isoformat() if val is not None else None
        result["layer"] = pk
        return {"id": annotation.id, "result": result}

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
        # No layer-existence guard: the original DeleteAnnotationCommand
        # deletes by annotation id alone and ignores the layer pk in the URL
        # (superset_old/commands/annotation_layer/annotation/delete.py:33-48).
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
        # No layer-existence guard — see delete_annotation above.
        ids = extract_ids_required(rison_params)
        cmd = BulkDeleteAnnotationCommand(dao=ann_dao, ids=ids)
        await cmd.execute()
        await event_logger.alog_with_context(
            "annotation.bulk_delete",
            extra={"layer_id": pk, "count": len(ids)},
        )
        num = len(ids)
        message = (
            _("Deleted %(num)d annotation", num=num)
            if num == 1
            else _("Deleted %(num)d annotations", num=num)
        )
        return {"message": message}
