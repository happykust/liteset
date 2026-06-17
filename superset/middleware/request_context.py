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
"""Request-context middleware.

Binds the active Litestar :class:`~litestar.connection.Request` to a
:class:`ContextVar` (``_current_request_ctx`` in
:mod:`superset.utils.core`) so any code path executed within the
request's task can resolve it via :func:`get_current_request` without
needing it threaded through every signature.

Why this exists
---------------
The original Apache Superset relied on a thread-local
``request`` proxy.  Audit logging, jinja templating, and a handful
of utility helpers consulted ``request.referrer`` / ``request.path`` /
``request.headers`` directly — the request was never passed in
explicitly.  In the async port we cannot reach for a thread-local; this
middleware reproduces the original implicit availability of the
request object using :class:`ContextVar`, which gives per-task isolation
under asyncio (concurrent requests served by the same event loop never
share state).

Body parsing rules
------------------
For ``POST`` / ``PUT`` / ``PATCH`` requests we *optionally* parse the
body into the ``form_data`` ContextVar so audit logging and jinja
templating can read the same dict the original code reached for
via ``g.form_data``.  Parsing is gated on three independent
short-circuits to avoid materialising large or streaming uploads:

1. **Multipart bodies** (``Content-Type: multipart/...``) are passed
   through verbatim.  Multipart bodies are typically file uploads;
   buffering them in middleware would force every chart-import / dataset
   upload through a 4 MiB-or-bigger memory copy.  The route handler
   can still call ``await request.form()`` itself.
2. **Oversized bodies** (``Content-Length`` greater than
   :data:`_MAX_PARSED_BODY_BYTES`) are passed through verbatim for the
   same reason.
3. **Disconnected / partial bodies** — if the client disconnects before
   sending ``more_body=False``, ``form_data`` is left empty rather
   than risk silently feeding a truncated JSON dict into audit logs.

The middleware never blocks oversized requests — only the
``form_data`` parse is skipped.  The route handler still receives the
full body on its own ``receive`` callable, just without the
middleware-side replay buffer.
"""

from __future__ import annotations

import json as _json
import logging
from typing import Any, cast
from urllib.parse import parse_qs

from litestar import Request
from litestar.middleware.base import ASGIMiddleware
from litestar.types import ASGIApp, Receive, Scope, Send

from superset.utils.core import (
    reset_current_request,
    set_current_request,
)

logger = logging.getLogger(__name__)

# Methods that can carry a body we want to surface as ``form_data`` for
# audit logging / jinja templating.  GET / DELETE bodies are non-standard
# and we never used to ship them through to ``g.form_data`` upstream.
_FORM_BODY_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH"})

# Cap on how much of an unexpectedly-large body we pull into memory for
# the form_data ContextVar.  Beyond this we silently drop the form_data
# binding rather than risk a 100 MB JSON upload turning into 100 MB of
# RAM in the audit-log context.
_MAX_PARSED_BODY_BYTES: int = 4 * 1024 * 1024  # 4 MiB


