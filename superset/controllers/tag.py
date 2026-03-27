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
    extract_ids_required,
    extract_pagination,
    serialize_list_response,
)
from superset.events import event_logger
from superset.guards.rbac import require_permission
from superset.params.rison import provide_rison_query
from superset.providers import provide_tag_dao
from superset.schemas.tag import BulkTagCreateSchema, TagPostSchema, TagPutSchema
from superset.typing import UserProtocol
from superset.utils import filter_unset

_LIST_COLUMNS = ["id", "name", "description", "type"]


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
        page, page_size = extract_pagination(rison_params)
        items = await dao.find_all(page=page, page_size=page_size)
        total = await dao.count()
        return serialize_list_response(items, total, _LIST_COLUMNS)

    @get("/{pk:int}", guards=[require_permission("can_read", "Tag")])
    async def get_single(self, pk: int, dao: Any) -> dict[str, Any]:
        from superset.exceptions import ObjectNotFoundError

        item = await dao.find_by_id(pk)
        if item is None:
            raise ObjectNotFoundError("Tag", pk)
        return {"result": item}

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
        event_logger.log(
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
        event_logger.log("tag.update", object_ref=str(pk), user_id=current_user.id)
        return {"result": {"name": getattr(item, "name", "")}}

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
        event_logger.log("tag.delete", object_ref=str(pk), user_id=current_user.id)
        return {"message": "Deleted"}

    @delete("/", guards=[require_permission("can_write", "Tag")], status_code=200)
    async def bulk_delete(
        self,
        dao: Any,
        rison_params: dict[str, Any] | None,
        current_user: UserProtocol,
    ) -> dict[str, Any]:
        ids = extract_ids_required(rison_params)
        cmd = BulkDeleteTagCommand(dao=dao, ids=ids)
        count = await cmd.execute()
        event_logger.log("tag.bulk_delete", user_id=current_user.id)
        return {"message": f"Deleted {count} tags"}

    @post(
        "/bulk_create/",
        guards=[require_permission("can_write", "Tag")],
        status_code=201,
    )
    async def bulk_create(
        self,
        data: BulkTagCreateSchema,
        dao: Any,
        current_user: UserProtocol,
    ) -> dict[str, Any]:
        tags_raw = [msgspec.structs.asdict(t) for t in data.tags]
        cmd = BulkCreateTagCommand(dao=dao, tags_data=tags_raw)
        results = await cmd.execute()
        event_logger.log("tag.bulk_create", user_id=current_user.id)
        return {
            "result": [
                {"id": getattr(t, "id", None), "name": getattr(t, "name", "")}
                for t in results
            ]
        }

    @get("/get_objects/", guards=[require_permission("can_read", "Tag")])
    async def get_objects(
        self,
        request: Request[Any, Any, Any],
        dao: Any,
    ) -> dict[str, Any]:
        """GET /api/v1/tag/get_objects/ -- get tagged objects by tag names."""
        tag_names_raw = request.query_params.get("tags", "")
        types_raw = request.query_params.get("types", "")

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

        tagged_objects = await dao.get_tagged_objects_by_tag_names(
            tag_names, obj_types=types_filter
        )

        result: list[dict[str, Any]] = []
        for obj in tagged_objects:
            obj_type = getattr(obj, "object_type", None)
            result.append(
                {
                    "tag_id": getattr(obj, "tag_id", None),
                    "object_id": getattr(obj, "object_id", None),
                    "object_type": str(obj_type) if obj_type else None,
                }
            )

        return {"result": result}

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

    @post("/{pk:int}/favorites/", status_code=201)
    async def add_favorite(
        self,
        pk: int,
        dao: Any,
        current_user: UserProtocol,
    ) -> dict[str, str]:
        """Add tag to favorites."""
        await dao.favorite_tag_by_id_for_current_user(pk, current_user.id)
        event_logger.log(
            "tag.add_favorite", object_ref=str(pk), user_id=current_user.id
        )
        return {"message": "OK"}

    @delete("/{pk:int}/favorites/", status_code=200)
    async def remove_favorite(
        self,
        pk: int,
        dao: Any,
        current_user: UserProtocol,
    ) -> dict[str, str]:
        """Remove tag from favorites."""
        await dao.remove_user_favorite_tag(pk, current_user.id)
        event_logger.log(
            "tag.remove_favorite", object_ref=str(pk), user_id=current_user.id
        )
        return {"message": "OK"}
