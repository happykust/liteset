"""Tests for AuthMiddleware — cookie, JWT, and API key authentication."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from itsdangerous import URLSafeTimedSerializer
from litestar import get, Litestar
from litestar.connection import Request
from litestar.datastructures import State
from litestar.testing import AsyncTestClient

from liteset.middleware.auth import (
    CachedUser,
    LitesetAuthMiddleware,
    UnauthenticatedUser,
    _CachedRole,
)


SECRET_KEY = "test-secret-key-at-least-16-chars"


@dataclass
class MockUser:
    id: int = 1
    username: str = "admin"
    email: str = "admin@test.com"
    is_authenticated: bool = True
    is_active: bool = True
    active: int = 1
    roles: list = field(default_factory=list)


@dataclass
class MockRole:
    id: int = 1
    name: str = "Admin"


def _make_session_cookie(user_id: int) -> str:
    s = URLSafeTimedSerializer(SECRET_KEY, salt="cookie-session")
    return s.dumps({"_user_id": str(user_id)})


@get("/protected")
async def protected_route(request: Request) -> dict:
    user = request.user
    return {"username": getattr(user, "username", "anon")}


@get("/public", opt={"exclude_from_auth": True})
async def public_route() -> dict:
    return {"msg": "public"}


def _make_settings(**overrides):
    defaults = {
        "secret_key": MagicMock(get_secret_value=MagicMock(return_value=SECRET_KEY)),
        "session_cookie_name": "session",
        "embedded_superset": False,
        "guest_token_jwt_secret": "",
        "guest_token_jwt_algo": "HS256",
    }
    defaults.update(overrides)
    return MagicMock(**defaults)


@pytest.fixture
def mock_session_factory():
    session = AsyncMock()
    factory = MagicMock(return_value=session)
    factory.__aenter__ = AsyncMock(return_value=session)
    factory.__aexit__ = AsyncMock(return_value=False)
    return factory


@pytest.fixture
def app(mock_session_factory):
    state = State({
        "settings": _make_settings(),
        "session_factory": mock_session_factory,
        "redis": None,
    })
    return Litestar(
        route_handlers=[protected_route, public_route],
        middleware=[LitesetAuthMiddleware],
        state=state,
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


async def test_cookie_auth_success(app, mock_session_factory):
    mock_user = MockUser(roles=[MockRole()])

    with patch(
        "liteset.middleware.auth.LitesetAuthMiddleware._resolve_user_from_db"
    ) as mock_resolve:
        mock_resolve.return_value = mock_user
        async with AsyncTestClient(app=app) as client:
            cookie = _make_session_cookie(1)
            resp = await client.get(
                "/protected",
                cookies={"session": cookie},
            )
            assert resp.status_code == 200
            assert resp.json()["username"] == "admin"


async def test_invalid_cookie_returns_401(app):
    async with AsyncTestClient(app=app) as client:
        resp = await client.get(
            "/protected",
            cookies={"session": "invalid-cookie-data"},
        )
        assert resp.status_code == 401


async def test_jwt_auth_requires_embedded_superset_flag(mock_session_factory):
    """JWT auth should be rejected when embedded_superset is disabled."""
    state = State({
        "settings": _make_settings(embedded_superset=False),
        "session_factory": mock_session_factory,
        "redis": None,
    })
    app = Litestar(
        route_handlers=[protected_route, public_route],
        middleware=[LitesetAuthMiddleware],
        state=state,
    )
    async with AsyncTestClient(app=app) as client:
        resp = await client.get(
            "/protected",
            headers={"Authorization": "Bearer some.jwt.token"},
        )
        assert resp.status_code == 401


async def test_jwt_auth_success_with_embedded_flag(mock_session_factory):
    """JWT auth should work when embedded_superset is enabled."""
    mock_guest_user = MockUser(id=0, username="guest")
    state = State({
        "settings": _make_settings(embedded_superset=True),
        "session_factory": mock_session_factory,
        "redis": None,
    })
    app = Litestar(
        route_handlers=[protected_route, public_route],
        middleware=[LitesetAuthMiddleware],
        state=state,
    )

    with patch(
        "liteset.middleware.auth.LitesetAuthMiddleware._resolve_guest_from_jwt"
    ) as mock_resolve:
        mock_resolve.return_value = mock_guest_user
        async with AsyncTestClient(app=app) as client:
            resp = await client.get(
                "/protected",
                headers={"Authorization": "Bearer some.jwt.token"},
            )
            assert resp.status_code == 200


async def test_jwt_no_bearer_prefix_ignored(app):
    async with AsyncTestClient(app=app) as client:
        resp = await client.get(
            "/protected",
            headers={"Authorization": "Basic dXNlcjpwYXNz"},
        )
        assert resp.status_code == 401


# --- CachedUser tests ---

def test_cached_user_from_dict_with_roles():
    data = {
        "id": 1,
        "username": "admin",
        "email": "admin@test.com",
        "active": 1,
        "roles": [{"id": 1, "name": "Admin"}, {"id": 2, "name": "Gamma"}],
    }
    user = CachedUser.from_dict(data)
    assert user is not None
    assert user.id == 1
    assert user.username == "admin"
    assert len(user.roles) == 2
    assert user.roles[0].name == "Admin"
    assert user.active == 1


def test_cached_user_from_dict_inactive():
    data = {"id": 3, "username": "inactive", "active": 0, "roles": []}
    user = CachedUser.from_dict(data)
    assert user is not None
    assert user.active == 0


def test_cached_user_from_dict_invalid():
    assert CachedUser.from_dict({}) is None
    assert CachedUser.from_dict({"id": 1}) is None


async def test_redis_cache_rejects_inactive_user(mock_session_factory):
    """Deactivated users in Redis cache should be rejected."""
    mock_redis = AsyncMock()
    cached_data = json.dumps({
        "id": 3, "username": "inactive", "email": "x@x.com",
        "active": 0, "roles": [],
    })
    mock_redis.get = AsyncMock(return_value=cached_data)

    state = State({
        "settings": _make_settings(),
        "session_factory": mock_session_factory,
        "redis": mock_redis,
    })
    app = Litestar(
        route_handlers=[protected_route, public_route],
        middleware=[LitesetAuthMiddleware],
        state=state,
    )

    # Also mock DB fallback to return None (user inactive)
    with patch.object(
        LitesetAuthMiddleware, "_resolve_user_from_db",
        return_value=None,
    ):
        async with AsyncTestClient(app=app) as client:
            cookie = _make_session_cookie(3)
            resp = await client.get("/protected", cookies={"session": cookie})
            assert resp.status_code == 401


async def test_redis_cache_preserves_roles(mock_session_factory):
    """Cached users should retain their roles for permission checks."""
    mock_redis = AsyncMock()
    cached_data = json.dumps({
        "id": 1, "username": "admin", "email": "admin@test.com",
        "active": 1, "roles": [{"id": 1, "name": "Admin"}],
    })
    mock_redis.get = AsyncMock(return_value=cached_data)

    state = State({
        "settings": _make_settings(),
        "session_factory": mock_session_factory,
        "redis": mock_redis,
    })
    app = Litestar(
        route_handlers=[protected_route, public_route],
        middleware=[LitesetAuthMiddleware],
        state=state,
    )

    async with AsyncTestClient(app=app) as client:
        cookie = _make_session_cookie(1)
        resp = await client.get("/protected", cookies={"session": cookie})
        assert resp.status_code == 200
        data = resp.json()
        assert data["username"] == "admin"