class RequestContextMiddleware(ASGIMiddleware):
    """Bind the active Request (and its parsed body) to ContextVars.

    Mounts as a regular ``ASGIMiddleware`` (matches the pattern used by
    :class:`~superset.middleware.locale.LocaleMiddleware`) so it
    integrates with Litestar's middleware stack without needing
    ``copy_scope`` workarounds.

    Two ContextVars are populated:

    * ``_current_request_ctx`` (in :mod:`superset.utils.core`) — the raw
      :class:`litestar.Request`, used by audit-logging code paths that
      need to read ``request.headers`` / ``request.url`` without the
      caller threading the request through the call chain.
    * ``_form_data_ctx`` (in :mod:`superset.jinja_context`) — a parsed
      copy of the request body for ``POST``/``PUT``/``PATCH`` requests
      whose body is JSON / form-urlencoded and within the
      :data:`_MAX_PARSED_BODY_BYTES` limit.
      Multipart, oversized, and partially-received bodies are passed
      through without parsing.

    The reset token is captured for both ContextVars and reset in a
    ``finally`` block so any task that re-uses the ASGI scope object
    (e.g. an after-response hook scheduled via ``asyncio.shield``)
    never observes a stale binding.
    """

    # Middleware is created once per ASGI app and reused across all
    # requests; ``__slots__`` keeps the per-instance footprint to the
    # single ``app`` reference Litestar's base sets up.
    __slots__ = ()

    async def handle(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        next_app: ASGIApp,
    ) -> None:
        if scope["type"] != "http":
            await next_app(scope, receive, send)
            return

        method = str(scope.get("method") or "GET").upper()

        if method not in _FORM_BODY_METHODS:
            await self._dispatch_no_body(scope, receive, send, next_app)
            return

        headers = self._headers_dict(scope)
        content_type = headers.get(b"content-type", b"").lower()
        is_multipart = content_type.startswith(b"multipart/")

        try:
            content_length = int(headers.get(b"content-length", b"") or -1)
        except (TypeError, ValueError):
            content_length = -1
        too_large = content_length > _MAX_PARSED_BODY_BYTES

        if is_multipart or too_large:
            await self._dispatch_no_body(scope, receive, send, next_app)
            return

        body_bytes, wrapped_receive = await self._drain_body(receive)

        if body_bytes is not None:
            self._seed_scope_body(scope, body_bytes)

        await self._dispatch_with_body(
            scope=scope,
            receive=wrapped_receive,
            send=send,
            next_app=next_app,
            body_bytes=body_bytes,
            content_type=content_type.decode("latin-1", errors="replace"),
        )

    async def _dispatch_no_body(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        next_app: ASGIApp,
    ) -> None:
        """Bind the Request ContextVar without parsing the body."""
        request_token = None
        try:
            request: Request[Any, Any, Any] | None = Request(
                scope, receive=receive, send=send
            )
        except Exception:  # noqa: BLE001 — never break the stack
            logger.debug(
                "RequestContextMiddleware failed to wrap scope into Request",
                exc_info=True,
            )
            request = None

        if request is not None:
            request_token = set_current_request(request)
        try:
            await next_app(scope, receive, send)
        finally:
            if request_token is not None:
                reset_current_request(request_token)

    async def _dispatch_with_body(
        self,
        *,
        scope: Scope,
        receive: Receive,
        send: Send,
        next_app: ASGIApp,
        body_bytes: bytes | None,
        content_type: str,
    ) -> None:
        request_token = None
        form_data_token = None
        try:
            request: Request[Any, Any, Any] | None = Request(
                scope, receive=receive, send=send
            )
        except Exception:  # noqa: BLE001 — never break the stack
            logger.debug(
                "RequestContextMiddleware failed to wrap scope into Request",
                exc_info=True,
            )
            request = None

        if request is not None:
            if body_bytes is not None:
                try:
                    request._body = body_bytes
                except (AttributeError, TypeError):
                    logger.debug(
                        "Could not prime Request._body cache",
                        exc_info=True,
                    )

            request_token = set_current_request(request)

            if body_bytes is not None:
                form_data = self._parse_body(body_bytes, content_type)
                if form_data is not None:
                    form_data_token = self._set_form_data_ctx(form_data)

        try:
            await next_app(scope, receive, send)
        finally:
            if request_token is not None:
                reset_current_request(request_token)
            if form_data_token is not None:
                self._reset_form_data_ctx(form_data_token)

    @staticmethod
    def _headers_dict(scope: Scope) -> dict[bytes, bytes]:
        return dict(scope.get("headers", []))

    @staticmethod
    def _seed_scope_body(scope: Scope, body: bytes) -> None:
        """Pre-populate Litestar's ScopeState body cache from the
        already-drained buffer.

        Without this, the audit-logging ContextVar Request may read an exhausted
        stream, and Litestar's internal route-dispatch Request would re-stream the
        replayed receive instead of reading O(1) from cache.
        """
        try:
            from litestar.utils.scope.state import ScopeState
        except ImportError:  # pragma: no cover — version drift
            logger.debug(
                "ScopeState helper not importable; skipping body seed",
                exc_info=True,
            )
            return
        try:
            state = ScopeState.from_scope(scope)
            state.body = body
        except Exception:  # noqa: BLE001 — never break the stack
            logger.debug(
                "Failed to seed ScopeState.body cache from middleware",
                exc_info=True,
            )

    @staticmethod
    async def _drain_body(
        receive: Receive,
    ) -> tuple[bytes | None, Receive]:
        """Drain http.request chunks into a buffer and return (body, replay_receive).

        body is None when the stream exceeded _MAX_PARSED_BODY_BYTES or the client
        disconnected mid-body (partial data is unsafe to parse). replay_receive
        re-emits the captured bytes to downstream handlers.
        """
        chunks: list[bytes] = []
        total = 0
        oversized = False
        completed = False
        trailing_event: dict[str, Any] | None = None

        oversized_more_body = False
        while True:
            event = await receive()
            event_type = event.get("type")
            if event_type == "http.request":
                chunk = cast(bytes, event.get("body") or b"")
                if chunk:
                    total += len(chunk)
                    chunks.append(chunk)
                if total > _MAX_PARSED_BODY_BYTES:
                    # Too large to parse, but the replay must still deliver
                    # the full body — emit the buffered prefix then delegate
                    # the rest to the original receive.
                    oversized = True
                    oversized_more_body = bool(event.get("more_body"))
                    break
                if not event.get("more_body"):
                    completed = True
                    break
                continue
            if event_type == "http.disconnect":
                trailing_event = cast("dict[str, Any]", event)
                break
            trailing_event = cast("dict[str, Any]", event)
            break

        body: bytes | None
        if not completed or oversized:
            body = None
        else:
            body = b"".join(chunks)

        replay_payload = b"".join(chunks)
        replay = _ReplayReceive(
            body=replay_payload,
            fallback_receive=receive,
            trailing_event=trailing_event,
            more_body=oversized_more_body,
        )
        return body, replay

    @staticmethod
    def _parse_body(body_bytes: bytes, content_type: str) -> dict[str, Any] | None:
        """Parse body into a flat dict for JSON and form-urlencoded
        content; None for multipart."""
        if not body_bytes:
            return {}

        ct = content_type.lower()

        if "json" in ct:
            try:
                decoded = _json.loads(body_bytes.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                return {}
            if isinstance(decoded, dict):
                return decoded
            return {}

        if "application/x-www-form-urlencoded" in ct:
            try:
                parsed = parse_qs(
                    body_bytes.decode("utf-8", errors="replace"),
                    keep_blank_values=True,
                )
            except Exception:  # noqa: BLE001
                return {}
            return {k: (v[0] if len(v) == 1 else v) for k, v in parsed.items()}

        return None

    @staticmethod
    def _set_form_data_ctx(form_data: dict[str, Any]) -> Any:
        # Set the ContextVar directly (not via the public set_form_data helper)
        # so we capture the reset token for cleanup in the finally block.
        from superset.jinja_context import _form_data_ctx

        return _form_data_ctx.set(form_data)

    @staticmethod
    def _reset_form_data_ctx(token: Any) -> None:
        from superset.jinja_context import _form_data_ctx

        try:
            _form_data_ctx.reset(token)
        except (LookupError, ValueError):
            _form_data_ctx.set(None)


class _ReplayReceive:
    """ASGI receive that replays a pre-drained body buffer.

    State machine: 0 → emit buffered body (more_body=False, or True when oversized);
    1 → emit trailing event; 2 → delegate to fallback_receive.

    Payload fidelity is preserved but not wire-level chunking — controllers
    read JSON/form bodies wholesale so chunk boundaries do not matter.
    For oversized bodies the buffer is empty and the real upload bytes come
    from fallback_receive on subsequent calls.
    """

    __slots__ = ("_body", "_trailing", "_fallback", "_state", "_more_body")

    def __init__(
        self,
        body: bytes,
        fallback_receive: Receive,
        trailing_event: dict[str, Any] | None = None,
        more_body: bool = False,
    ) -> None:
        self._body = body
        self._trailing = trailing_event
        self._fallback = fallback_receive
        self._more_body = more_body
        self._state = 0

    async def __call__(self) -> Any:
        if self._state == 0:
            if self._more_body:
                self._state = 2
                return {
                    "type": "http.request",
                    "body": self._body,
                    "more_body": True,
                }
            self._state = 1
            return {
                "type": "http.request",
                "body": self._body,
                "more_body": False,
            }
        if self._state == 1:
            self._state = 2
            if self._trailing is not None:
                return self._trailing
        return await self._fallback()


__all__ = ["RequestContextMiddleware"]
