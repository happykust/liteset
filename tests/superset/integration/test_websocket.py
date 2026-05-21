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
    # Explicitly set to None so _resolve_secret_key falls through to secret_key.
    # MagicMock auto-creates attributes as MagicMock (truthy), which would cause
    # _resolve_secret_key to use a garbage value and fail authentication.
    settings.global_async_queries_jwt_secret = None
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


def _get_ws_close_code(exc: WebSocketException) -> int | None:
    """Extract the WebSocket close code from a WebSocketException/Disconnect.

    Litestar's AsyncTestClient raises ``WebSocketDisconnect`` (a subclass of
    ``WebSocketException``) whose ``code`` attribute carries the integer close
    code sent by the server (e.g. 4401, 4403, 4429).  Falls back to inspecting
    ``detail`` for string-encoded codes when ``code`` is absent.
    Returns None when no recognisable close-code value is found.
    """
    # WebSocketDisconnect has a .code attribute (the WS close code).
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return code
    # Fallback: some versions encode it as a string in detail.
    detail = getattr(exc, "detail", None)
    if isinstance(detail, int):
        return detail
    if isinstance(detail, str) and detail.isdigit():
        return int(detail)
    return None


async def test_websocket_unauthorized_no_token():
    """Test WebSocket rejection when no token is provided.

    The handler at /ws/events accepts, then closes with 4401 (Unauthorized).
    This verifies the real handler is reached — a 404 would raise a different
    exception type (not WebSocketException with a 4401 close code).
    """
    app, _ = _make_app()
    async with AsyncTestClient(app) as client:
        with pytest.raises(WebSocketException) as exc_info:
            await _connect_and_receive(client, "/ws/events")
        # A 404/route-miss surfaces a different close code (e.g. 4500), so
        # asserting the exact 4401 (WS_CLOSE_UNAUTHORIZED) proves the real
        # handler ran and rejected the connection.
        close_code = _get_ws_close_code(exc_info.value)
        assert close_code == 4401, (
            f"Expected close code 4401 (Unauthorized), got {close_code}"
        )


async def test_websocket_unauthorized_invalid_token():
    """Test WebSocket rejection with invalid JWT.

    The handler at /ws/events accepts, then closes with 4401 (Unauthorized).
    If the route were wrong (404), no WebSocketException from the handler would
    be raised — this test would not pass for the wrong reason.
    """
    app, _ = _make_app()
    async with AsyncTestClient(app) as client:
        with pytest.raises(WebSocketException) as exc_info:
            await _connect_and_receive(client, "/ws/events?token=invalid")
        close_code = _get_ws_close_code(exc_info.value)
        assert close_code == 4401, (
            f"Expected close code 4401 (Unauthorized), got {close_code}"
        )


async def test_websocket_forbidden_origin():
    """Test WebSocket rejection when origin is not allowed.

    The handler at /ws/events accepts, then closes with 4403 (Forbidden).
    A 404 would surface a different exception, not a 4403-bearing WebSocketException.
    """
    app, _ = _make_app(cors_allow_origins=["https://allowed.com"])
    token = _create_token()
    async with AsyncTestClient(app) as client:
        with pytest.raises(WebSocketException) as exc_info:
            await _connect_and_receive(
                client,
                f"/ws/events?token={token}",
                headers={"origin": "https://evil.com"},
            )
        close_code = _get_ws_close_code(exc_info.value)
        assert close_code == 4403, (
            f"Expected close code 4403 (Forbidden origin), got {close_code}"
        )


async def test_websocket_per_user_limit():
    """Test per-user connection limit enforcement.

    The handler at /ws/events accepts, then closes with 4429 (Too Many).
    A 404 would not exercise the active_websockets counter — this test
    pre-seeds active_ws and asserts the limit path in the real handler.
    """
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
        with pytest.raises(WebSocketException) as exc_info:
            await _connect_and_receive(client, f"/ws/events?token={token}")
        close_code = _get_ws_close_code(exc_info.value)
        assert close_code == 4429, (
            f"Expected close code 4429 (Too Many Connections), got {close_code}"
        )
        # Prove the limit check ran: active_ws still contains the pre-seeded entry
        # (the handler does NOT add the rejected socket to active_ws).
        assert fake_socket in active_ws, (
            "Pre-seeded socket should still be in active_ws after rejection"
        )
        assert active_ws[fake_socket] == 42


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


async def test_relay_increments_send_error_metric():
    """A send failure emits ws_client_send_error (1:1 with the sidecar) and
    still propagates so the handler tears the connection down."""
    import json
    from unittest.mock import patch

    event = {"channel_id": "ch-1", "job_id": "j", "status": "done"}
    entries = [("1-0", {"data": json.dumps(event)})]
    redis = AsyncMock()
    redis.xread = AsyncMock(return_value=_stream_response("ch-1", entries))
    socket = MagicMock()
    socket.send_json = AsyncMock(side_effect=ConnectionError("client gone"))

    with patch("superset.websocket.events.stats_logger_manager") as stats:
        with pytest.raises(ConnectionError):
            await AsyncQueryWebSocket._relay_events(
                socket, _relay_state(redis), "ch-1", None
            )
        stats.incr.assert_any_call("ws_client_send_error")


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
    """_receiver raises when receive() RETURNS a disconnect message.

    Litestar's receive() returns the first websocket.disconnect dict rather
    than raising immediately — only the subsequent call raises.  This test
    exercises the real semantics: the coroutine returns a disconnect dict and
    _receiver must treat that as a disconnect without needing a second call.
    """
    socket = MagicMock()
    # Simulate Litestar's real behaviour: first call returns a disconnect dict,
    # second call would raise — but _receiver should raise on the first.
    socket.receive = AsyncMock(return_value={"type": "websocket.disconnect"})

    with pytest.raises(WebSocketDisconnect):
        await AsyncQueryWebSocket._receiver(socket)

    # Only one receive() call should have been made (detected on first message).
    socket.receive.assert_called_once()


