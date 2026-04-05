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
"""ASGI equivalent of Werkzeug's ProxyFix middleware.

Reads X-Forwarded-For, X-Forwarded-Proto, X-Forwarded-Host,
X-Forwarded-Port, and X-Forwarded-Prefix headers and updates
the ASGI scope accordingly.  Respects configurable proxy chain
depth (``x_for``, ``x_proto``, ``x_host``, ``x_port``,
``x_prefix``) matching Werkzeug ProxyFix semantics.
"""

from __future__ import annotations

import logging

from litestar.middleware.base import ASGIMiddleware
from litestar.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)


def _get_trusted_value(
    raw: bytes | None,
    num_proxies: int,
) -> str | None:
    """Extract a trusted value from a comma-separated forwarded header.

    Werkzeug ProxyFix semantics: the header contains a list of values
    appended by each proxy.  ``num_proxies`` controls how many proxy
    hops to trust, counting from the **right** of the list.  If
    ``num_proxies`` is 1, the rightmost value (i.e. the one set by
    the nearest trusted proxy) is used.  If the header has fewer
    values than ``num_proxies``, the leftmost value is returned.

    Returns ``None`` when *raw* is empty/absent or *num_proxies* < 1.
    """
    if not raw or num_proxies < 1:
        return None

    decoded = raw.decode("latin-1")
    parts = [p.strip() for p in decoded.split(",")]

    if not parts:
        return None

    # Index from the right: with N proxies the trusted value is at
    # position ``len(parts) - num_proxies``.  Clamp to 0 so that
    # when there are fewer values than proxies we fall back to the
    # leftmost (most-upstream) entry.
    idx = max(0, len(parts) - num_proxies)
    value = parts[idx].strip()
    return value if value else None


class ProxyFixMiddleware(ASGIMiddleware):
    """ASGI equivalent of Werkzeug's ProxyFix.

    Reads ``X-Forwarded-For``, ``X-Forwarded-Proto``,
    ``X-Forwarded-Host``, ``X-Forwarded-Port``, and
    ``X-Forwarded-Prefix`` headers and updates the ASGI scope
    accordingly.

    Each ``x_*`` parameter controls how many proxies set that
    particular header.  Set to 0 to disable processing for that
    header.  The default (1) trusts one proxy hop for each.

    Parameters
    ----------
    x_for:
        Number of proxies setting ``X-Forwarded-For`` to determine
        the real client IP.
    x_proto:
        Number of proxies setting ``X-Forwarded-Proto`` to determine
        the original scheme (http/https).
    x_host:
        Number of proxies setting ``X-Forwarded-Host`` to determine
        the original ``Host`` header.
    x_port:
        Number of proxies setting ``X-Forwarded-Port`` to determine
        the server port.
    x_prefix:
        Number of proxies setting ``X-Forwarded-Prefix`` to determine
        a path prefix prepended to ``root_path``.
    """

    def __init__(
        self,
        *,
        x_for: int = 1,
        x_proto: int = 1,
        x_host: int = 1,
        x_port: int = 1,
        x_prefix: int = 1,
    ) -> None:
        self.x_for = x_for
        self.x_proto = x_proto
        self.x_host = x_host
        self.x_port = x_port
        self.x_prefix = x_prefix

    async def handle(  # noqa: C901
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        next_app: ASGIApp,
    ) -> None:
        if scope["type"] not in ("http", "websocket"):
            await next_app(scope, receive, send)
            return

        # Build a fast lookup of header name -> raw value.
        # ASGI headers are a list of [name, value] byte pairs.
        headers: dict[bytes, bytes] = {}
        for name, value in scope.get("headers", []):
            # Use the first occurrence of each header (consistent
            # with Werkzeug behaviour).
            headers.setdefault(name, value)

        # --- X-Forwarded-For -> scope["client"] ---
        forwarded_for = _get_trusted_value(
            headers.get(b"x-forwarded-for"),
            self.x_for,
        )
        if forwarded_for is not None:
            # Client is (host, port).  We only know the IP from the
            # header; preserve the original port if present.
            original_port = 0
            if scope.get("client"):
                original_port = scope["client"][1]
            scope["client"] = (forwarded_for, original_port)

        # --- X-Forwarded-Proto -> scope["scheme"] ---
        forwarded_proto = _get_trusted_value(
            headers.get(b"x-forwarded-proto"),
            self.x_proto,
        )
        if forwarded_proto is not None:
            scope["scheme"] = forwarded_proto.lower()

        # --- X-Forwarded-Host -> Host header ---
        forwarded_host = _get_trusted_value(
            headers.get(b"x-forwarded-host"),
            self.x_host,
        )
        if forwarded_host is not None:
            # Replace the Host header in the scope so that downstream
            # code (e.g. url_for, request.host) sees the original host.
            new_headers: list[tuple[bytes, bytes]] = []
            host_replaced = False
            for h_name, h_value in scope.get("headers", []):
                if h_name == b"host":
                    new_headers.append((b"host", forwarded_host.encode("latin-1")))
                    host_replaced = True
                else:
                    new_headers.append((h_name, h_value))
            if not host_replaced:
                new_headers.append((b"host", forwarded_host.encode("latin-1")))
            scope["headers"] = new_headers

            # Also update scope["server"] host portion if present.
            if scope.get("server"):
                _, port = scope["server"]
                scope["server"] = (forwarded_host.split(":")[0], port)

        # --- X-Forwarded-Port -> scope["server"] port ---
        forwarded_port = _get_trusted_value(
            headers.get(b"x-forwarded-port"),
            self.x_port,
        )
        if forwarded_port is not None:
            try:
                port_int = int(forwarded_port)
                if scope.get("server"):
                    host, _ = scope["server"]
                    scope["server"] = (host, port_int)
                else:
                    scope["server"] = ("", port_int)
            except (ValueError, TypeError):
                logger.debug(
                    "Ignoring invalid X-Forwarded-Port value: %s",
                    forwarded_port,
                )

        # --- X-Forwarded-Prefix -> scope["root_path"] ---
        forwarded_prefix = _get_trusted_value(
            headers.get(b"x-forwarded-prefix"),
            self.x_prefix,
        )
        if forwarded_prefix is not None:
            # Strip trailing slash to avoid double-slash when
            # concatenated with the path.
            prefix = forwarded_prefix.rstrip("/")
            if prefix:
                scope["root_path"] = prefix + scope.get("root_path", "")

        await next_app(scope, receive, send)
