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
The original Apache Superset relied on Flask's thread-local
``flask.request`` proxy.  Audit logging, jinja templating, and a handful
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
templating can read the same dict the original Flask code reached for
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
# and we never used to ship them through to ``g.form_data`` in Flask.
_FORM_BODY_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH"})

# Cap on how much of an unexpectedly-large body we pull into memory for
# the form_data ContextVar.  Beyond this we silently drop the form_data
# binding rather than risk a 100 MB JSON upload turning into 100 MB of
# RAM in the audit-log context.  Mirrors the original Flask behaviour
# where ``MAX_CONTENT_LENGTH`` capped what FAB/Flask would parse out of
# the request.
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
      :data:`_MAX_PARSED_BODY_BYTES` limit.  This mirrors the original
      ``g.form_data`` populated by the Flask ``set_form_data`` helper.
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
            # Lifespan / websocket scopes — pass through.  We don't
            # populate the request ContextVar for non-HTTP scopes
            # because the audit-logging / jinja-templating code that
            # reads it only ever runs on HTTP request paths.
            await next_app(scope, receive, send)
            return

        method = (scope.get("method") or "GET").upper()

        # ----------------------------------------------------------
        # Fast paths that DON'T parse the body.
        # ----------------------------------------------------------
        # Methods that historically never carried a form body in Flask
        # (GET / HEAD / DELETE / OPTIONS) — bind the request ContextVar
        # but skip everything body-related.  ``form_data`` stays at its
        # default (empty dict) for these requests.
        if method not in _FORM_BODY_METHODS:
            await self._dispatch_no_body(scope, receive, send, next_app)
            return

        # Multipart / oversized bodies short-circuit body buffering to
        # avoid pulling potentially-huge file uploads into RAM.  The
        # route handler still receives the unmodified ``receive`` so
        # streaming readers (``request.stream()``, ``request.form()``)
        # work normally.  ``form_data`` is left empty.
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

        # ----------------------------------------------------------
        # Body-parsing path: drain → seed cache → parse → dispatch.
        # ----------------------------------------------------------
        body_bytes, wrapped_receive = await self._drain_body(receive)

        if body_bytes is not None:
            # Seed Litestar's connection-state body cache so every
            # Request derived from ``scope`` shares one body buffer.
            self._seed_scope_body(scope, body_bytes)

        await self._dispatch_with_body(
            scope=scope,
            receive=wrapped_receive,
            send=send,
            next_app=next_app,
            body_bytes=body_bytes,
            content_type=content_type.decode("latin-1", errors="replace"),
        )

    # ------------------------------------------------------------------
    # dispatch helpers
    # ------------------------------------------------------------------

    async def _dispatch_no_body(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        next_app: ASGIApp,
    ) -> None:
        """Bind the Request ContextVar without touching the body.

        Used for methods that don't carry a form body (GET/HEAD/...)
        and for the multipart / oversized short-circuits.  ``form_data``
        is left at its default (empty dict).
        """
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
        """Build the Request, populate ContextVars, dispatch downstream.

        ``body_bytes`` is ``None`` when the body could not be safely
        materialised (incomplete stream, oversized post-drain, etc.).
        In that case we still bind the Request ContextVar but leave
        ``form_data`` empty — exactly the behaviour the original Flask
        code took when ``request.form == {}``.
        """
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
                # Prime the Request's per-instance body cache so
                # ``await request.body()`` returns immediately without
                # touching the receive at all.
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

    # ------------------------------------------------------------------
    # body parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _headers_dict(scope: Scope) -> dict[bytes, bytes]:
        """Return a lower-cased ``bytes -> bytes`` view of scope headers.

        ASGI delivers headers as a list of ``(name, value)`` pairs with
        lowercase names; we materialise once per request and reuse the
        dict for content-type / content-length lookups so the
        per-request cost stays at one ``dict()`` call.
        """
        return dict(scope.get("headers", []))

    @staticmethod
    def _seed_scope_body(scope: Scope, body: bytes) -> None:
        """Pre-populate Litestar's connection-state body cache.

        Litestar lazily reads the request body on the *first*
        ``await request.body()`` call and stores the bytes on
        :class:`~litestar.utils.scope.state.ScopeState` (keyed under
        ``scope["state"]["_ls_connection_state"]``) so subsequent reads
        through the same scope never touch the ASGI receive again.
        We seed this cache from the buffer we already drained in
        :meth:`_drain_body` for two reasons:

        1. The ContextVar Request we expose for audit-logging code paths
           may be read AFTER the route handler has fully consumed (and
           replayed) the receive — without the seed those late reads
           would observe an exhausted stream.
        2. Litestar internally builds *its own* Request inside route
           dispatch; without the seed it would re-stream the replayed
           receive.  Seeding makes the body read O(1) regardless of how
           many Request instances are spawned over the same scope.

        Defensive: if Litestar changes its internal scope-state shape
        in a future release, the import-fail branch logs at debug and
        otherwise leaves the request to fall back to its normal
        receive-stream path.  No exception ever escapes.
        """
        try:
            # Public-by-convention internal helper — exposed at
            # ``litestar.utils.scope.state.ScopeState.from_scope``.
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
        """Drain ``http.request`` chunks from ``receive`` into a buffer.

        Returns ``(body, replay_receive)`` where ``body`` is one of:

        * the consolidated body when the stream completed cleanly and
          stayed within :data:`_MAX_PARSED_BODY_BYTES`;
        * ``None`` when the body exceeded the cap mid-stream — we keep
          draining so the server doesn't hang on stuck producers, but
          discard the chunks we've already buffered to free the RAM,
          and the replay falls through to the original ``receive`` so
          downstream handlers still see the rest of the upload;
        * ``None`` when the client disconnected before the body
          finished — partial bodies are unsafe to feed to JSON parsing,
          so we leave ``form_data`` empty.

        ``replay_receive`` is an ASGI-compatible callable that yields
        whatever the middleware did capture back to downstream
        middleware / the route handler so request semantics aren't
        broken.  See :class:`_ReplayReceive` for the one-shot replay
        contract.
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
                    # Too large to PARSE for request context, but the handler
                    # must still receive the FULL body.  Stop draining here,
                    # keep what we've buffered, and let the replay emit those
                    # bytes and then delegate the rest of the stream straight
                    # to the original ``receive`` (so large non-multipart
                    # uploads aren't truncated to an empty body).
                    oversized = True
                    oversized_more_body = bool(event.get("more_body"))
                    break
                if not event.get("more_body"):
                    completed = True
                    break
                continue
            if event_type == "http.disconnect":
                # Client cut the connection mid-body.  We pass the
                # disconnect event through to the handler (which will
                # bail with a normal ASGI disconnection) but signal
                # "no parseable body" via ``None``.
                trailing_event = cast("dict[str, Any]", event)
                break
            # Unknown event ordering — treat as end-of-body, forward
            # the trailing event downstream, but don't try to parse.
            trailing_event = cast("dict[str, Any]", event)
            break

        body: bytes | None
        if not completed or oversized:
            body = None
        else:
            body = b"".join(chunks)

        # Replay payload: the bytes we captured.  In the oversized case we
        # keep the buffered prefix and flag ``more_body`` so the replay
        # streams it, then delegates the remaining real chunks to the
        # original ``receive`` — the handler still sees the complete upload.
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
        """Parse ``body_bytes`` into a flat dict according to
        ``content_type``.  Returns ``None`` for content-types we
        deliberately don't parse (e.g. multipart uploads).
        """
        if not body_bytes:
            return {}

        ct = content_type.lower()

        # ---- application/json ----
        if "json" in ct:
            try:
                decoded = _json.loads(body_bytes.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                return {}
            if isinstance(decoded, dict):
                return decoded
            # JSON arrays / scalars don't map to form_data — Flask never
            # accepted those either.
            return {}

        # ---- application/x-www-form-urlencoded ----
        if "application/x-www-form-urlencoded" in ct:
            try:
                parsed = parse_qs(
                    body_bytes.decode("utf-8", errors="replace"),
                    keep_blank_values=True,
                )
            except Exception:  # noqa: BLE001
                return {}
            # parse_qs returns ``list[str]`` per key; flatten single-value
            # keys to match Flask's ``request.form.to_dict()`` semantics.
            return {k: (v[0] if len(v) == 1 else v) for k, v in parsed.items()}

        # ---- multipart/form-data ----
        # Should never reach here — multipart short-circuits earlier in
        # :meth:`handle` — but keep the explicit ``None`` so the
        # contract stays clear: any caller that *does* push a multipart
        # body through this method ends up with no form_data binding.
        return None

    # ------------------------------------------------------------------
    # form_data ContextVar plumbing
    # ------------------------------------------------------------------

    @staticmethod
    def _set_form_data_ctx(form_data: dict[str, Any]) -> Any:
        """Set the form_data ContextVar and return a reset token."""
        # ``set_form_data`` returns ``None`` because it's the public
        # contract (controllers call it without caring about the token);
        # we set the ContextVar directly here to capture the token.
        from superset.jinja_context import _form_data_ctx

        return _form_data_ctx.set(form_data)

    @staticmethod
    def _reset_form_data_ctx(token: Any) -> None:
        from superset.jinja_context import _form_data_ctx

        try:
            _form_data_ctx.reset(token)
        except (LookupError, ValueError):
            _form_data_ctx.set(None)


# ---------------------------------------------------------------------------
# _ReplayReceive — re-emits a previously-drained body to downstream ASGI.
# ---------------------------------------------------------------------------


class _ReplayReceive:
    """ASGI ``receive`` callable that replays a buffered body once.

    Usage contract — important for downstream consumers:

    * **First call** yields a single ``http.request`` event with the
      full buffered body and ``more_body=False``.  This means handlers
      that read via ``await request.body()`` (one read) get the
      consolidated body in one shot.
    * **Second call** yields the trailing event captured during drain
      (typically ``http.disconnect``) when one was recorded; otherwise
      it falls through to ``fallback_receive``.
    * **Third and subsequent calls** delegate to ``fallback_receive``
      so disconnect-after-handler-runs flows still work.

    Handlers that *stream* a request body in chunks via
    ``request.stream()`` will therefore see one chunk + EOF rather
    than the original wire-level chunking — buffer fidelity is not
    preserved, only payload fidelity.  None of the Liteset controllers
    actually need wire-level chunk fidelity (we read JSON / form
    bodies wholesale), and Litestar's own ``Request.body()`` is
    chunk-agnostic, so this is safe.

    When the middleware drained an oversized body and dropped the
    buffer, ``body`` is empty (``b""``).  The first call still emits
    the empty ``http.request`` event so downstream code that calls
    ``await receive()`` doesn't hang waiting for a body that already
    streamed past — but the *real* upload bytes are then re-streamed
    from ``fallback_receive`` on subsequent calls.
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
        # When ``more_body`` is set (oversized stream), the buffered prefix is
        # emitted with ``more_body=True`` and every subsequent event is pulled
        # straight from the original ``receive`` so the handler gets the rest
        # of the upload verbatim.
        self._more_body = more_body
        # 0 → emit body; 1 → emit trailing (if any); 2 → delegate.
        self._state = 0

    async def __call__(self) -> Any:
        if self._state == 0:
            if self._more_body:
                # Emit the buffered prefix, then delegate the remaining real
                # chunks to the original receive.
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
        # Delegate any further events back to the original receive.
        return await self._fallback()


__all__ = ["RequestContextMiddleware"]
