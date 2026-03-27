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
"""Integration tests for RBAC guards with Litestar routing."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from litestar import get, Litestar
from litestar.connection import ASGIConnection
from litestar.middleware import AbstractAuthenticationMiddleware, AuthenticationResult
from litestar.testing import AsyncTestClient

from superset.guards.rbac import require_permission


@dataclass
class FakeUser:
    username: str = "admin"
    permissions: set[str] = field(
        default_factory=lambda: {"can_read_Chart", "can_write_Chart"}
    )
    is_authenticated: bool = True


@dataclass
class FakeViewerUser:
    username: str = "viewer"
    permissions: set[str] = field(default_factory=lambda: {"can_read_Chart"})
    is_authenticated: bool = True


class FakeAuthMiddleware(AbstractAuthenticationMiddleware):
    async def authenticate_request(
        self, connection: ASGIConnection
    ) -> AuthenticationResult:
        role = connection.headers.get("x-test-role", "admin")
        if role == "viewer":
            return AuthenticationResult(user=FakeViewerUser(), auth="test")
        if role == "anonymous":
            return AuthenticationResult(
                user=FakeUser(permissions=set(), is_authenticated=False), auth="test"
            )
        return AuthenticationResult(user=FakeUser(), auth="test")


@get("/charts", guards=[require_permission("can_read", "Chart")])
async def list_charts() -> dict[str, str]:
    return {"result": "charts"}


@get("/charts/create", guards=[require_permission("can_write", "Chart")])
async def create_chart() -> dict[str, str]:
    return {"result": "created"}


@pytest.fixture
def guarded_app() -> Litestar:
    return Litestar(
        route_handlers=[list_charts, create_chart],
        middleware=[FakeAuthMiddleware],
    )


async def test_admin_can_read(guarded_app: Litestar) -> None:
    async with AsyncTestClient(app=guarded_app) as client:
        resp = await client.get("/charts", headers={"x-test-role": "admin"})
        assert resp.status_code == 200
        assert resp.json() == {"result": "charts"}


async def test_admin_can_write(guarded_app: Litestar) -> None:
    async with AsyncTestClient(app=guarded_app) as client:
        resp = await client.get("/charts/create", headers={"x-test-role": "admin"})
        assert resp.status_code == 200


async def test_viewer_can_read(guarded_app: Litestar) -> None:
    async with AsyncTestClient(app=guarded_app) as client:
        resp = await client.get("/charts", headers={"x-test-role": "viewer"})
        assert resp.status_code == 200


async def test_viewer_cannot_write(guarded_app: Litestar) -> None:
    async with AsyncTestClient(app=guarded_app) as client:
        resp = await client.get("/charts/create", headers={"x-test-role": "viewer"})
        assert resp.status_code == 403


async def test_anonymous_unauthorized(guarded_app: Litestar) -> None:
    async with AsyncTestClient(app=guarded_app) as client:
        resp = await client.get("/charts", headers={"x-test-role": "anonymous"})
        assert resp.status_code == 401
