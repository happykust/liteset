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
WebSocket handler.

Like the original ``superset-websocket/src/index.ts``, events are delivered
by reading a per-channel Redis Stream via ``XREAD BLOCK`` and forwarding each
entry — with its stream ``id`` attached — as a JSON frame.  Starting the read
from ``increment_id(last_id)`` returns any missed backlog immediately and then
blocks for new entries, so a single read loop handles both reconnection
catch-up and live delivery.

Keepalive is handled at the WebSocket protocol level by uvicorn's built-in
ping/pong (``ws_ping_interval``/``ws_ping_timeout``); the server never sends
application-level JSON frames that are not real async events, because the
frontend (``superset-frontend/src/middleware/asyncEvent.ts``) parses *every*
inbound frame as an ``AsyncEvent``.

Custom close codes:
- 4401: Unauthorized (JWT invalid or missing)
- 4403: Forbidden (origin not allowed)
- 4429: Too many connections (per-user limit exceeded)
- 4400: Invalid channel claim (missing, or colliding with the reserved
  global-stream channel name)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

from litestar import Controller, websocket
from litestar.connection import WebSocket
from litestar.datastructures import State
from litestar.exceptions import WebSocketDisconnect

from superset.async_events.manager import increment_id, parse_event
from superset.extensions import stats_logger_manager
from superset.middleware.async_token import _resolve_verified_secret_key
from superset.websocket.auth import (
    authenticate_websocket,
    is_same_origin,
    validate_origin,
)

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
WS_CLOSE_INVALID_CHANNEL = 4400

# Reserved channel-name suffix for the global firehose stream (mirrors
# ``superset.async_events.manager``'s ``global_stream_key`` construction:
# ``f"{stream_prefix}full"``).  A forged JWT ``channel`` claim equal to this
# would resolve to the SAME Redis Stream key every user's events are also
# written to — granting a live read of every user's async-query events
# (security audit finding H5, point 3).
RESERVED_GLOBAL_CHANNEL_NAME = "full"

