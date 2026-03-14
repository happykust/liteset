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
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from litestar.connection import ASGIConnection
from litestar.exceptions import NotAuthorizedException
from litestar.middleware import AbstractAuthenticationMiddleware, AuthenticationResult


@dataclass
class UnauthenticatedUser:
    is_authenticated: bool = False


class LitesetAuthMiddleware(AbstractAuthenticationMiddleware):
    async def authenticate_request(
        self, connection: ASGIConnection[Any, Any, Any, Any]
    ) -> AuthenticationResult:
        user = await self._authenticate_cookie(connection)
        if user:
            return AuthenticationResult(user=user, auth="cookie")

        user = await self._authenticate_jwt(connection)
        if user:
            return AuthenticationResult(user=user, auth="jwt")

        user = await self._authenticate_api_key(connection)
        if user:
            return AuthenticationResult(user=user, auth="api_key")

        raise NotAuthorizedException(detail="Not authenticated")

    async def _authenticate_cookie(
        self, connection: ASGIConnection[Any, Any, Any, Any]
    ) -> Any | None:
        # TODO(liteset/data-layer): implement cookie-based session auth
        # using Flask-Login compatible session decoding
        return None

    async def _authenticate_jwt(
        self, connection: ASGIConnection[Any, Any, Any, Any]
    ) -> Any | None:
        # TODO(liteset/data-layer): implement JWT bearer token validation
        auth_header = connection.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            return None
        return None

    async def _authenticate_api_key(
        self, connection: ASGIConnection[Any, Any, Any, Any]
    ) -> Any | None:
        # TODO(liteset/data-layer): implement API key auth
        # via X-API-Key header or query parameter
        return None
