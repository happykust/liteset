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
"""Async events controller — poll for async job results."""

from __future__ import annotations

from typing import Any

from litestar import Controller, get
from litestar.connection import Request
from litestar.datastructures import State

from liteset.typing import UserProtocol


class AsyncEventsController(Controller):
    path = "/api/v1/async_event"
    tags = ["Async Events"]

    @get("/")
    async def poll_events(
        self,
        request: Request[Any, Any, Any],
        current_user: UserProtocol,
        state: State,
    ) -> dict[str, Any]:
        """GET /api/v1/async_event/ — poll async job events."""
        # Check GLOBAL_ASYNC_QUERIES feature flag
        feature_flags: dict[str, Any] = getattr(
            getattr(state, "settings", None), "feature_flags", {}
        )
        if not feature_flags.get("GLOBAL_ASYNC_QUERIES", False):
            return {"result": []}

        last_id: str = request.query_params.get("last_id", "0-0")

        redis: Any = getattr(state, "redis", None)
        if redis is None:
            return {"result": []}

        from liteset.async_events.manager import AsyncEventManager

        manager = AsyncEventManager(redis)

        channel = f"user_{current_user.id}"
        events = await manager.read_events(
            channel=channel,
            last_id=last_id,
        )

        return {"result": events}
