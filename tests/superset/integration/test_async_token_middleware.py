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
"""Integration tests for AsyncTokenMiddleware.

Verifies the middleware mints / refreshes the ``async-token`` JWT cookie on
authenticated responses — 1:1 with the original Flask
``register_request_handlers`` after-request hook.
"""

from __future__ import annotations

from http.cookies import SimpleCookie
from types import SimpleNamespace
from typing import Any

import jwt as pyjwt
from litestar import Litestar, get
from litestar.datastructures import State
from litestar.testing import AsyncTestClient

from superset.middleware.async_token import AsyncTokenMiddleware

JWT_SECRET = "test-secret-key-that-is-32-bytes!"


class _User:
    def __init__(self, uid: int | None, authed: bool) -> None:
        self.id = uid
        self.is_authenticated = authed


def _fake_auth(user: _User):
    """Litestar middleware factory that injects ``scope['user']`` like the real
    AbstractAuthenticationMiddleware would."""

    class _FakeAuth:
        def __init__(self, app: Any) -> None:
            self.app = app

        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            if scope["type"] == "http":
                scope["user"] = user
            await self.app(scope, receive, send)

    return _FakeAuth


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        global_async_queries_jwt_secret=JWT_SECRET,
        secret_key=JWT_SECRET,
        global_async_queries_jwt_cookie_name="async-token",
        global_async_queries_jwt_cookie_secure=False,
        global_async_queries_jwt_cookie_samesite=None,
        global_async_queries_jwt_cookie_domain=None,
    )


def _make_app(user: _User) -> Litestar:
    @get("/ping")
    async def ping() -> dict[str, str]:
        return {"ok": "1"}

    return Litestar(
        route_handlers=[ping],
        middleware=[_fake_auth(user), AsyncTokenMiddleware()],
        # redis=None -> channel id is a fresh uuid (no persistence), which is
        # fine for asserting the cookie is minted with the right shape.
        state=State({"settings": _settings(), "redis": None}),
    )


def _async_token(resp: Any) -> str | None:
    """Pull the async-token value out of any Set-Cookie header on the response."""
    for name, value in resp.headers.multi_items():
        if name.lower() == "set-cookie" and value.startswith("async-token="):
            c: SimpleCookie = SimpleCookie()
            c.load(value)
            m = c.get("async-token")
            return m.value if m else None
    return None


async def test_mints_cookie_for_authenticated_user():
    app = _make_app(_User(42, authed=True))
    async with AsyncTestClient(app) as client:
        resp = await client.get("/ping")
        token = _async_token(resp)
        assert token, "expected an async-token Set-Cookie"
        payload = pyjwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        assert payload["sub"] == "42"
        assert payload.get("channel")  # a channel id is present


async def test_anonymous_user_gets_cookie_with_null_sub():
    app = _make_app(_User(None, authed=False))
    async with AsyncTestClient(app) as client:
        resp = await client.get("/ping")
        token = _async_token(resp)
        assert token, "anonymous request should still get an async-token cookie"
        # The original minted sub=None for anonymous; pyjwt >= 2.10 refuses to
        # decode a null sub unless verify_sub is disabled (the app's decode sites
        # catch this and fall back), so disable it here to inspect the claim.
        payload = pyjwt.decode(
            token, JWT_SECRET, algorithms=["HS256"], options={"verify_sub": False}
        )
        assert payload["sub"] is None  # str(user_id) if user_id else None -> None


async def test_no_refresh_when_valid_cookie_matches_user():
    app = _make_app(_User(42, authed=True))
    async with AsyncTestClient(app) as client:
        existing = pyjwt.encode(
            {"channel": "existing-uuid", "sub": "42"}, JWT_SECRET, algorithm="HS256"
        )
        resp = await client.get("/ping", headers={"Cookie": f"async-token={existing}"})
        # cookie sub matches the authenticated user -> middleware must NOT re-mint
        assert _async_token(resp) is None


async def test_refresh_when_cookie_sub_mismatches_user():
    app = _make_app(_User(7, authed=True))
    async with AsyncTestClient(app) as client:
        stale = pyjwt.encode(
            {"channel": "old-uuid", "sub": "42"}, JWT_SECRET, algorithm="HS256"
        )
        resp = await client.get("/ping", headers={"Cookie": f"async-token={stale}"})
        token = _async_token(resp)
        assert token, "sub mismatch must trigger a fresh cookie"
        payload = pyjwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        assert payload["sub"] == "7"
