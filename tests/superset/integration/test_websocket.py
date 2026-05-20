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
"""Integration tests for WebSocket event streaming.

Tests cover:
- Unauthorized / forbidden origin rejection
- Per-user connection limit enforcement
- WebSocket auth module (unit-level, tested via controller path)
- The XREAD-based relay loop (id-bearing delivery, exclusive last_id cursor)
- Disconnect detection via the receiver loop

The full "connect -> receive event" round-trip is hard to drive through
Litestar's synchronous WebSocketTestSession because the handler runs infinite
relay/receiver coroutines, so the relay and disconnect behaviour is exercised
by calling the controller coroutines directly with fakes.  Auth rejection
tests work end-to-end because the handler closes the socket before entering
the relay loop.

Redis is mocked to avoid external dependency in CI.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import jwt as pyjwt
import pytest
from litestar import Litestar
from litestar.datastructures import State
from litestar.exceptions import WebSocketDisconnect, WebSocketException
from litestar.testing import AsyncTestClient

from superset.websocket.events import AsyncQueryWebSocket

JWT_SECRET = "test-secret-key-that-is-32-bytes!"


def _create_token(user_id: int = 42, channel: str = "ch-1") -> str:
    return pyjwt.encode(
        {"channel": channel, "sub": str(user_id)}, JWT_SECRET, algorithm="HS256"
    )


def _create_settings(**overrides: object) -> MagicMock:
    settings = MagicMock()
    settings.secret_key = JWT_SECRET
    settings.cors_allow_origins = overrides.get("cors_allow_origins", [])
    settings.max_ws_per_user = overrides.get("max_ws_per_user", 10)
    return settings


@pytest.fixture
def mock_redis() -> AsyncMock:
    """Mock redis.asyncio client whose xread yields no entries by default."""
    redis = AsyncMock()
    redis.xread = AsyncMock(return_value=[])
    return redis


def _make_app(**settings_overrides: object) -> tuple[Litestar, dict[object, int]]:
    """Create a Litestar app with AsyncQueryWebSocket and given settings."""
    settings = _create_settings(**settings_overrides)
    active_ws: dict[object, int] = settings_overrides.get("_active_ws", {})  # type: ignore[assignment]
    mock_redis = AsyncMock()
    app = Litestar(
        route_handlers=[AsyncQueryWebSocket],
        state=State(
            {
                "settings": settings,
                "redis": mock_redis,
                "active_websockets": active_ws,
            }
        ),
    )
    return app, active_ws


async def _connect_and_receive(
    client: AsyncTestClient, url: str, headers: dict[str, str] | None = None
) -> None:
    """Open a WebSocket and try to read one frame; raises on rejection."""
    ws = await client.websocket_connect(url, headers=headers or {})
    with ws:
        ws.receive_json()


async def test_websocket_unauthorized_no_token():
    """Test WebSocket rejection when no token is provided."""
    app, _ = _make_app()
    async with AsyncTestClient(app) as client:
        with pytest.raises(WebSocketException):
            await _connect_and_receive(client, "/ws/events")


async def test_websocket_unauthorized_invalid_token():
    """Test WebSocket rejection with invalid JWT."""
    app, _ = _make_app()
    async with AsyncTestClient(app) as client:
        with pytest.raises(WebSocketException):
            await _connect_and_receive(client, "/ws/events?token=invalid")


async def test_websocket_forbidden_origin():
    """Test WebSocket rejection when origin is not allowed."""
    app, _ = _make_app(cors_allow_origins=["https://allowed.com"])
    token = _create_token()
    async with AsyncTestClient(app) as client:
        with pytest.raises(WebSocketException):
            await _connect_and_receive(
                client,
                f"/ws/events?token={token}",
                headers={"origin": "https://evil.com"},
            )


async def test_websocket_per_user_limit():
    """Test per-user connection limit enforcement."""
    active_ws: dict[object, int] = {}
    # Pre-fill with one connection for user 42
    fake_socket = MagicMock()
    active_ws[fake_socket] = 42

    settings = _create_settings(max_ws_per_user=1)
    mock_redis = AsyncMock()
    app = Litestar(
        route_handlers=[AsyncQueryWebSocket],
        state=State(
            {
                "settings": settings,
                "redis": mock_redis,
                "active_websockets": active_ws,
            }
        ),
    )
    token = _create_token(user_id=42)
    async with AsyncTestClient(app) as client:
        with pytest.raises(WebSocketException):
            await _connect_and_receive(client, f"/ws/events?token={token}")


# ---------------------------------------------------------------------------
# Relay loop (_relay_events) — XREAD stream delivery
# ---------------------------------------------------------------------------


def _disconnect() -> WebSocketDisconnect:
    """Build a WebSocketDisconnect (the detail kwarg is required)."""
    return WebSocketDisconnect(detail="client disconnected")


def _relay_state(redis: AsyncMock) -> State:
    settings = MagicMock()
    settings.global_async_queries_redis_stream_prefix = "async-events-"
    settings.global_async_queries_redis_stream_read_block_ms = 5000
    settings.global_async_queries_redis_stream_read_count = 100
    return State({"settings": settings, "redis": redis})


def _stream_response(channel: str, entries: list[tuple[str, dict[str, str]]]):
    """Build the redis.asyncio xread reply shape: [[key, [(id, fields), ...]]]."""
    return [[f"async-events-{channel}", entries]]


async def test_relay_delivers_id_bearing_events():
    """Each delivered frame carries the stream entry id plus decoded fields."""
    import json

    entries = [
        (
            "1607477697866-0",
            {
                "data": json.dumps(
                    {"channel_id": "ch-1", "job_id": "j1", "status": "running"}
                )
            },
        ),
        (
            "1607477697867-0",
            {
                "data": json.dumps(
                    {"channel_id": "ch-1", "job_id": "j1", "status": "done"}
                )
            },
        ),
    ]
    redis = AsyncMock()
    # First xread returns the entries, second call raises to break the loop.
    redis.xread = AsyncMock(
        side_effect=[_stream_response("ch-1", entries), _disconnect()]
    )
    socket = MagicMock()
    socket.send_json = AsyncMock()

    with pytest.raises(WebSocketDisconnect):
        await AsyncQueryWebSocket._relay_events(
            socket, _relay_state(redis), "ch-1", None
        )

    sent = [call.args[0] for call in socket.send_json.call_args_list]
    assert len(sent) == 2
    assert sent[0]["id"] == "1607477697866-0"
    assert sent[0]["status"] == "running"
    assert sent[0]["channel_id"] == "ch-1"
    assert sent[1]["id"] == "1607477697867-0"
    assert sent[1]["status"] == "done"
    # No frame is a non-event control message (e.g. a heartbeat ping).
    assert all(frame.get("type") != "ping" for frame in sent)
    assert all("id" in frame for frame in sent)


async def test_relay_exclusive_last_id_cursor():
    """With last_id supplied, the first xread starts from increment_id(last_id)."""
    redis = AsyncMock()
    redis.xread = AsyncMock(side_effect=[[], _disconnect()])
    socket = MagicMock()
    socket.send_json = AsyncMock()

    with pytest.raises(WebSocketDisconnect):
        await AsyncQueryWebSocket._relay_events(
            socket, _relay_state(redis), "ch-1", "1607477697866-0"
        )

    first_call = redis.xread.call_args_list[0]
    streams = first_call.args[0]
    # Exclusive of last_id -> incremented seq.
    assert streams == {"async-events-ch-1": "1607477697866-1"}
    assert first_call.kwargs["block"] == 5000
    assert first_call.kwargs["count"] == 100


async def test_relay_only_new_cursor_without_last_id():
    """Without last_id, the read starts from the only-new sentinel '$'."""
    redis = AsyncMock()
    redis.xread = AsyncMock(side_effect=[_disconnect()])
    socket = MagicMock()
    socket.send_json = AsyncMock()

    with pytest.raises(WebSocketDisconnect):
        await AsyncQueryWebSocket._relay_events(
            socket, _relay_state(redis), "ch-1", None
        )

    assert redis.xread.call_args_list[0].args[0] == {"async-events-ch-1": "$"}


async def test_relay_advances_cursor_after_delivery():
    """After delivering entries, the next read resumes from the last id."""
    import json

    entries = [
        (
            "100-0",
            {"data": json.dumps({"channel_id": "ch-1", "status": "running"})},
        ),
    ]
    redis = AsyncMock()
    redis.xread = AsyncMock(
        side_effect=[_stream_response("ch-1", entries), _disconnect()]
    )
    socket = MagicMock()
    socket.send_json = AsyncMock()

    with pytest.raises(WebSocketDisconnect):
        await AsyncQueryWebSocket._relay_events(
            socket, _relay_state(redis), "ch-1", None
        )

    # First read: "$"; second read resumes from the last delivered id.
    assert redis.xread.call_args_list[0].args[0] == {"async-events-ch-1": "$"}
    assert redis.xread.call_args_list[1].args[0] == {"async-events-ch-1": "100-0"}


async def test_relay_retries_on_transient_redis_error():
    """A transient (non-disconnect) Redis error backs off and retries."""
    redis = AsyncMock()
    redis.xread = AsyncMock(side_effect=[RuntimeError("boom"), _disconnect()])
    socket = MagicMock()
    socket.send_json = AsyncMock()

    with pytest.raises(WebSocketDisconnect):
        await AsyncQueryWebSocket._relay_events(
            socket, _relay_state(redis), "ch-1", None
        )

    # The transient error did not kill the loop; it read again.
    assert redis.xread.call_count == 2
    socket.send_json.assert_not_called()


async def test_relay_idle_when_no_channel():
    """An empty channel has no stream; the relay idles without reading."""
    redis = AsyncMock()
    redis.xread = AsyncMock()
    socket = MagicMock()
    socket.send_json = AsyncMock()

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            AsyncQueryWebSocket._relay_events(socket, _relay_state(redis), "", None),
            timeout=0.1,
        )

    # Never read the bare-prefix key, never sent a frame.
    redis.xread.assert_not_called()
    socket.send_json.assert_not_called()


# ---------------------------------------------------------------------------
# Receiver loop (_receiver) — disconnect detection
# ---------------------------------------------------------------------------


async def test_receiver_propagates_disconnect():
    """_receiver raises when the client disconnects so the handler tears down."""
    socket = MagicMock()
    socket.receive = AsyncMock(side_effect=_disconnect())

    with pytest.raises(WebSocketDisconnect):
        await AsyncQueryWebSocket._receiver(socket)


async def test_no_heartbeat_method():
    """The JSON heartbeat has been removed entirely."""
    assert not hasattr(AsyncQueryWebSocket, "_heartbeat")
    from superset.websocket import events

    assert not hasattr(events, "HEARTBEAT_INTERVAL_SECONDS")
