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
"""Security controller — CSRF token and auth-related endpoints."""
from __future__ import annotations

from typing import Any

from litestar import Controller, get
from litestar.connection import Request


class SecurityController(Controller):
    """Security-related API endpoints."""

    path = "/api/v1/security"

    @get(
        "/csrf_token/",
        opt={"exclude_from_auth": True},
    )
    async def csrf_token(self, request: Request[Any, Any, Any]) -> dict[str, str]:
        """Get a CSRF token for state-changing requests.

        Returns the CSRF token from the cookie set by Litestar's
        CSRFConfig middleware. On the first request (no cookie yet),
        Litestar's CSRF middleware sets the cookie in the response —
        the client should read it from the Set-Cookie header or
        cookie jar.

        Backward-compatible with Superset frontend's
        GET /api/v1/security/csrf_token/ endpoint.
        """
        # Read the CSRF cookie that Litestar's CSRFConfig middleware sets.
        # The cookie name is configurable but defaults to "csrf_access_token".
        settings = getattr(request.app.state, "settings", None)
        cookie_name = getattr(settings, "csrf_cookie_name", "csrf_access_token")
        token = request.cookies.get(cookie_name, "")
        return {"result": token}
