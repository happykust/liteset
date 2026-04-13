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
"""Request/Response dump middleware for Litestar.

Records every HTTP request/response pair to a JSONL file.
Designed for comparing API behaviour between the original Flask
backend and the Litestar port.

Usage::

    from superset.middleware.request_dump import RequestDumpMiddleware

    app = Litestar(
        middleware=[
            RequestDumpMiddleware.configure(
                output_path="./superset_dump.jsonl",
                exclude_paths=("/static/", "/health"),
            )
        ],
    )
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import traceback
from dataclasses import dataclass
from typing import Any

from litestar.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

_MAX_BODY_SIZE = 10 * 1024 * 1024  # 10 MB
_DEFAULT_OUTPUT = "./superset_dump.jsonl"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class RequestDumpConfig:
    """Configuration for :class:`RequestDumpMiddleware`."""

    output_path: str = _DEFAULT_OUTPUT
    dump_request_body: bool = True
    dump_response_body: bool = True
    max_body_size: int = _MAX_BODY_SIZE
    exclude_paths: tuple[str, ...] = ("/static/",)


class _SharedState:
    """Shared mutable state across all middleware instances.

    Litestar creates one middleware instance per route handler, so we
    keep the file handle, sequence counter and lock in a singleton that
    is initialised once per ``configure()`` call.
    """

    def __init__(self, path: str) -> None:
        self.seq = 0
        self.lock = asyncio.Lock()
        self.file: io.TextIOWrapper | None = None
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            self.file = open(path, "w", encoding="utf-8")  # noqa: SIM115
            logger.warning("Request dump middleware active, writing to %s", path)
        except OSError:
            logger.exception("Failed to initialise dump file %s", path)

    def close(self) -> None:
        if self.file and not self.file.closed:
            self.file.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_body(
    data: bytes | str | None,
    max_size: int = _MAX_BODY_SIZE,
) -> str | None:
    """Safely decode and optionally truncate a body payload."""
    if data is None:
        return None
    if isinstance(data, bytes):
        if len(data) > max_size:
            return f"[truncated {len(data)} bytes]"
        try:
            data = data.decode("utf-8")
        except UnicodeDecodeError:
            return f"[binary {len(data)} bytes]"
    if len(data) > max_size:
        return f"[truncated {len(data)} chars]"
    return data


def _try_parse_json(text: str | None) -> Any:
    """Try to parse *text* as JSON so we don't double-encode."""
    if text is None:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return text