async def test_relay_skips_malformed_entry_keeps_connection():
    """A malformed stream entry (missing 'data' key) is skipped, not fatal.

    Mirrors the Node sidecar's processStreamResults per-entry try/catch
    (index.ts:271-278): a bad entry advances the cursor and the loop
    continues — the connection is NOT dropped.
    """
    import json

    good_entry = (
        "200-0",
        {"data": json.dumps({"channel_id": "ch-1", "status": "done"})},
    )
    bad_entry = ("100-0", {})  # missing "data" field -> KeyError in parse_event

    redis = AsyncMock()
    # First response: bad entry then good entry; second call disconnects.
    redis.xread = AsyncMock(
        side_effect=[
            _stream_response("ch-1", [bad_entry, good_entry]),
            _disconnect(),
        ]
    )
    socket = MagicMock()
    socket.send_json = AsyncMock()

    with pytest.raises(WebSocketDisconnect):
        await AsyncQueryWebSocket._relay_events(
            socket, _relay_state(redis), "ch-1", None
        )

    # Only the good entry should have been sent; bad entry is silently skipped.
    assert socket.send_json.call_count == 1
    sent = socket.send_json.call_args_list[0].args[0]
    assert sent["id"] == "200-0"
    assert sent["status"] == "done"


# ---------------------------------------------------------------------------
# Teardown test — socket removed from active_websockets on disconnect
# ---------------------------------------------------------------------------


async def test_teardown_removes_socket_from_active_websockets():
    """When _receiver detects a disconnect, the relay is cancelled AND the
    socket is removed from active_websockets.

    Drives on_event directly as an unbound coroutine (bypassing Litestar's
    Controller.__init__ which requires an ``owner``) with:
    - a fake socket whose receive() returns a websocket.disconnect dict
    - an idle channel so the relay idles (xread returns [])
    - active_websockets pre-seeded with the fake socket
    Then asserts the socket is gone from active_websockets after the handler
    returns.
    """
    from litestar.datastructures import State

    token = _create_token(user_id=99, channel="teardown-ch")

    # Mock redis: xread always returns [] (simulating a blocking idle stream).
    mock_redis_inst = AsyncMock()
    mock_redis_inst.xread = AsyncMock(return_value=[])
    mock_redis_inst.get = AsyncMock(return_value=None)

    settings = _create_settings()
    settings.secret_key = JWT_SECRET

    active_ws: dict[object, int] = {}

    fake_socket = MagicMock()
    fake_socket.headers = {"origin": ""}
    fake_socket.query_params = {"token": token}
    fake_socket.cookies = {}
    fake_socket.accept = AsyncMock()
    fake_socket.close = AsyncMock()
    fake_socket.send_json = AsyncMock()
    # receive() returns a disconnect dict — exercises the real Litestar semantic
    # where the first call RETURNS the disconnect message rather than raising.
    fake_socket.receive = AsyncMock(return_value={"type": "websocket.disconnect"})

    state = State(
        {
            "settings": settings,
            "redis": mock_redis_inst,
            "active_websockets": active_ws,
        }
    )

    # AsyncQueryWebSocket.on_event is a Litestar WebsocketRouteHandler, not a
    # plain coroutine function — its __call__ goes through Litestar's ASGI
    # machinery.  Access the underlying coroutine via .fn and call it with a
    # fake self to bypass Controller.__init__(owner=...).
    #
    # Use spec=AsyncQueryWebSocket so that attribute lookups on fake_self
    # (e.g. fake_self._relay_events, fake_self._receiver) resolve against
    # the real class — _relay_events and _receiver are @staticmethod, so
    # Python's descriptor protocol returns the real coroutine functions when
    # accessed via a spec'd instance, not an auto-created child MagicMock.
    from unittest.mock import patch

    fake_self = MagicMock(spec=AsyncQueryWebSocket)
    with patch("superset.websocket.events.stats_logger_manager") as stats:
        await AsyncQueryWebSocket.on_event.fn(
            fake_self, socket=fake_socket, state=state
        )

    # The socket must be removed from active_websockets after teardown.
    assert fake_socket not in active_ws, (
        "Socket was not removed from active_websockets after disconnect teardown"
    )
    # The accepted connection emitted ws_connected_client (1:1 with the sidecar).
    stats.incr.assert_any_call("ws_connected_client")


# ---------------------------------------------------------------------------
# Empty-channel cancellability test (Minor 5)
# ---------------------------------------------------------------------------


async def test_idle_relay_is_cancellable():
    """The empty-channel relay (idles on asyncio.Event().wait()) can be
    cancelled cleanly — it does not hang on task.cancel().

    This locks in that the idle path tears down on disconnect.
    """
    redis = AsyncMock()
    redis.xread = AsyncMock()
    socket = MagicMock()
    socket.send_json = AsyncMock()

    task = asyncio.create_task(
        AsyncQueryWebSocket._relay_events(socket, _relay_state(redis), "", None)
    )
    # Let the task start and reach the Event().wait() suspension point.
    await asyncio.sleep(0)

    task.cancel()
    # The task should complete (raise CancelledError) promptly.
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(asyncio.shield(task), timeout=1.0)

    assert task.done(), "Idle relay task did not finish after cancel()"
    redis.xread.assert_not_called()


async def test_no_heartbeat_method():
    """The JSON heartbeat has been removed entirely."""
    assert not hasattr(AsyncQueryWebSocket, "_heartbeat")
    from superset.websocket import events

    assert not hasattr(events, "HEARTBEAT_INTERVAL_SECONDS")
