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
"""Async event manager — Redis stream-based event polling."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class AsyncEventManager:
    """Polls Redis stream for async query/chart job results."""

    def __init__(self, redis: Any) -> None:
        self._redis = redis

    async def read_events(
        self,
        channel: str,
        last_id: str = "0-0",
        count: int = 100,
        block_ms: int = 0,
    ) -> list[dict[str, Any]]:
        """XREAD from Redis stream."""
        if self._redis is None:
            return []
        try:
            result = await self._redis.xread(
                {channel: last_id}, count=count, block=block_ms
            )
            events: list[dict[str, Any]] = []
            for _stream_name, messages in result:
                for msg_id, data in messages:
                    event: dict[str, Any] = {"id": msg_id}
                    if isinstance(data, dict):
                        event.update(data)
                    events.append(event)
            return events
        except Exception:
            logger.warning("Failed to read events from %s", channel, exc_info=True)
            return []

    async def publish_event(self, channel: str, data: dict[str, Any]) -> str | None:
        """XADD to Redis stream. Returns event ID."""
        if self._redis is None:
            return None
        try:
            return await self._redis.xadd(channel, data)
        except Exception:
            logger.warning("Failed to publish event to %s", channel, exc_info=True)
            return None
