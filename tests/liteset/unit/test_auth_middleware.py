import pytest
from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

from litestar import Litestar, get, Request
from litestar.testing import AsyncTestClient
from liteset.middleware.auth import LitesetAuthMiddleware, UnauthenticatedUser


@get("/protected")
async def protected_route(request: Request) -> dict:
    return {"user": getattr(request.user, "username", "anon")}


@get("/public", opt={"exclude_from_auth": True})
async def public_route() -> dict:
    return {"msg": "public"}


@pytest.fixture
def app():
    return Litestar(
        route_handlers=[protected_route, public_route],
        middleware=[LitesetAuthMiddleware],
    )


async def test_unauthenticated_returns_401(app):
    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/protected")
        assert resp.status_code == 401


async def test_public_route_no_auth_needed(app):
    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/public")
        assert resp.status_code == 200
        assert resp.json() == {"msg": "public"}


@dataclass
class MockAuthenticatedUser:
    username: str = "admin"
    is_authenticated: bool = True


async def test_authenticated_user_passes(app):
    with patch.object(
        LitesetAuthMiddleware,
        "_authenticate_cookie",
        new_callable=AsyncMock,
        return_value=MockAuthenticatedUser(),
    ):
        async with AsyncTestClient(app=app) as client:
            resp = await client.get("/protected")
            assert resp.status_code == 200
            assert resp.json() == {"user": "admin"}