def _decode_headers(
    raw: list[tuple[bytes, bytes]] | list[Any],
) -> dict[str, str]:
    """Convert ASGI raw headers ``[(b"name", b"value")]`` to a dict."""
    result: dict[str, str] = {}
    for pair in raw:
        if isinstance(pair, (list, tuple)) and len(pair) == 2:
            raw_name, raw_value = pair[0], pair[1]
            name = (
                raw_name.decode("latin-1")
                if isinstance(raw_name, bytes)
                else str(raw_name)
            )
            value = (
                raw_value.decode("latin-1")
                if isinstance(raw_value, bytes)
                else str(raw_value)
            )
            result[name] = value
    return result


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class RequestDumpMiddleware:
    """ASGI middleware that writes request/response pairs to JSONL.

    Use :meth:`configure` to create a middleware factory with custom
    settings.
    """

    def __init__(
        self,
        app: ASGIApp,
        config: RequestDumpConfig | None = None,
        shared: _SharedState | None = None,
    ) -> None:
        self.app = app
        self.config = config or RequestDumpConfig()
        self._shared = shared or _SharedState(self.config.output_path)

    # -- Factory ---------------------------------------------------------------

    @classmethod
    def configure(
        cls,
        output_path: str = _DEFAULT_OUTPUT,
        dump_request_body: bool = True,
        dump_response_body: bool = True,
        max_body_size: int = _MAX_BODY_SIZE,
        exclude_paths: tuple[str, ...] = ("/static/",),
    ) -> Any:
        """Return a middleware factory for Litestar's middleware list."""
        config = RequestDumpConfig(
            output_path=output_path,
            dump_request_body=dump_request_body,
            dump_response_body=dump_response_body,
            max_body_size=max_body_size,
            exclude_paths=exclude_paths,
        )

        shared = _SharedState(config.output_path)

        def factory(app: ASGIApp) -> RequestDumpMiddleware:
            return cls(app, config, shared)

        return factory

    # -- ASGI entry point ------------------------------------------------------

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        if any(path.startswith(prefix) for prefix in self.config.exclude_paths):
            await self.app(scope, receive, send)
            return

        # 1. Capture request body
        request_body_bytes: bytes | None = None
        if self.config.dump_request_body:
            request_body_bytes, receive = await _capture_request(receive)

        # 2. Prepare response capture
        response_status = 0
        response_headers_raw: list[tuple[bytes, bytes]] = []
        response_body_chunks: list[bytes] = []
        error_info: dict[str, Any] | None = None

        async def send_wrapper(message: Message) -> None:
            nonlocal response_status, response_headers_raw
            if message["type"] == "http.response.start":
                response_status = message.get("status", 0)
                response_headers_raw = list(message.get("headers", []))
            elif message["type"] == "http.response.body":
                chunk = message.get("body", b"")
                if chunk:
                    response_body_chunks.append(chunk)
            await send(message)

        # 3. Call downstream, capture exceptions
        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:
            error_info = {
                "type": type(exc).__qualname__,
                "message": str(exc),
                "traceback": traceback.format_exception(
                    type(exc), exc, exc.__traceback__
                ),
            }
            raise
        finally:
            # 4. Write entry (even on error)
            try:
                await self._write_entry(
                    scope,
                    request_body_bytes,
                    response_status,
                    response_headers_raw,
                    response_body_chunks,
                    error_info,
                )
            except Exception:
                logger.exception("Failed to write request dump entry")

    # -- Internals -------------------------------------------------------------

    async def _write_entry(
        self,
        scope: Scope,
        request_body: bytes | None,
        response_status: int,
        response_headers_raw: list[tuple[bytes, bytes]],
        response_body_chunks: list[bytes],
        error_info: dict[str, Any] | None,
    ) -> None:
        max_size = self.config.max_body_size

        method = scope.get("method", "")
        path = scope.get("path", "")
        qs = (scope.get("query_string", b"") or b"").decode("latin-1")
        req_headers = _decode_headers(list(scope.get("headers", [])))

        req_body: str | Any | None = None
        if self.config.dump_request_body and request_body is not None:
            req_body = _try_parse_json(_safe_body(request_body, max_size))

        resp_headers = _decode_headers(response_headers_raw)
        resp_body: str | Any | None = None
        if self.config.dump_response_body and response_body_chunks:
            raw = b"".join(response_body_chunks)
            resp_body = _try_parse_json(_safe_body(raw, max_size))

        # Atomic seq + write under lock
        async with self._shared.lock:
            seq = self._shared.seq
            self._shared.seq += 1

            entry: dict[str, Any] = {
                "seq": seq,
                "request": {
                    "method": method,
                    "path": path,
                    "query_string": qs,
                    "headers": req_headers,
                    "body": req_body,
                },
                "response": {
                    "status_code": response_status,
                    "headers": resp_headers,
                    "body": resp_body,
                },
                "error": error_info,
            }

            line = json.dumps(entry, ensure_ascii=False, default=str)
            await asyncio.to_thread(self._append_line, line + "\n")

    def _append_line(self, line: str) -> None:
        """Synchronous file write (called via ``asyncio.to_thread``)."""
        try:
            f = self._shared.file
            if f and not f.closed:
                f.write(line)
                f.flush()
            else:
                with open(  # noqa: SIM115
                    self.config.output_path, "a", encoding="utf-8"
                ) as fh:
                    fh.write(line)
        except OSError:
            logger.exception(
                "Failed to append to dump file %s",
                self.config.output_path,
            )


# ---------------------------------------------------------------------------
# Request body capture (module-level for reuse)
# ---------------------------------------------------------------------------


async def _capture_request(
    receive: Receive,
) -> tuple[bytes, Receive]:
    """Read the full request body and return a replay-able receive."""
    body = bytearray()
    while True:
        message = await receive()
        chunk: bytes = message.get("body", b"") or b""  # type: ignore[assignment]
        if chunk:
            body.extend(chunk)
        if not message.get("more_body", False):
            break

    body_bytes = bytes(body)
    replayed = False

    async def replay_receive() -> Any:
        nonlocal replayed
        if not replayed:
            replayed = True
            return {
                "type": "http.request",
                "body": body_bytes,
                "more_body": False,
            }
        return {"type": "http.disconnect"}

    return body_bytes, replay_receive
