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

import json
from datetime import datetime
from typing import Any
from urllib import parse

import humanize
from litestar import Controller, get, post
from litestar.connection import ASGIConnection
from litestar.di import Provide
from litestar.handlers import BaseRouteHandler

from superset.controllers.base import build_rison_query_params, serialize_list_response
from superset.events import event_logger
from superset.guards.rbac import (
    require_authenticated_user,
    require_authentication,
    require_permission,
)
from superset.params.rison import provide_rison_query
from superset.providers import provide_log_dao
from superset.schemas.log import LogPostSchema
from superset.typing import UserProtocol
from superset.utils.dates import datetime_to_epoch


def _require_log_views_enabled(
    connection: ASGIConnection[Any, Any, Any, Any], _: BaseRouteHandler
) -> None:
    """Guard that returns 404 when log views are disabled via config.

    1:1 with the original ``LogRestApi.is_enabled()`` /
    ``@before_request ensure_enabled`` (superset_old/views/log/api.py:88-96)
    which returns 404 when ``FAB_ADD_SECURITY_VIEWS`` or
    ``SUPERSET_LOG_VIEW`` is ``False``.
    """
    from litestar.exceptions import NotFoundException

    from superset.config import SupersetSettings

    settings = getattr(connection.app.state, "settings", None)
    if settings is None:  # pragma: no cover — state always set in the app
        settings = SupersetSettings()  # type: ignore[call-arg]

    if not settings.fab_add_security_views or not settings.superset_log_view:
        raise NotFoundException(detail="Not found")


class LogController(Controller):
    path = "/api/v1/log"
    tags = ["Log"]
    dependencies = {
        "dao": Provide(provide_log_dao, sync_to_thread=False),
        "rison_params": Provide(provide_rison_query),
    }

    @get(
        "/",
        guards=[
            _require_log_views_enabled,
            require_authenticated_user,
            require_permission("can_read", "Log"),
        ],
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
            # ``LogRestApi.page_size = 20`` (superset_old/views/log/api.py:76).
            default_page_size=20,
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
        guards=[
            _require_log_views_enabled,
            require_authenticated_user,
            require_permission("can_read", "Log"),
        ],
    )
    async def get_single(self, pk: int, dao: Any) -> dict[str, Any]:
        """GET /api/v1/log/{pk} — get single log entry.

        msgspec cannot serialize the SA ``Log`` ORM instance directly, so
        the response mirrors original Superset's Marshmallow ``LogModelView``
        dump shape.
        """
        from sqlalchemy.orm import selectinload

        from superset.exceptions import ObjectNotFoundError
        from superset.models.core import Log

        items = await dao.find_all(
            filters=[Log.id == pk], options=[selectinload(Log.user)]
        )
        item = items[0] if items else None
        if item is None:
            raise ObjectNotFoundError("Log", pk)
        user = getattr(item, "user", None)
        return {
            "id": pk,
            "result": {
                "id": getattr(item, "id", None),
                "action": getattr(item, "action", None),
                "user_id": getattr(item, "user_id", None),
                "dashboard_id": getattr(item, "dashboard_id", None),
                "slice_id": getattr(item, "slice_id", None),
                "json": getattr(item, "json", None),
                "dttm": str(getattr(item, "dttm", "") or ""),
                "duration_ms": getattr(item, "duration_ms", None),
                "referrer": getattr(item, "referrer", None),
                "user": {
                    "first_name": getattr(user, "first_name", None),
                    "last_name": getattr(user, "last_name", None),
                    "username": getattr(user, "username", None),
                }
                if user is not None
                else None,
            },
        }

    @post(
        "/",
        guards=[
            _require_log_views_enabled,
            require_authenticated_user,
            require_permission("can_write", "Log"),
        ],
        status_code=201,
    )
    async def create_log(
        self,
        data: LogPostSchema,
        dao: Any,
        current_user: UserProtocol,
    ) -> dict[str, Any]:
        """POST /api/v1/log/ — create log entry.

        Only ``id`` is accepted in the payload (matches FAB's default
        ``add_columns = [<pk>]`` behaviour); ``user_id`` is set from the
        authenticated user. ``action`` and the other Log columns are
        populated by call sites (event logger), not by external POSTs.
        """
        import msgspec

        raw = msgspec.structs.asdict(data)
        # Drop the optional id (let the DB auto-increment) and stamp user.
        raw.pop("id", None)
        raw["user_id"] = current_user.id
        item = await dao.create(raw)
        await event_logger.alog_with_context(
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

    @get(
        "/recent_activity/",
        guards=[
            require_authentication,
            require_permission("can_recent_activity", "Log"),
        ],
    )
    async def recent_activity(
        self,
        dao: Any,
        current_user: UserProtocol,
        rison_params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """GET /api/v1/log/recent_activity/ — recent activity for current user."""
        params = rison_params or {}
        page = params.get("page", 0)
        # Mirror FAB _sanitize_page_args: clamp to FAB_API_MAX_PAGE_SIZE (default 100).
        # LogRestApi does not override max_page_size, so the cap is always 100.
        page_size = min(params.get("page_size", 20), 100)
        actions = params.get("actions", ["mount_explorer", "mount_dashboard"])
        distinct = params.get("distinct", True)

        items = await dao.get_recent_activity(
            user_id=current_user.id,
            actions=actions,
            distinct=distinct,
            page=page,
            page_size=page_size,
        )

        now = datetime.utcnow()
        result = []
        seen: set[str] = set()
        for item in items:
            dashboard_id = getattr(item, "dashboard_id", None)
            slice_id = getattr(item, "slice_id", None)
            dttm = getattr(item, "dttm", None)

            # Determine item_type and item_url
            dashboard_slug = getattr(item, "dashboard_slug", None)
            if dashboard_id:
                item_type = "dashboard"
                item_url = f"/superset/dashboard/{dashboard_slug or dashboard_id}/"
            elif slice_id:
                item_type = "slice"
                # Mirror Slice.build_explore_url() (superset_old/models/slice.py:309)
                form_data_param = parse.quote(json.dumps({"slice_id": slice_id}))
                item_url = f"/explore/?slice_id={slice_id}&form_data={form_data_param}"
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
                    # Dashboard-first priority — mirrors the original if/elif block
                    # in superset_old/daos/log.py:128-135.  Both
                    # get_recent_activity query paths already JOIN Dashboard and
                    # Slice and SELECT dashboard_title / slice_name, so we read
                    # them directly from the row rather than issuing a redundant
                    # batch-fetch that could miss in a race condition.
                    "item_title": getattr(item, "dashboard_title", None)
                    if dashboard_id
                    else (getattr(item, "slice_name", None) or "<empty>")
                    if slice_id
                    else None,
                    "time": datetime_to_epoch(dttm) if dttm else None,
                    "time_delta_humanized": time_delta_humanized,
                }
            )

        return {"result": result}