# Defaults (overridable via settings)
MAX_WS_PER_USER = 10
REDIS_RECONNECT_DELAY_SECONDS = 1
# Node sidecar defaults (redisStreamReadBlockMs=5000,
# redisStreamReadCount=100).
DEFAULT_STREAM_READ_BLOCK_MS = 5000
DEFAULT_STREAM_READ_COUNT = 100
# Sentinel cursor meaning "only new entries from now on" (XREAD "$").
ONLY_NEW_CURSOR = "$"

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
        3. Channel is the per-browser UUID from the JWT ``channel`` claim
           (set by ``AsyncTokenMiddleware`` in the ``async-token`` cookie).
           If the channel claim is absent (rare: session-cookie-only auth),
           the relay idles until the client disconnects.
        4. Events are relayed from the per-channel Redis Stream via
           ``XREAD BLOCK`` -> WebSocket, each carrying its stream ``id``
        5. Keepalive is handled by uvicorn's protocol-level WebSocket ping
        6. On disconnect, the read loop is cancelled and the socket cleaned up
    """

    path = "/"
    # Skip Litestar's session-based auth middleware for WebSocket.
    # The handler performs its own JWT auth via authenticate_websocket()
    # BEFORE accepting the connection — unauthenticated clients receive
    # a close frame (4401) without any data being sent.
    opt = {"exclude_from_auth": True}

    @websocket("/ws/events")
    async def on_event(self, socket: WebSocket[Any, Any, Any], state: State) -> None:  # noqa: C901
        """Handle WebSocket connection for async query events.

        Path: /ws/events
        """
        settings = state.settings

        # Origin validation (CORS does not apply to WebSocket upgrade).
        # Sources, in order of trust:
        #  1. Same-origin browser tab — always allowed regardless of CORS
        #     config (CORS_OPTIONS governs cross-origin *fetches* the
        #     frontend makes, e.g. to map-tile servers — not who may open
        #     the app's own WebSocket).
        #  2. An origin explicitly listed in CORS_OPTIONS['origins'] (the
        #     upstream-compatible surface — also applied to the HTTP CORS
        #     middleware in app.py:_build_cors_config) or the Liteset-only
        #     ``cors_allow_origins`` (kept for backward compatibility; the
        #     key it maps from does not exist in upstream Superset).
        #  3. No Origin header at all — not a browser-driven request, so
        #     not a cross-site-WebSocket-hijacking vector; let it through
        #     to the auth check below.
        # Anything else is REJECTED even when nothing is configured — the
        # previous ``if allowed_origins and ...`` skipped validation
        # entirely whenever no allow-list was configured, which is the
        # common case, since CORS_ALLOW_ORIGINS is not even an upstream
        # config key (security audit finding M20).
        origin = socket.headers.get("origin", "")
        host_header = socket.headers.get("host", "")
        cors_options: dict[str, Any] = getattr(settings, "cors_options", None) or {}
        allowed_origins: list[str] = list(cors_options.get("origins") or []) + list(
            getattr(settings, "cors_allow_origins", None) or []
        )
        if (
            origin
            and not is_same_origin(origin, host_header)
            and not (allowed_origins and validate_origin(origin, allowed_origins))
        ):
            await socket.accept()
            await socket.close(code=WS_CLOSE_FORBIDDEN_ORIGIN)
            logger.warning("WebSocket rejected: forbidden origin %s", origin)
            return

        # Authenticate before accepting.
        # The async-token cookie is signed with global_async_queries_jwt_secret
        # (default "test-secret-change-me"), NOT with secret_key.  Use the same
        # canonical helper as AsyncTokenMiddleware and the polling endpoint:
        # prefer global_async_queries_jwt_secret, fall back to secret_key,
        # unwrap SecretStr if needed.  Fails closed (returns None) on an
        # empty or known-default secret rather than verifying against a
        # public string (security audit finding H5) — refuse the connection
        # immediately in that case, without even attempting to decode a
        # token.
        jwt_secret: str | None = _resolve_verified_secret_key(settings)
        if jwt_secret is None:
            await socket.accept()
            await socket.close(code=WS_CLOSE_UNAUTHORIZED)
            logger.warning(
                "WebSocket rejected: GLOBAL_ASYNC_QUERIES_JWT_SECRET is unset "
                "or a known shipped default; refusing to verify any channel token"
            )
            return

        cookie_name: str = getattr(
            settings, "global_async_queries_jwt_cookie_name", "async-token"
        )
        session_cookie_name = getattr(settings, "session_cookie_name", "session")
        allow_query_token = bool(
            getattr(settings, "global_async_queries_ws_allow_query_token", False)
        )
        auth_result = await authenticate_websocket(
            socket,
            jwt_secret=jwt_secret,
            cookie_name=cookie_name,
            session_cookie_name=session_cookie_name,
            allow_query_token=allow_query_token,
        )
        if auth_result is None:
            await socket.accept()
            await socket.close(code=WS_CLOSE_UNAUTHORIZED)
            logger.debug("WebSocket rejected: authentication failed")
            return

        # Resolve the per-browser channel id early and reject a claim that
        # collides with the reserved global-stream channel name BEFORE the
        # connection is accepted/registered — accepting it would grant a
        # live read of EVERY user's async-query events (audit finding H5,
        # point 3).  A genuinely missing/empty channel (rare: session-cookie
        # -only auth with no async-token cookie) is intentionally left to
        # idle further down rather than rejected outright: it never
        # resolves a stream key, so nothing is exposed by keeping the
        # socket open — see ``_relay_events``.
        channel = auth_result.channel
        if channel and channel == RESERVED_GLOBAL_CHANNEL_NAME:
            await socket.accept()
            await socket.close(code=WS_CLOSE_INVALID_CHANNEL)
            logger.warning(
                "WebSocket rejected: channel claim %r collides with the "
                "reserved global stream channel name",
                channel,
            )
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
            # Increment the ws_connected_client metric for monitoring.
            stats_logger_manager.incr("ws_connected_client")

        # ``channel`` was already resolved (and validated against the
        # reserved global-stream name) above, before this connection was
        # accepted/registered.
        logger.info(
            "WebSocket accepted: user=%d, channel=%s",
            auth_result.user_id,
            channel,
        )

        # The ``last_id`` query param is appended by the frontend on
        # reconnection.  The relay loop starts the stream read from
        # ``increment_id(last_id)`` so the missed backlog is delivered before
        # blocking for live entries — no separate catch-up pass is needed.
        last_id = socket.query_params.get("last_id")

        relay_task = asyncio.create_task(
            self._relay_events(socket, state, channel, last_id)
        )
        # The frontend never sends data over this socket; the receiver loop
        # exists solely to surface client disconnects promptly.
        receiver_task = asyncio.create_task(self._receiver(socket))
        try:
            done, pending = await asyncio.wait(
                [relay_task, receiver_task],
                return_when=asyncio.FIRST_EXCEPTION,
            )
            for task in pending:
                task.cancel()
            # Await each cancelled task so cancellation completes deterministically
            # and does not leak a pending coroutine into the event loop.
            for task in pending:
                with contextlib.suppress(
                    asyncio.CancelledError,
                    WebSocketDisconnect,
                    ConnectionError,
                    OSError,
                    _ClientDisconnected,
                    _ConnectionClosed,
                ):
                    await task
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
    async def _relay_events(
        socket: WebSocket[Any, Any, Any],
        state: State,
        channel: str,
        last_id: str | None = None,
    ) -> None:
        """Read the channel Redis Stream via ``XREAD BLOCK`` and forward events.

        Reads the per-channel stream so no in-process channel registry is required.
        Each delivered frame carries the stream entry ``id`` (via
        :func:`~superset.async_events.manager.parse_event`), which the frontend
        persists as ``last_async_event_id`` and replays on reconnection.

        The cursor starts at ``increment_id(last_id)`` (exclusive) when a
        ``last_id`` is supplied — delivering any missed backlog first — and at
        ``"$"`` (only-new) otherwise.  After each delivered entry the cursor
        advances to that entry's id.
        """
        # Read from the SAME client the GAQ writers use: Celery tasks publish
        # via ``AsyncEventManager`` on ``state.event_redis`` (built from
        # GLOBAL_ASYNC_QUERIES_CACHE_BACKEND), which can point at a different
        # host/db than the general ``REDIS_URL`` client — reading
        # ``state.redis`` here would XREAD a stream nobody writes to.
        redis = getattr(state, "event_redis", None) or state.redis
        settings = state.settings

        # If no channel could be resolved (rare: session-cookie fallback with
        # no Redis channel entry), there is no stream to read.  Reading the
        # bare ``{prefix}`` key would be wrong, so block harmlessly until the
        # client disconnects (the receiver task tears the connection down).
        if not channel:
            logger.debug(
                "No channel resolved for WebSocket; relay idle (no stream to read)"
            )
            await asyncio.Event().wait()
            return

        stream_prefix: str = getattr(
            settings, "global_async_queries_redis_stream_prefix", "async-events-"
        )
        stream_key = f"{stream_prefix}{channel}"
        block_ms: int = getattr(
            settings,
            "global_async_queries_redis_stream_read_block_ms",
            DEFAULT_STREAM_READ_BLOCK_MS,
        )
        count: int = getattr(
            settings,
            "global_async_queries_redis_stream_read_count",
            DEFAULT_STREAM_READ_COUNT,
        )

        # increment_id makes the catch-up read exclusive of last_id, matching
        # the original fetchRangeFromStream(startId=incrementId(lastId)).
        cursor = increment_id(last_id) if last_id else ONLY_NEW_CURSOR

        while True:
            try:
                # redis.asyncio (decode_responses=True) returns a list shaped
                # [[stream_key, [(entry_id, {field: value}), ...]], ...], or an
                # empty list on block timeout.
                # NOTE: each WebSocket holds one pooled state.redis connection
                # for the duration of the BLOCK call (same as the prior pub/sub
                # design; flagged for future connection-pool scale work).
                response = await redis.xread(
                    {stream_key: cursor},
                    count=count,
                    block=block_ms,
                )
            except (
                WebSocketDisconnect,
                ConnectionError,
                OSError,
                _ClientDisconnected,
                _ConnectionClosed,
            ):
                # Genuine disconnect (e.g. send-side failure surfaced through
                # the same client) — propagate so the handler tears down.
                raise
            except Exception:  # noqa: BLE001
                # Transient Redis error — back off briefly and retry without
                # killing the handler (mirrors the Node loop's catch+continue).
                logger.debug(
                    "Redis XREAD failed for stream %s, retrying...",
                    stream_key,
                    exc_info=True,
                )
                await asyncio.sleep(REDIS_RECONNECT_DELAY_SECONDS)
                continue

            if not response:
                continue  # block timeout, no new entries

            for _stream_key, entries in response:
                for entry_id, fields in entries:
                    try:
                        event = parse_event((entry_id, fields))
                    except (KeyError, json.JSONDecodeError, ValueError):
                        # Malformed stream entry (missing "data" field or bad
                        # JSON) — mirrors processStreamResults in the original
                        # Node sidecar (index.ts:271-278) which try/catch-es
                        # each item and continues.  Advance the cursor past the
                        # bad entry so it is not re-delivered, but keep the
                        # connection alive.
                        logger.warning(
                            "Skipping malformed stream entry %s on %s",
                            entry_id,
                            stream_key,
                            exc_info=True,
                        )
                        cursor = entry_id
                        continue
                    # A send failure (client gone) propagates intentionally so
                    # the handler tears down — only the parse is guarded.  Emit
                    # the original sidecar's ws_client_send_error metric first
                    # (statsd.increment('ws_client_send_error')), then re-raise.
                    try:
                        await socket.send_json(event)
                    except Exception:
                        stats_logger_manager.incr("ws_client_send_error")
                        raise
                    cursor = entry_id  # advance past last delivered id

    @staticmethod
    async def _receiver(socket: WebSocket[Any, Any, Any]) -> None:
        """Loop on ``receive()`` solely to detect client disconnect.

        The frontend never sends application data over this socket, so any
        inbound frame is discarded.

        Litestar's ``receive()`` RETURNS the first ``websocket.disconnect``
        message rather than raising immediately; only the *subsequent* call
        raises :class:`WebSocketDisconnect`.  This method inspects each
        received message so a disconnect is detected on the first call,
        matching real Litestar/ASGI semantics.
        """
        while True:
            msg = await socket.receive()
            if msg.get("type") == "websocket.disconnect":
                raise WebSocketDisconnect(detail="client disconnected")
