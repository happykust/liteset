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
"""Tag controller -- 11 endpoints for tag CRUD, bulk operations, and favorites."""

from __future__ import annotations

from typing import Any

import msgspec
from litestar import Controller, delete, get, post, put
from litestar.connection import Request
from litestar.di import Provide

from superset.commands.tag import (
    BulkCreateTagCommand,
    BulkDeleteTagCommand,
    CreateTagCommand,
    DeleteTagCommand,
    UpdateTagCommand,
)
from superset.controllers.base import (
    _serialize_item,
    build_rison_query_params,
    extract_ids,
    get_info_payload,
    get_related_payload,
    serialize_list_response,
)
from superset.events import event_logger
from superset.exceptions import ObjectNotFoundError, SupersetValidationException
from superset.guards.rbac import require_permission
from superset.params.rison import provide_rison_query
from superset.providers import provide_tag_dao
from superset.schemas.tag import (
    AddTagsToObjectSchema,
    BulkTagCreateSchema,
    TagPostSchema,
    TagPutSchema,
)
from superset.typing import SecurityManagerProtocol, UserProtocol
from superset.utils import filter_unset


class TagController(Controller):
    path = "/api/v1/tag"
    tags = ["Tags"]
    dependencies = {
        "dao": Provide(provide_tag_dao, sync_to_thread=False),
        "rison_params": Provide(provide_rison_query),
    }

    @get("/", guards=[require_permission("can_read", "Tag")])
    async def get_list(
        self,
        dao: Any,
        rison_params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        from sqlalchemy.orm import selectinload

        from superset.models.tags import Tag

        rison_filters, order_by, page, page_size = build_rison_query_params(
            Tag,
            rison_params,
        )
        items = await dao.find_all(
            filters=rison_filters or None,
            page=page,
            page_size=page_size,
            order_by=order_by,
            options=[
                selectinload(Tag.changed_by),
                selectinload(Tag.created_by),
            ],
        )
        total = await dao.count(filters=rison_filters or None)
        return serialize_list_response(
            items,
            total,
            [
                "id",
                "name",
                "type",
                "description",
                "changed_on_delta_humanized",
                "created_on_delta_humanized",
                "changed_by.first_name",
                "changed_by.last_name",
                "created_by.first_name",
                "created_by.last_name",
            ],
            list_title="List Tag",
        )

    @get("/{pk:int}", guards=[require_permission("can_read", "Tag")])
    async def get_single(self, pk: int, dao: Any) -> dict[str, Any]:
        from sqlalchemy.orm import selectinload

        from superset.exceptions import ObjectNotFoundError
        from superset.models.tags import Tag

        # Eager-load changed_by/created_by so ``_serialize_item`` reads them
        # without a sync lazy-load (MissingGreenlet) — ``find_by_id`` returns a
        # bare row (the list endpoint already eager-loads these).
        results = await dao.find_all(
            filters=[Tag.id == pk],
            options=[
                selectinload(Tag.changed_by),
                selectinload(Tag.created_by),
            ],
        )
        item = results[0] if results else None
        if item is None:
            raise ObjectNotFoundError("Tag", pk)
        # FAB ``get_headless`` envelope is ``{"id": <pk>, "result": {...}}`` —
        # the top-level ``id`` was missing.
        return {
            "id": item.id,
            "result": _serialize_item(
                item,
                [
                    "id",
                    "name",
                    "type",
                    "description",
                    "changed_on_delta_humanized",
                    "created_on_delta_humanized",
                    "changed_by.first_name",
                    "changed_by.last_name",
                    "created_by.first_name",
                    "created_by.last_name",
                ],
            )
        }

    @post("/", guards=[require_permission("can_write", "Tag")], status_code=201)
    async def create_tag(
        self,
        data: TagPostSchema,
        dao: Any,
        current_user: UserProtocol,
    ) -> dict[str, Any]:
        raw = msgspec.structs.asdict(data)
        cmd = CreateTagCommand(dao=dao, data=raw)
        item = await cmd.execute()
        await event_logger.alog_with_context(
            "tag.create", object_ref=str(item.id), user_id=current_user.id
        )
        return {"id": item.id, "result": {"name": item.name}}

    @put("/{pk:int}", guards=[require_permission("can_write", "Tag")])
    async def update_tag(
        self,
        pk: int,
        data: TagPutSchema,
        dao: Any,
        current_user: UserProtocol,
    ) -> dict[str, Any]:
        raw = filter_unset(msgspec.structs.asdict(data))
        cmd = UpdateTagCommand(dao=dao, pk=pk, data=raw)
        item = await cmd.execute()
        await event_logger.alog_with_context(
            "tag.update", object_ref=str(pk), user_id=current_user.id
        )
        # FAB ``put_headless`` envelope: ``{"id": <pk>, "result": <edit_columns
        # dump>}``. 1:1 with the original ``response(200, id=changed_model.id,
        # result=item)`` — the port previously dropped ``id`` and reduced
        # ``result`` to just ``{"name": …}``.
        return {"id": item.id, "result": raw}

    @delete(
        "/{pk:int}",
        guards=[require_permission("can_write", "Tag")],
        status_code=200,
    )
    async def delete_tag(
        self,
        pk: int,
        dao: Any,
        current_user: UserProtocol,
    ) -> dict[str, str]:
        cmd = DeleteTagCommand(dao=dao, pk=pk)
        await cmd.execute()
        await event_logger.alog_with_context(
            "tag.delete", object_ref=str(pk), user_id=current_user.id
        )
        return {"message": "Deleted"}

    @delete("/", guards=[require_permission("can_write", "Tag")], status_code=200)
    async def bulk_delete(
        self,
        dao: Any,
        rison_params: list[Any] | dict[str, Any] | None,
        current_user: UserProtocol,
    ) -> dict[str, Any]:
        """DELETE /api/v1/tag?q=!(name1,name2) -- bulk delete tags by name.

        Matches original Superset where ``delete_tags_schema`` is
        ``{"type": "array", "items": {"type": "string"}}`` — the rison
        payload is a list of *tag names*, not integer ids. See
        superset_old/tags/api.py:486 / tags/schemas.py:22.
        """
        if rison_params is None:
            raise SupersetValidationException(
                "tag names parameter is required and cannot be empty"
            )
        if isinstance(rison_params, list):
            tag_names_raw: list[Any] = rison_params
        elif isinstance(rison_params, dict):
            tag_names_raw = rison_params.get("ids") or rison_params.get("tags") or []
        else:
            tag_names_raw = []
        tag_names = [str(t) for t in tag_names_raw if t]
        if not tag_names:
            raise SupersetValidationException(
                "tag names parameter is required and cannot be empty"
            )
        cmd = BulkDeleteTagCommand(dao=dao, tag_names=tag_names)
        count = await cmd.execute()
        await event_logger.alog_with_context("tag.bulk_delete", user_id=current_user.id)
        return {"message": f"Deleted {count} tags"}

    @post(
        # No trailing slash — 1:1 with upstream ``@expose("/bulk_create")``
        # and the frontend POST to ``/api/v1/tag/bulk_create``; a trailing
        # slash here would force a 307 redirect round-trip.
        "/bulk_create",
        guards=[require_permission("can_write", "Tag")],
        # 1:1 with upstream ``response(200, result=...)`` — not 201.
        status_code=200,
    )
    async def bulk_create(
        self,
        data: BulkTagCreateSchema,
        dao: Any,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
    ) -> dict[str, Any]:
        tags_raw = [msgspec.structs.asdict(t) for t in data.tags]
        cmd = BulkCreateTagCommand(
            dao=dao,
            tags_data=tags_raw,
            security_manager=security_manager,
            current_user=current_user,
        )
        # ``{objects_tagged, objects_skipped}`` — the shape ``BulkTagModal``
        # consumes (it reads ``result.objects_tagged``/``objects_skipped``).
        result = await cmd.execute()
        await event_logger.alog_with_context("tag.bulk_create", user_id=current_user.id)
        return {"result": result}

    @get("/get_objects/", guards=[require_permission("can_read", "Tag")])
    async def get_objects(
        self,
        request: Request[Any, Any, Any],
        dao: Any,
    ) -> dict[str, Any]:
        """GET /api/v1/tag/get_objects/ -- get tagged objects by tag names or IDs."""
        tag_ids_raw = request.query_params.get("tagIds", "")
        tag_names_raw = request.query_params.get("tags", "")
        types_raw = request.query_params.get("types", "")

        tag_ids = (
            [int(t.strip()) for t in tag_ids_raw.split(",") if t.strip()]
            if tag_ids_raw
            else []
        )
        tag_names = (
            [t.strip() for t in tag_names_raw.split(",") if t.strip()]
            if tag_names_raw
            else []
        )
        types_filter = (
            [t.strip() for t in types_raw.split(",") if t.strip()]
            if types_raw
            else None
        )

        # tagIds takes priority over tag names (matches original)
        if tag_ids:
            tagged_objects = await dao.get_tagged_objects_by_tag_ids(
                tag_ids, obj_types=types_filter
            )
        else:
            tagged_objects = await dao.get_tagged_objects_by_tag_names(
                tag_names, obj_types=types_filter
            )

        # ``get_tagged_objects_by_tag_*`` now returns the entity-shaped dicts
        # ``{id, type, name, url, changed_on, created_by, creator, tags,
        # owners}`` matching upstream's ``TaggedObjectEntityResponseSchema``
        # (superset_old/tags/schemas.py:48). Previous port shape — raw
        # ``TaggedObject`` link rows ``{tag_id, object_id, object_type}`` —
        # broke the Tagged Objects page which reads ``.type``/``.name``/
        # ``.url`` from each entry. Pass-through unchanged.
        return {"result": tagged_objects}

    @get("/{pk:int}/favorites/")
    async def check_favorite(
        self,
        pk: int,
        dao: Any,
        current_user: UserProtocol,
    ) -> dict[str, Any]:
        """Check if tag is favorited by current user."""
        fav_ids = await dao.favorited_ids([pk], current_user.id)
        return {"result": {"id": pk, "value": pk in fav_ids}}

    @post("/{pk:int}/favorites/", status_code=200)
    async def add_favorite(
        self,
        pk: int,
        dao: Any,
        current_user: UserProtocol,
    ) -> dict[str, str]:
        """Add tag to favorites.

        1:1 with upstream superset_old/tags/api.py:689-693 which catches
        TagNotFoundError → 404; the DAO returns ``False`` for an
        unknown tag id, propagate as ObjectNotFoundError (also 404).
        """
        ok = await dao.favorite_tag_by_id_for_current_user(pk, current_user.id)
        if not ok:
            raise ObjectNotFoundError("Tag", pk)
        await event_logger.alog_with_context(
            "tag.add_favorite", object_ref=str(pk), user_id=current_user.id
        )
        return {"result": "OK"}

    @delete("/{pk:int}/favorites/", status_code=200)
    async def remove_favorite(
        self,
        pk: int,
        dao: Any,
        current_user: UserProtocol,
    ) -> dict[str, str]:
        """Remove tag from favorites — symmetric 404 on missing tag."""
        ok = await dao.remove_user_favorite_tag(pk, current_user.id)
        if not ok:
            raise ObjectNotFoundError("Tag", pk)
        await event_logger.alog_with_context(
            "tag.remove_favorite", object_ref=str(pk), user_id=current_user.id
        )
        return {"result": "OK"}

    # ------------------------------------------------------------------
    # POST /{object_type}/{object_id}/ -- add tags to an object
    # ------------------------------------------------------------------
    @post(
        "/{object_type:int}/{object_id:int}/",
        guards=[require_permission("can_write", "Tag")],
        status_code=201,
    )
    async def add_objects(
        self,
        object_type: int,
        object_id: int,
        data: AddTagsToObjectSchema,
        dao: Any,
        current_user: UserProtocol,
    ) -> dict[str, str]:
        """POST /api/v1/tag/{object_type}/{object_id}/ -- add tags to object.

        Creates new tags if they do not already exist and links them
        to the given object.
        """
        from superset.models.tags import ObjectType

        try:
            obj_type = ObjectType(object_type)
        except ValueError as exc:
            from superset.exceptions import SupersetValidationException

            # 1:1 with superset_old/tags/api.py:407-408 — a TagInvalidError
            # surfaces as 422 with the "Invalid tag" message.
            raise SupersetValidationException("Invalid tag") from exc

        await dao.create_custom_tagged_objects(
            object_type=obj_type.name,
            object_id=object_id,
            tag_names=data.properties.tags,
        )
        await event_logger.alog_with_context(
            "tag.add_objects",
            extra={"object_type": object_type, "object_id": object_id},
            user_id=current_user.id,
        )
        return {"message": "OK"}

    # ------------------------------------------------------------------
    # DELETE /{object_type}/{object_id}/{tag}/ -- remove tag from object
    # ------------------------------------------------------------------
    @delete(
        "/{object_type:int}/{object_id:int}/{tag:str}/",
        guards=[require_permission("can_write", "Tag")],
        status_code=200,
    )
    async def delete_object(
        self,
        object_type: int,
        object_id: int,
        tag: str,
        dao: Any,
        current_user: UserProtocol,
    ) -> dict[str, str]:
        """DELETE /api/v1/tag/{object_type}/{object_id}/{tag}/ -- remove tag."""
        from superset.models.tags import ObjectType

        try:
            obj_type = ObjectType(object_type)
        except ValueError as exc:
            from superset.exceptions import SupersetValidationException

            # 1:1 with superset_old/tags/api.py:462-463 — an invalid tag /
            # object type maps to TagInvalidError → 422. A missing tag or
            # tagged-object link instead raises ObjectNotFoundError (404)
            # from dao.delete_tagged_object below.
            raise SupersetValidationException("Invalid tag") from exc

        await dao.delete_tagged_object(
            object_type=obj_type.name,
            object_id=object_id,
            tag_name=tag,
        )
        await event_logger.alog_with_context(
            "tag.delete_object",
            extra={
                "object_type": object_type,
                "object_id": object_id,
                "tag": tag,
            },
            user_id=current_user.id,
        )
        return {"message": "OK"}

    # ------------------------------------------------------------------
    # GET /favorite_status/ -- batch check favorite status
    # ------------------------------------------------------------------
    @get("/favorite_status/", guards=[require_permission("can_read", "Tag")])
    async def favorite_status(
        self,
        dao: Any,
        rison_params: list[int] | dict[str, Any] | None,
        current_user: UserProtocol,
    ) -> dict[str, Any]:
        """GET /api/v1/tag/favorite_status/?q=(ids) -- batch favorite check."""
        requested_ids = extract_ids(rison_params)
        fav_ids = await dao.favorited_ids(requested_ids, current_user.id)
        return {
            "result": [{"id": rid, "value": rid in fav_ids} for rid in requested_ids]
        }

    # ------------------------------------------------------------------
    # GET /_info -- API metadata
    # ------------------------------------------------------------------
    @get("/_info", guards=[require_permission("can_read", "Tag")])
    async def info(self, dao: Any) -> dict[str, Any]:
        """GET /api/v1/tag/_info -- API metadata for frontend."""
        return await get_info_payload(
            dao=dao,
            model_name="Tag",
            permissions=["can_read", "can_write"],
        )

    # ------------------------------------------------------------------
    # GET /related/{column_name} -- related values for dropdowns
    # ------------------------------------------------------------------
    @get(
        "/related/{column_name:str}",
        guards=[require_permission("can_read", "Tag")],
    )
    async def related(
        self,
        column_name: str,
        dao: Any,
        rison_params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """GET /api/v1/tag/related/{column_name} -- related values."""
        return await get_related_payload(
            dao=dao,
            column_name=column_name,
            rison_params=rison_params,
            allowed_fields=frozenset({"created_by", "changed_by"}),
        )
