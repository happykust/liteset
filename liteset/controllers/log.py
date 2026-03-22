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
"""Log controller — activity logging and recent activity."""

from __future__ import annotations

from typing import Any

from litestar import Controller, get, post
from litestar.di import Provide

from liteset.controllers.base import extract_pagination, serialize_list_response
from liteset.events import event_logger
from liteset.guards.rbac import require_permission
from liteset.params.rison import provide_rison_query
from liteset.providers import provide_log_dao
from liteset.schemas.log import LogPostSchema
from liteset.typing import UserProtocol


class LogController(Controller):
    path = "/api/v1/log"
    tags = ["Log"]
    dependencies = {
        "dao": Provide(provide_log_dao, sync_to_thread=False),
        "rison_params": Provide(provide_rison_query),
    }

    @get(
        "/",
        guards=[require_permission("can_read", "Log")],
    )
    async def get_list(
        self,
        dao: Any,
        rison_params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """GET /api/v1/log/ — list log entries."""
        page, page_size = extract_pagination(rison_params)
        items = await dao.find_all(page=page, page_size=page_size)
        total = await dao.count()
        return serialize_list_response(
            items,
            total,
            ["id", "action", "user_id", "dashboard_id", "slice_id", "json", "dttm"],
        )

    @get(
        "/{pk:int}",
        guards=[require_permission("can_read", "Log")],
    )
    async def get_single(self, pk: int, dao: Any) -> dict[str, Any]:
        """GET /api/v1/log/{pk} — get single log entry."""
        from liteset.exceptions import ObjectNotFoundError

        item = await dao.find_by_id(pk)
        if item is None:
            raise ObjectNotFoundError("Log", pk)
        return {"result": item}

    @post(
        "/",
        guards=[require_permission("can_write", "Log")],
        status_code=201,
    )
    async def create_log(
        self,
        data: LogPostSchema,
        dao: Any,
        current_user: UserProtocol,
    ) -> dict[str, Any]:
        """POST /api/v1/log/ — create log entry."""
        import msgspec

        raw = msgspec.structs.asdict(data)
        raw["user_id"] = current_user.id
        item = await dao.create(raw)
        event_logger.log(
            "log.create",
            object_ref=f"log:{item.id}",
            user_id=current_user.id,
        )
        return {"id": item.id}

    @get("/recent_activity/")
    async def recent_activity(
        self,
        dao: Any,
        current_user: UserProtocol,
        rison_params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """GET /api/v1/log/recent_activity/ — recent activity for current user."""
        params = rison_params or {}
        page = params.get("page", 0)
        page_size = params.get("page_size", 25)
        actions = params.get("actions", ["explore", "dashboard"])

        items = await dao.get_recent_activity(
            user_id=current_user.id,
            actions=actions,
            page=page,
            page_size=page_size,
        )

        result = []
        for item in items:
            result.append(
                {
                    "action": getattr(item, "action", ""),
                    "item_type": getattr(item, "action", ""),
                    "item_id": getattr(item, "slice_id", None)
                    or getattr(item, "dashboard_id", None),
                    "item_title": getattr(item, "slice_id", ""),
                    "time": str(getattr(item, "dttm", "")),
                }
            )

        return {"result": result}
