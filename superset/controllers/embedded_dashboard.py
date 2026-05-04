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
"""Embedded dashboard controllers.

Two controllers live here:

* ``EmbeddedDashboardController`` — JSON API at ``/api/v1/embedded_dashboard``
* ``EmbeddedSSRController`` — HTML SSR route at ``/embedded/{uuid}``
  (ported from ``superset_old/embedded/view.py:EmbeddedView``)
"""

from __future__ import annotations

import json
from urllib.parse import urlparse
from typing import Any

from litestar import Controller, get
from litestar.connection import Request
from litestar.datastructures import State
from litestar.di import Provide
from litestar.response import Response, Template

from superset.exceptions import SupersetNotFoundError
from superset.providers import provide_embedded_dao


def _same_origin(url1: str | None, url2: str | None) -> bool:
    """Return True when *url1* and *url2* share the same scheme + netloc.

    Mirrors the logic of ``flask_wtf.csrf.same_origin`` which is not
    available in the Litestar / ASGI context.  If either argument is
    empty/None the function returns False.
    """
    if not url1 or not url2:
        return False
    try:
        p1 = urlparse(url1)
        p2 = urlparse(url2)
        # netloc includes port when it is non-standard (e.g. "example.com:8080")
        return (p1.scheme, p1.netloc) == (p2.scheme, p2.netloc)
    except Exception:  # noqa: BLE001
        return False


class EmbeddedDashboardController(Controller):
    path = "/api/v1/embedded_dashboard"
    tags = ["Embedded Dashboard"]
    dependencies = {
        "embedded_dao": Provide(provide_embedded_dao, sync_to_thread=False),
    }

    @get(
        "/{uuid:str}",
        opt={"exclude_from_auth": True},
    )
    async def get_embedded(
        self,
        uuid: str,
        state: State,
        embedded_dao: Any,
    ) -> dict[str, Any]:
        """GET /api/v1/embedded_dashboard/{uuid} — get embedded dashboard config."""
        # Check EMBEDDED_SUPERSET feature flag
        feature_flags = getattr(state.settings, "feature_flags", {})
        if not feature_flags.get("EMBEDDED_SUPERSET", False):
            raise SupersetNotFoundError("Embedded dashboards are not enabled")

        embedded = await embedded_dao.find_by_uuid(uuid)
        if embedded is None:
            raise SupersetNotFoundError("Embedded dashboard not found")

        # allow_domain_list is stored as comma-separated string in the DB
        raw_domains = getattr(embedded, "allow_domain_list", None)
        allowed_domains: list[str] = []
        if raw_domains:
            allowed_domains = [d for d in raw_domains.split(",") if d]

        return {
            "result": {
                "uuid": str(embedded.uuid),
                "dashboard_id": embedded.dashboard_id,
                "allowed_domains": allowed_domains,
            },
        }


class EmbeddedSSRController(Controller):
    """Server-side-rendered HTML route for the embedded Superset iframe.

    Mounted at ``/embedded`` (no ``/api/v1`` prefix) — mirrors
    ``superset_old/embedded/view.py:EmbeddedView`` 1:1.

    Steps performed on each request:
    1. Gate on ``EMBEDDED_SUPERSET`` feature flag → 404 if off.
    2. Look up the ``EmbeddedDashboard`` record by UUID → 404 if missing.
    3. Validate the ``Referer`` header against ``allowed_domains`` via
       ``_same_origin`` → 403 when the referrer is not allowed and the
       domain list is non-empty.
    4. Build bootstrap data (``config``, ``common``, ``embedded``) and
       render the SPA template with ``entry="embedded"``.
    """

    path = "/embedded"
    tags = ["Embedded"]
    dependencies = {
        "embedded_dao": Provide(provide_embedded_dao, sync_to_thread=False),
    }

    @get(
        "/{uuid:str}",
        media_type="text/html",
        # No authentication required — guest tokens are handled by the frontend.
        opt={"exclude_from_auth": True},
    )
    async def embedded(
        self,
        uuid: str,
        request: Request[Any, Any, Any],
        state: State,
        embedded_dao: Any,
    ) -> Any:
        """GET /embedded/{uuid} — serve the embedded dashboard SPA shell.

        1:1 port of ``superset_old/embedded/view.py:EmbeddedView.embedded``.
        """
        settings = getattr(state, "settings", None)
        feature_flags = getattr(settings, "feature_flags", {}) or {}

        if not feature_flags.get("EMBEDDED_SUPERSET", False):
            return Response(
                content=b"Not found",
                status_code=404,
                media_type="text/plain",
            )

        embedded = await embedded_dao.find_by_uuid(uuid)
        if embedded is None:
            return Response(
                content=b"Not found",
                status_code=404,
                media_type="text/plain",
            )

        # Validate request referrer against allowed_domains.
        # An empty allowed_domains list means any origin is permitted —
        # 1:1 with the original ``not embedded.allowed_domains`` short-circuit.
        allowed_domains: list[str] = getattr(embedded, "allowed_domains", []) or []
        if allowed_domains:
            referrer = request.headers.get("Referer") or request.headers.get(
                "Referrer"
            )
            is_referrer_allowed = any(
                _same_origin(referrer, domain) for domain in allowed_domains
            )
            if not is_referrer_allowed:
                return Response(
                    content=b"Forbidden",
                    status_code=403,
                    media_type="text/plain",
                )

        # Build the bootstrap_data matching the original EmbeddedView payload.
        # The frontend's embedded entry (superset-frontend/src/embedded/index.tsx)
        # reads ``bootstrapData.config.GUEST_TOKEN_HEADER_NAME``,
        # ``bootstrapData.common``, and ``bootstrapData.embedded.dashboard_id``.
        guest_token_header = getattr(
            settings, "guest_token_header_name", "X-GuestToken"
        )

        # Build common bootstrap payload (feature flags, conf, menu, etc.)
        # Re-use the SPA controller helper to avoid duplicating the logic.
        from superset.controllers.spa import _build_bootstrap_data

        # For the embedded route the user is always treated as anonymous;
        # the actual authentication is done via the guest token that the
        # embedding application injects into request headers.
        from superset.middleware.auth import UnauthenticatedUser

        anon_user = UnauthenticatedUser()
        common_data = _build_bootstrap_data(anon_user, settings).get("common", {})

        bootstrap_data: dict[str, Any] = {
            "config": {
                "GUEST_TOKEN_HEADER_NAME": guest_token_header,
            },
            "common": common_data,
            "embedded": {
                "dashboard_id": embedded.dashboard_id,
            },
        }

        assets_prefix = getattr(settings, "static_assets_prefix", "")

        return Template(
            template_name="spa.html",
            context={
                "bootstrap_data": json.dumps(bootstrap_data),
                "entry": "embedded",
                "title": "Superset",
                "assets_prefix": assets_prefix,
                "standalone_mode": False,
                "favicons": [
                    {"href": "/static/assets/images/favicon.png"},
                ],
                "csrf_token": "",
            },
        )
