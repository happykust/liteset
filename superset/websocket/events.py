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
"""WebSocket handler for async query events.

Replaces the Node.js ``superset-websocket`` module with a native Litestar
WebSocket handler. Uses Redis pub/sub for real-time event streaming with
backpressure via an asyncio.Queue and heartbeat via TaskGroup.

Custom close codes:
- 4401: Unauthorized (JWT invalid or missing)
- 4403: Forbidden (origin not allowed)
- 4429: Too many connections (per-user limit exceeded)
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from litestar import Controller, websocket
from litestar.connection import WebSocket
from litestar.datastructures import State
from litestar.exceptions import WebSocketDisconnect

from superset.websocket.auth import authenticate_websocket, validate_origin

# Graceful disconnect exceptions — import defensively
try:
    from uvicorn.protocols.utils import ClientDisconnected as _ClientDisconnected
except ImportError:
    _ClientDisconnected = ConnectionError  # type: ignore[assignment,misc]
try:
    from websockets.exceptions import ConnectionClosed as _ConnectionClosed
except ImportError:
    _ConnectionClosed = ConnectionError  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)

# Custom WebSocket close codes
WS_CLOSE_UNAUTHORIZED = 4401
WS_CLOSE_FORBIDDEN_ORIGIN = 4403
WS_CLOSE_TOO_MANY_CONNECTIONS = 4429

# Defaults (overridable via settings)
MAX_WS_PER_USER = 10
HEARTBEAT_INTERVAL_SECONDS = 30
SEND_QUEUE_MAX_SIZE = 64
REDIS_RECONNECT_DELAY_SECONDS = 1

# Lock protecting the check-accept-register sequence to prevent TOCTOU races
# on the per-user connection limit.  Created lazily to avoid binding to the
# wrong event loop when the module is imported at startup.
_ws_accept_lock: asyncio.Lock | None = None


def _get_ws_lock() -> asyncio.Lock:
    """Return the module-level WebSocket accept lock, creating it lazily."""
    global _ws_accept_lock
    if _ws_accept_lock is None:
        _ws_accept_lock = asyncio.Lock()
    return _ws_accept_lock


class AsyncQueryWebSocket(Controller):
    """WebSocket controller for async query event streaming.

    Path: /ws/events

    Authentication:
        JWT token via query parameter ``?token=<jwt>`` or HTTP-only cookie fallback.
        Browsers cannot send custom headers during WebSocket handshake, so query
        parameters are the standard approach.

    Event flow:
        1. Client connects with JWT containing ``{channel, sub}``
        2. Server validates JWT and origin, then accepts connection
        3. Server subscribes to Redis pub/sub channel ``events:{user_id}``
        4. Events are relayed from Redis -> asyncio.Queue -> WebSocket
        5. Heartbeat ping sent every 30s; client must respond with pong
        6. On disconnect, Redis subscription and socket are cleaned up
    """

    path = "/"
    # Skip Litestar's session-based auth middleware for WebSocket.
    # The handler performs its own JWT auth via authenticate_websocket()
    # BEFORE accepting the connection — unauthenticated clients receive
    # a close frame (4401) without any data being sent.
    opt = {"exclude_from_auth": True}

    @websocket("/ws")
    async def on_event(self, socket: WebSocket[Any, Any, Any], state: State) -> None:  # noqa: C901
        """Handle WebSocket connection for async query events."""
        settings = state.settings

        # Origin validation (CORS does not apply to WebSocket upgrade)
        origin = socket.headers.get("origin", "")
        allowed_origins: list[str] = getattr(settings, "cors_allow_origins", []) or []
        if allowed_origins and not validate_origin(origin, allowed_origins):
            await socket.accept()
            await socket.close(code=WS_CLOSE_FORBIDDEN_ORIGIN)
            logger.warning("WebSocket rejected: forbidden origin %s", origin)
            return

        # Authenticate before accepting
        jwt_secret = settings.secret_key
        if hasattr(jwt_secret, "get_secret_value"):
            jwt_secret = jwt_secret.get_secret_value()

        session_cookie_name = getattr(settings, "session_cookie_name", "session")
        auth_result = await authenticate_websocket(
            socket,
            jwt_secret=jwt_secret,
            session_cookie_name=session_cookie_name,
        )
        if auth_result is None:
            await socket.accept()
            await socket.close(code=WS_CLOSE_UNAUTHORIZED)
            logger.debug("WebSocket rejected: authentication failed")
            return

        # --- Per-user connection limit enforcement ---
        # The lock serialises the check-accept-register sequence so that
        # concurrent handshakes for the same user cannot both pass the limit
        # check before either is registered (TOCTOU race).
        max_ws: int = getattr(settings, "max_ws_per_user", MAX_WS_PER_USER)
        active_ws: dict[WebSocket[Any, Any, Any], int] = state.active_websockets

        async with _get_ws_lock():
            user_ws_count = sum(
                1 for uid in active_ws.values() if uid == auth_result.user_id
            )
            if user_ws_count >= max_ws:
                await socket.accept()
                await socket.close(code=WS_CLOSE_TOO_MANY_CONNECTIONS)
                logger.warning(
                    "WebSocket rejected: user %d has %d connections (max %d)",
                    auth_result.user_id,
                    user_ws_count,
                    max_ws,
                )
                return

            await socket.accept()
            active_ws[socket] = auth_result.user_id
        channel = f"events:{auth_result.user_id}"

        logger.info(
            "WebSocket accepted: user=%d, channel=%s",
            auth_result.user_id,
            channel,
        )

        # --- Catch-up: deliver missed events before subscribing to live stream ---
        last_id = socket.query_params.get("last_id")
        if last_id:
            from superset.async_events.manager import AsyncEventManager

            event_manager = AsyncEventManager(redis=state.redis)
            missed_events = await event_manager.read_events(
                channel_id=auth_result.channel,
                last_id=last_id,
            )
            for event in missed_events:
                await socket.send_json(event)
            logger.debug(
                "Sent %d catch-up events to user %d (last_id=%s)",
                len(missed_events),
                auth_result.user_id,
                last_id,
            )

        relay_task = asyncio.create_task(self._relay_events(socket, state, channel))
        heartbeat_task = asyncio.create_task(self._heartbeat(socket))
        try:
            done, pending = await asyncio.wait(
                [relay_task, heartbeat_task],
                return_when=asyncio.FIRST_EXCEPTION,
            )
            for task in pending:
                task.cancel()
            # Re-raise non-disconnect exceptions
            for task in done:
                exc = task.exception()
                if exc is not None and not isinstance(
                    exc,
                    (
                        WebSocketDisconnect,
                        ConnectionError,
                        OSError,
                        _ClientDisconnected,
                        _ConnectionClosed,
                    ),
                ):
                    logger.exception(
                        "WebSocket error for user %d",
                        auth_result.user_id,
                        exc_info=exc,
                    )
        except (
            WebSocketDisconnect,
            ConnectionError,
            OSError,
            _ClientDisconnected,
            _ConnectionClosed,
        ):
            pass  # Client disconnected — clean exit
        finally:
            active_ws.pop(socket, None)
            try:
                await socket.close()
            except Exception:  # noqa: S110
                pass  # Socket may already be closed
            logger.info("WebSocket closed: user=%d", auth_result.user_id)

    @staticmethod
    async def _relay_events(  # noqa: C901
        socket: WebSocket[Any, Any, Any],
        state: State,
        channel: str,
    ) -> None:
        """Subscribe to Redis pub/sub and forward events to WebSocket.

        Backpressure strategy:
            A bounded ``asyncio.Queue(maxsize=64)`` sits between the Redis
            subscriber (producer) and the WebSocket sender (consumer).
        """
        redis = state.redis

        send_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=SEND_QUEUE_MAX_SIZE,
        )

        async def _producer() -> None:
            while True:
                pubsub = redis.pubsub()
                try:
                    await pubsub.subscribe(channel)
                    async for message in pubsub.listen():
                        if message["type"] != "message":
                            continue
                        data = message["data"]
                        if isinstance(data, bytes):
                            data = data.decode("utf-8")
                        try:
                            parsed = json.loads(data)
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            logger.warning(
                                "Malformed event on channel %s, skipping",
                                channel,
                            )
                            continue
                        if send_queue.full():
                            try:
                                send_queue.get_nowait()  # drop oldest stale event
                            except asyncio.QueueEmpty:
                                pass
                        await send_queue.put(parsed)
                except Exception:
                    logger.debug(
                        "Redis pub/sub connection lost for %s, reconnecting...",
                        channel,
                    )
                    await asyncio.sleep(REDIS_RECONNECT_DELAY_SECONDS)
                    continue  # re-subscribe
                finally:
                    try:
                        await pubsub.unsubscribe(channel)
                        await pubsub.close()
                    except Exception:  # noqa: BLE001, S110
                        pass  # Best-effort cleanup

        async def _consumer() -> None:
            while True:
                message = await send_queue.get()
                await socket.send_json(message)

        async with asyncio.TaskGroup() as tg:
            tg.create_task(_producer())
            tg.create_task(_consumer())

    @staticmethod
    async def _heartbeat(socket: WebSocket[Any, Any, Any]) -> None:
        """Send periodic ping frames to detect dead connections."""
        import time

        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
            await socket.send_json({"type": "ping", "timestamp": time.time()})
