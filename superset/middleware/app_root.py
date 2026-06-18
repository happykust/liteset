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
"""Strip the configured application-root prefix off incoming request paths.

When Superset is served under a URL prefix (``APPLICATION_ROOT`` /
``SUPERSET_APP_ROOT``), routes stay registered at the root and this middleware
removes the prefix from ``scope["path"]`` before routing — exactly what a
path-stripping reverse proxy does.  The prefix is *optional*: a request that
already lacks it (e.g. Cypress ``cy.request`` resolving a root-relative URL
against the origin) passes through untouched, while a prefixed request (e.g. a
browser navigation or asset load) is stripped.  ``root_path`` is set so the
ASGI scope still records the public mount point.
"""

from __future__ import annotations

from litestar.types import ASGIApp, Receive, Scope, Send


class AppRootMiddleware:
    """Remove ``app_root`` from the front of the request path when present."""

    def __init__(self, app: ASGIApp, *, app_root: str) -> None:
        self.app = app
        # Normalised to ``/prefix`` (leading slash, no trailing slash).
        self.app_root = "/" + app_root.strip("/")
        self._raw_root = self.app_root.encode("latin-1")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            path: str = scope.get("path", "")
            if path == self.app_root or path.startswith(self.app_root + "/"):
                # Mutate the request-scoped ASGI scope in place; routing reads
                # ``path`` downstream and ``root_path`` records the public mount.
                scope["path"] = path[len(self.app_root) :] or "/"
                scope["root_path"] = self.app_root
                raw_path = scope.get("raw_path")
                if isinstance(raw_path, bytes) and raw_path.startswith(self._raw_root):
                    scope["raw_path"] = raw_path[len(self._raw_root) :] or b"/"
        await self.app(scope, receive, send)
