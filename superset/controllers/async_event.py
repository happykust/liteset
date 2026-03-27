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

The WebSocket endpoint (superset/websocket/events.py) provides real-time
delivery as an alternative, but this polling endpoint is preserved for
full backward compatibility with the existing frontend.
"""
from __future__ import annotations

import logging
from typing import Any

from litestar import Controller, get

from superset.async_events.manager import AsyncEventManager
from superset.guards.rbac import require_authentication
from superset.typing import UserProtocol

logger = logging.getLogger(__name__)


class AsyncEventController(Controller):
    """REST controller for polling async query events."""

    path = "/api/v1/async_event"

    @get("/", guards=[require_authentication])
    async def get_events(
        self,
        event_manager: AsyncEventManager,
        current_user: UserProtocol,
        last_id: str | None = None,
    ) -> dict[str, Any]:
        """Poll for async events since the given last_id.

        ---
        summary: Get async events
        description: >
            Returns async query events for the current user's channel.
            Pass ``last_id`` to get only events after that ID.
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
        # The channel_id is derived from the user's session.
        # In the original Flask implementation, channel_id is stored in the
        # user's session. Here we derive it from the user ID for simplicity,
        # matching the WebSocket channel pattern.
        channel_id = f"user-{current_user.id}"

        events = await event_manager.read_events(
            channel_id=channel_id,
            last_id=last_id,
        )

        return {"result": events}
