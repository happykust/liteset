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
"""Polling REST API for async query events.

Preserves the existing frontend polling mechanism. The frontend calls
``GET /api/v1/async_event/?last_id=X`` at 500ms intervals to check
for completed async query results. This controller is the async
equivalent of Flask's ``AsyncEventsRestApi``.

Channel id is read from the ``async-token`` JWT cookie (minted by
:class:`superset.middleware.async_token.AsyncTokenMiddleware`) — this
mirrors the original Flask implementation exactly and ensures the polling
endpoint reads from the same Redis Stream that the Celery task wrote to.

The WebSocket endpoint (superset/websocket/events.py) provides real-time
delivery as an alternative, but this polling endpoint is preserved for
full backward compatibility with the existing frontend.
"""

from __future__ import annotations

import logging
from typing import Any

from litestar import Controller, get, Request
from litestar.exceptions import NotAuthorizedException

from superset.async_events.manager import AsyncEventManager
from superset.guards.rbac import require_authentication
from superset.middleware.async_token import resolve_async_channel_id_from_request
from superset.typing import UserProtocol

logger = logging.getLogger(__name__)

MAX_EVENT_COUNT = 100


class AsyncEventController(Controller):
    """REST controller for polling async query events."""

    path = "/api/v1/async_event"

    @get("/", guards=[require_authentication])
    async def get_events(
        self,
        request: Request,
        event_manager: AsyncEventManager,
        current_user: UserProtocol,
        last_id: str | None = None,
    ) -> dict[str, Any]:
        """Poll for async events since the given last_id.

        ---
        summary: Get async events
        description: >
            Returns async query events for the current user's channel.
            The channel is identified by the ``async-token`` JWT cookie
            that :class:`~superset.middleware.async_token.AsyncTokenMiddleware`
            mints on every authenticated response.  Pass ``last_id`` to
            get only events after that ID (exclusive).
        parameters:
          - name: last_id
            in: query
            required: false
            schema:
              type: string
        responses:
          200:
            description: List of events
        """
        # ---------------------------------------------------------------------------
        # Resolve channel_id from the JWT cookie — 1:1 with the original Flask path:
        #   AsyncQueryManager.parse_channel_id_from_request(request)
        #   → jwt.decode(cookie["async-token"])["channel"]
        # ---------------------------------------------------------------------------
        app = getattr(request, "app", None)
        app_state = getattr(app, "state", None)
        settings = getattr(app_state, "settings", None) if app_state else None

        channel_id: str | None = resolve_async_channel_id_from_request(
            request, settings
        )

        if not channel_id:
            # 1:1 with the original Flask path
            # (``superset_old/async_events/api.py:91-101``): a missing or
            # unparseable ``async-token`` cookie raises
            # ``AsyncQueryTokenException`` → ``self.response_401()``.  Mirror
            # the submit paths (``chart.py``/``explore_json.py``) which raise
            # ``NotAuthorizedException`` on the same condition rather than
            # silently returning an empty list from an unwritten channel.
            logger.debug("async-token cookie missing or invalid; returning 401")
            raise NotAuthorizedException(
                detail="Failed to parse async query channel token"
            )

        events = await event_manager.read_events(
            channel_id=channel_id,
            last_id=last_id,
            count=MAX_EVENT_COUNT,
        )

        return {"result": events}
