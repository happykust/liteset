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

from datetime import datetime
from typing import Any

import humanize
from litestar import Controller, get, post
from litestar.di import Provide

from superset.controllers.base import build_rison_query_params, serialize_list_response
from superset.events import event_logger
from superset.guards.rbac import require_authentication, require_permission
from superset.params.rison import provide_rison_query
from superset.providers import provide_log_dao
from superset.schemas.log import LogPostSchema
from superset.typing import UserProtocol


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
        from sqlalchemy.orm import selectinload

        from superset.models.core import Log

        rison_filters, order_by, page, page_size = build_rison_query_params(
            Log,
            rison_params,
        )
        items = await dao.find_all(
            filters=rison_filters or None,
            page=page,
            page_size=page_size,
            order_by=order_by,
            options=[selectinload(Log.user)],
        )
        total = await dao.count(filters=rison_filters or None)
        return serialize_list_response(
            items,
            total,
            [
                "id",
                "action",
                "user_id",
                "slice_id",
                "dashboard_id",
                "dttm",
                "json",
                "duration_ms",
                "referrer",
                "user.first_name",
                "user.last_name",
                "user.username",
            ],
            list_title="List Log",
        )

    @get(
        "/{pk:int}",
        guards=[require_permission("can_read", "Log")],
    )
    async def get_single(self, pk: int, dao: Any) -> dict[str, Any]:
        """GET /api/v1/log/{pk} — get single log entry."""
        from superset.exceptions import ObjectNotFoundError

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
        return {
            "id": item.id,
            "result": {
                "id": getattr(item, "id", None),
                "action": getattr(item, "action", None),
                "user_id": getattr(item, "user_id", None),
                "dashboard_id": getattr(item, "dashboard_id", None),
                "slice_id": getattr(item, "slice_id", None),
                "json": getattr(item, "json", None),
                "dttm": str(getattr(item, "dttm", "")),
                "duration_ms": getattr(item, "duration_ms", None),
                "referrer": getattr(item, "referrer", None),
            },
        }

    @get("/recent_activity/", guards=[require_authentication])
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
        actions = params.get("actions", ["mount_explorer", "mount_dashboard"])
        distinct = params.get("distinct", True)

        items = await dao.get_recent_activity(
            user_id=current_user.id,
            actions=actions,
            page=page,
            page_size=page_size,
        )

        # Batch-fetch dashboard titles and slice names so item_title
        # shows meaningful names instead of raw IDs.
        dashboard_ids = {item.dashboard_id for item in items if item.dashboard_id}
        slice_ids = {item.slice_id for item in items if item.slice_id}

        dashboard_titles = await dao.get_dashboard_titles(dashboard_ids)
        slice_names = await dao.get_slice_names(slice_ids)

        now = datetime.now()
        result = []
        seen: set[str] = set()
        for item in items:
            dashboard_id = getattr(item, "dashboard_id", None)
            slice_id = getattr(item, "slice_id", None)
            dttm = getattr(item, "dttm", None)

            # Determine item_type and item_url
            if dashboard_id:
                item_type = "dashboard"
                item_url = f"/superset/dashboard/{dashboard_id}/"
            elif slice_id:
                item_type = "slice"
                item_url = f"/explore/?slice_id={slice_id}"
            else:
                item_type = None
                item_url = None

            # Deduplicate by (action, item_type, item_id) when distinct=True
            if distinct:
                item_id = dashboard_id or slice_id or ""
                dedup_key = f"{getattr(item, 'action', '')}:{item_type}:{item_id}"
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)

            # Compute human-readable time delta
            time_delta_humanized = ""
            if dttm is not None:
                time_delta_humanized = humanize.naturaltime(now - dttm)

            result.append(
                {
                    "action": getattr(item, "action", ""),
                    "item_type": item_type,
                    "item_url": item_url,
                    "item_title": slice_names.get(int(slice_id), "")
                    if slice_id
                    else dashboard_titles.get(
                        int(dashboard_id), str(dashboard_id or "")
                    )
                    if dashboard_id
                    else "",
                    "time": dttm.timestamp() * 1000 if dttm else None,
                    "time_delta_humanized": time_delta_humanized,
                }
            )

        return {"result": result}
