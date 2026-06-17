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
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from litestar import Controller, get
from litestar.connection import Request
from litestar.datastructures import State
from litestar.di import Provide
from litestar.response import Response, Template

from superset.exceptions import SupersetNotFoundError
from superset.guards.rbac import require_feature_flag, require_permission
from superset.providers import provide_embedded_dao
from superset.utils import json as json_utils


def _same_origin(url1: str | None, url2: str | None) -> bool:
    """Return True when *url1* and *url2* share the same scheme + netloc.

    If either argument is empty/None the function returns False.
    """
    if not url1 or not url2:
        return False
    try:
        p1 = urlparse(url1)
        p2 = urlparse(url2)
        # Use .hostname (auto-lowercased) and .port (int or None) separately
        # so that uppercase hostnames in headers are treated case-insensitively.
        return (
            p1.scheme == p2.scheme and p1.hostname == p2.hostname and p1.port == p2.port
        )
    except Exception:  # noqa: BLE001
        return False


class EmbeddedDashboardController(Controller):
    path = "/api/v1/embedded_dashboard"
    tags = ["Embedded Dashboard"]
    guards = [require_feature_flag("EMBEDDED_SUPERSET")]
    dependencies = {
        "embedded_dao": Provide(provide_embedded_dao, sync_to_thread=False),
    }

    @get(
        "/{uuid:str}",
        guards=[require_permission("can_read", "EmbeddedDashboard")],
    )
    async def get_embedded(
        self,
        uuid: str,
        state: State,
        embedded_dao: Any,
    ) -> dict[str, Any]:
        """GET /api/v1/embedded_dashboard/{uuid} — get embedded dashboard config."""

        embedded = await embedded_dao.find_by_uuid(uuid)
        if embedded is None:
            raise SupersetNotFoundError("Embedded dashboard not found")

        # allow_domain_list is stored as comma-separated string in the DB.
        # Plain ``split(",")`` with NO empty-token filtering —
        # ``'a,,b'`` yields ``['a', '', 'b']``.
        raw_domains = getattr(embedded, "allow_domain_list", None)
        allowed_domains: list[str] = []
        if raw_domains:
            allowed_domains = raw_domains.split(",")

        changed_by_data: dict[str, Any] | None = None
        changed_by = getattr(embedded, "changed_by", None)
        if changed_by is not None:
            changed_by_data = {
                "id": changed_by.id,
                "username": getattr(changed_by, "username", None),
                "first_name": getattr(changed_by, "first_name", None),
                "last_name": getattr(changed_by, "last_name", None),
            }

        changed_on = getattr(embedded, "changed_on", None)
        changed_on_str: str | None = None
        if changed_on is not None:
            changed_on_str = changed_on.isoformat()

        return {
            "result": {
                "uuid": str(embedded.uuid),
                "dashboard_id": str(embedded.dashboard_id),
                "allowed_domains": allowed_domains,
                "changed_on": changed_on_str,
                "changed_by": changed_by_data,
            },
        }


class EmbeddedSSRController(Controller):
    """Server-side-rendered HTML route for the embedded Superset iframe.

    Mounted at ``/embedded`` (no ``/api/v1`` prefix).

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
    guards = [require_feature_flag("EMBEDDED_SUPERSET")]
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
    ) -> Template | Response[Any]:
        """GET /embedded/{uuid} — serve the embedded dashboard SPA shell."""
        settings = getattr(state, "settings", None)

        embedded = await embedded_dao.find_by_uuid(uuid)
        if embedded is None:
            return Response(
                content=b"Not found",
                status_code=404,
                media_type="text/plain",
            )

        # Validate request referrer against allowed_domains.
        # An empty allowed_domains list means any origin is permitted.
        allowed_domains: list[str] = getattr(embedded, "allowed_domains", []) or []
        if allowed_domains:
            referrer = request.headers.get("Referer") or request.headers.get("Referrer")
            is_referrer_allowed = any(
                _same_origin(referrer, domain) for domain in allowed_domains
            )
            if not is_referrer_allowed:
                return Response(
                    content=b"Forbidden",
                    status_code=403,
                    media_type="text/plain",
                )

        # The frontend embedded entry (superset-frontend/src/embedded/index.tsx)
        # reads ``bootstrapData.config.GUEST_TOKEN_HEADER_NAME``,
        # ``bootstrapData.common``, and ``bootstrapData.embedded.dashboard_id``.
        guest_token_header = getattr(
            settings, "guest_token_header_name", "X-GuestToken"
        )

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

        from superset.events import event_logger

        await event_logger.alog_with_context(
            "EmbeddedView.embedded",
            extra={
                "embedded_dashboard_id": uuid,
                "dashboard_version": "v2",
            },
        )

        return Template(
            template_name="spa.html",
            context={
                "bootstrap_data": json_utils.dumps(
                    bootstrap_data,
                    default=json_utils.pessimistic_json_iso_dttm_ser,
                ),
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
