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
"""ASGI middleware that wraps inbound requests with ``pyinstrument``.

Direct port of ``superset_old/utils/profiler.py``.  The original was a
WSGI middleware; the Liteset port is an ASGI middleware that:

* mounts at the application level (``superset.app.on_startup``)
* triggers when the ``?_instrument=1`` query parameter is present
* runs the wrapped app under ``pyinstrument.Profiler`` and replaces the
  response body with the HTML profile output

Public API preserved:

* class name ``SupersetProfiler``
* constructor signature ``SupersetProfiler(app, interval=0.0001)``

If ``pyinstrument`` is not installed, the middleware is a no-op when the
flag is absent and raises a clear error when a profiling request comes
in (matches old behaviour of "pyinstrument is not installed").
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING
from urllib.parse import parse_qs

if TYPE_CHECKING:
    from pyinstrument import Profiler
else:
    # pyinstrument is an optional dependency; under TYPE_CHECKING the real class
    # provides precise types, at runtime we fall back to ``None``.
    try:
        from pyinstrument import Profiler
    except ModuleNotFoundError:  # pragma: no cover - optional dep
        Profiler = None


class SupersetProfiler:  # pylint: disable=too-few-public-methods
    """ASGI middleware to instrument Superset.

    Set ``PROFILING=True`` in the config and append ``?_instrument=1`` to
    any page to render an HTML pyinstrument profile of the request.

    Originally a WSGI middleware — Liteset uses ASGI throughout, so we
    reimplement ``__call__`` against the ASGI protocol while keeping the
    constructor signature byte-for-byte compatible with the old class.
    """

    def __init__(
        self,
        app: Any,
        interval: float = 0.0001,
    ) -> None:
        self.app = app
        self.interval = interval

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        # Only HTTP requests are instrumentable; let websocket / lifespan
        # scopes pass straight through to the wrapped app.
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        # Inspect the query string for ``_instrument=1``.  ``scope["query_string"]``
        # is bytes in ASGI; decode and parse.
        raw_qs: bytes = scope.get("query_string", b"") or b""
        qs = parse_qs(raw_qs.decode("latin-1"), keep_blank_values=True)
        if qs.get("_instrument", [""])[0] != "1":
            await self.app(scope, receive, send)
            return

        if Profiler is None:
            raise Exception(  # pylint: disable=broad-exception-raised
                "The module pyinstrument is not installed."
            )

        profiler = Profiler(interval=self.interval)
        profiler.start()
        # Buffer the wrapped app's messages so we can drop its body in
        # favour of the profiler HTML.
        try:
            captured: list[dict[str, Any]] = []

            async def _capturing_send(message: dict[str, Any]) -> None:
                captured.append(message)

            await self.app(scope, receive, _capturing_send)
        finally:
            profiler.stop()

        html = profiler.output_html()
        body = html.encode("utf-8")
        # Send a fresh 200 OK with the profiler HTML.  Any captured
        # messages from the wrapped app are intentionally discarded —
        # this matches the original Flask middleware which discarded the
        # response entirely.
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"text/html; charset=utf-8"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body, "more_body": False})
