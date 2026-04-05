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
"""Async event manager using Redis Streams.

Replaces superset.async_events.async_query_manager with an async-native
implementation using redis.asyncio. Supports both polling REST API and
WebSocket real-time delivery via Redis pub/sub notifications.

Redis data model:
- Global stream:  ``async-events-full`` (capped at 1M entries)
- Channel stream: ``async-events-{channel_id}`` (capped at 1K entries)
- Pub/sub topic:  ``events:{user_id}`` (for real-time WebSocket relay)
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from redis.asyncio import Redis

logger = logging.getLogger(__name__)


def build_job_metadata(
    channel_id: str,
    job_id: str,
    user_id: int | None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Build the event payload dict for a job status update."""
    return {
        "channel_id": channel_id,
        "job_id": job_id,
        "user_id": user_id,
        "status": kwargs.get("status"),
        "errors": kwargs.get("errors", []),
        "result_url": kwargs.get("result_url"),
    }


def parse_event(event_data: tuple[str, dict[str, Any]]) -> dict[str, Any]:
    """Parse a raw Redis Stream entry into an event dict."""
    event_id = event_data[0]
    event_payload = event_data[1]["data"]
    return {"id": event_id, **json.loads(event_payload)}


def increment_id(entry_id: str) -> str:
    """Increment a Redis Stream ID for exclusive range queries.

    Redis stream IDs are in the format ``1607477697866-0``.
    Incrementing the last digit ensures xrange excludes the starting entry.
    """
    try:
        prefix, seq = entry_id.rsplit("-", 1)
        return f"{prefix}-{int(seq) + 1}"
    except (ValueError, IndexError):
        return entry_id


class AsyncEventManager:
    """Async event manager backed by Redis Streams.

    Provides init_job, update_job, read_events for the polling REST API,
    and publishes notifications to Redis pub/sub for WebSocket relay.
    """

    def __init__(
        self,
        redis: Redis,
        stream_prefix: str = "async-events-",
        global_stream_key: str = "async-events-full",
        global_stream_limit: int = 1_000_000,
        channel_stream_limit: int = 1_000,
    ) -> None:
        self.redis = redis
        self.stream_prefix = stream_prefix
        self.global_stream_key = global_stream_key
        self.global_stream_limit = global_stream_limit
        self.channel_stream_limit = channel_stream_limit

    def _channel_key(self, channel_id: str) -> str:
        """Return the Redis Stream key for a specific channel."""
        return f"{self.stream_prefix}{channel_id}"

    async def init_job(self, user_id: int | None) -> tuple[str, str]:
        """Initialize a new async job. Returns (channel_id, job_id).

        Creates initial "pending" event in both the channel-specific stream
        and the global firehose stream.
        """
        channel_id = str(uuid.uuid4())
        job_id = str(uuid.uuid4())
        metadata = build_job_metadata(
            channel_id=channel_id,
            job_id=job_id,
            user_id=user_id,
            status="pending",
        )
        payload = {"data": json.dumps(metadata)}

        # Write to channel-specific stream
        await self.redis.xadd(
            self._channel_key(channel_id),
            payload,  # type: ignore[arg-type]
        )
        # Write to global firehose stream
        await self.redis.xadd(
            self.global_stream_key,
            payload,  # type: ignore[arg-type]
        )

        logger.debug(
            "Initialized job %s on channel %s for user %s",
            job_id,
            channel_id,
            user_id,
        )
        return channel_id, job_id

    async def update_job(
        self,
        channel_id: str,
        job_id: str,
        user_id: int | None,
        status: str = "running",
        errors: list[dict[str, Any]] | None = None,
        result_url: str | None = None,
    ) -> None:
        """Update job status. Writes to streams and publishes pub/sub notification."""
        metadata = build_job_metadata(
            channel_id=channel_id,
            job_id=job_id,
            user_id=user_id,
            status=status,
            errors=errors or [],
            result_url=result_url,
        )
        payload = {"data": json.dumps(metadata)}

        # Write to channel stream
        await self.redis.xadd(
            self._channel_key(channel_id),
            payload,  # type: ignore[arg-type]
        )
        # Write to global stream
        await self.redis.xadd(
            self.global_stream_key,
            payload,  # type: ignore[arg-type]
        )

        # Publish notification for WebSocket relay
        if user_id is not None:
            await self.redis.publish(
                f"events:{user_id}",
                json.dumps(metadata),
            )

        logger.debug(
            "Updated job %s on channel %s: status=%s",
            job_id,
            channel_id,
            status,
        )

    async def read_events(
        self,
        channel_id: str,
        last_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Read events from a channel stream, optionally starting after last_id.

        Used by the polling REST API endpoint.
        """
        start = increment_id(last_id) if last_id else "-"
        raw_events: list[tuple[str, dict[str, Any]]] = await self.redis.xrange(
            self._channel_key(channel_id),
            start,
            "+",
        )
        return [parse_event(event) for event in raw_events]

    async def cleanup_channel(self, channel_id: str) -> None:
        """Trim a channel stream if it exceeds the configured limit."""
        key = self._channel_key(channel_id)
        length: int = await self.redis.xlen(key)
        if length > self.channel_stream_limit:
            await self.redis.xtrim(key, maxlen=self.channel_stream_limit)
            logger.debug(
                "Trimmed channel %s from %d to %d entries",
                channel_id,
                length,
                self.channel_stream_limit,
            )

    async def cleanup_global_stream(self) -> None:
        """Trim the global firehose stream if it exceeds the configured limit."""
        length: int = await self.redis.xlen(self.global_stream_key)
        if length > self.global_stream_limit:
            await self.redis.xtrim(
                self.global_stream_key,
                maxlen=self.global_stream_limit,
            )
            logger.debug(
                "Trimmed global stream from %d to %d entries",
                length,
                self.global_stream_limit,
            )

    async def cleanup_stale_channels(self, max_idle_seconds: int = 120) -> None:
        """Delete Redis Stream keys for channels with no recent activity.

        Scans all channel streams matching ``self.stream_prefix*``.
        For each stream, checks the timestamp of the last entry.
        If older than ``max_idle_seconds``, deletes the stream key.

        This prevents unbounded growth of abandoned channel streams
        (e.g., when a user closes their browser without a clean disconnect).
        """
        import time

        cursor: int = 0
        pattern = f"{self.stream_prefix}*"
        now = time.time()
        deleted = 0

        while True:
            _cursor, keys = await self.redis.scan(
                cursor=cursor,
                match=pattern,
                count=100,
            )
            cursor = int(_cursor)
            for key in keys:
                # XREVRANGE with COUNT 1 returns the newest entry
                entries = await self.redis.xrevrange(key, count=1)
                if not entries:
                    # Empty stream — delete
                    await self.redis.delete(key)
                    deleted += 1
                    continue
                entry_id = entries[0][0]
                # Redis stream IDs are "{milliseconds}-{seq}"
                try:
                    ts_ms = int(entry_id.split("-")[0])
                    if (now - ts_ms / 1000) > max_idle_seconds:
                        await self.redis.delete(key)
                        deleted += 1
                except (ValueError, IndexError):
                    pass
            if cursor == 0:
                break

        if deleted:
            logger.info("Cleaned up %d stale channel streams", deleted)
