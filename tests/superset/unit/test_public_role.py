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
"""Tests for Public role support for anonymous users.

Verifies that:
- Anonymous users receive Public role permissions from the database
- RBAC guards allow anonymous access to public endpoints
- RBAC guards block anonymous access to private endpoints
- require_authenticated_user strictly requires login
- Redis caching of Public role permissions works correctly
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from litestar import get, Litestar
from litestar.connection import Request
from litestar.datastructures import State
from litestar.exceptions import NotAuthorizedException, PermissionDeniedException
from litestar.testing import AsyncTestClient

from superset.guards.rbac import (
    has_permissions,
    require_authenticated_user,
    require_authentication,
    require_permission,
)
from superset.middleware.auth import (
    _PUBLIC_ROLE_CACHE_KEY,
    _PUBLIC_ROLE_CACHE_TTL,
    SupersetAuthMiddleware,
    UnauthenticatedUser,
)

SECRET_KEY = "test-secret-key-at-least-16-chars"


def _signed_public_role_entry(
    permissions: list[list[str]],
    role: str = "Public",
    epoch: str = "",
    secret: str = SECRET_KEY,
) -> str:
    """Build a Public-role cache envelope the middleware will accept."""
    from superset.middleware.auth import _PUBLIC_ROLE_CACHE_KEY
    from superset.security.auth_cache import sign_keyed_payload

    body = json.dumps({"epoch": epoch, "role": role, "permissions": permissions})
    return json.dumps(
        {"sig": sign_keyed_payload(_PUBLIC_ROLE_CACHE_KEY, body, secret), "data": body}
    )


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------


@dataclass
class MockRole:
    id: int = 0
    name: str = "Public"


def _make_settings(**overrides: object) -> MagicMock:
    defaults = {
        "secret_key": MagicMock(get_secret_value=MagicMock(return_value=SECRET_KEY)),
        "session_cookie_name": "session",
        "session_max_age": 86400,
        "embedded_superset": False,
        "guest_token_jwt_secret": "",
        "guest_token_jwt_algo": "HS256",
        "auth_role_public": "Public",
        # Real types required by the guest-token branch in
        # SupersetAuthMiddleware (a bare MagicMock would make
        # feature_flags.get() truthy and header_name.lower() fail).
        "feature_flags": {},
        "guest_token_header_name": "X-GuestToken",
    }
    defaults.update(overrides)
    return MagicMock(**defaults)


def _make_mock_connection(user: object, auth_role_admin: str = "Admin") -> MagicMock:
    from litestar.connection import ASGIConnection

    conn = MagicMock(spec=ASGIConnection)
    conn.user = user
    conn.app.state.settings.auth_role_admin = auth_role_admin
    return conn


# ---------------------------------------------------------------------------
# UnauthenticatedUser dataclass tests
# ---------------------------------------------------------------------------


class TestUnauthenticatedUser:
    def test_default_has_no_permissions(self) -> None:
        user = UnauthenticatedUser()
        assert user.is_authenticated is False
        assert user.roles == []
        assert user.permissions == set()

    def test_with_public_role_permissions(self) -> None:
        user = UnauthenticatedUser(
            roles=[MockRole()],
            permissions={("can_read", "Dashboard"), ("can_read", "Chart")},
        )
        assert user.is_authenticated is False
        assert len(user.roles) == 1
        assert user.roles[0].name == "Public"
        assert ("can_read", "Dashboard") in user.permissions
        assert ("can_read", "Chart") in user.permissions

    def test_has_permissions_helper_works(self) -> None:
        user = UnauthenticatedUser(
            permissions={("can_read", "Dashboard")},
        )
        assert has_permissions(user, {("can_read", "Dashboard")}) is True
        assert has_permissions(user, {("can_write", "Dashboard")}) is False


# ---------------------------------------------------------------------------
# RBAC guard tests with Public role
# ---------------------------------------------------------------------------


class TestRequireAuthenticationWithPublicRole:
    def test_allows_authenticated_user(self) -> None:
        user = MagicMock(is_authenticated=True, permissions=set())
        conn = _make_mock_connection(user)
        require_authentication(conn, MagicMock())

    def test_rejects_anonymous_with_public_permissions(self) -> None:
        """require_authentication must reject anonymous users even when the
        Public role carries some permissions.

        The original FAB @protect() / @has_access_api only allow anonymous
        access when the Public role has the *specific* endpoint permission
        (is_item_public(permission_str, class_permission_name)).
        require_authentication has no endpoint-specific parameter and
        therefore must deny all anonymous callers — endpoints that need
        Public-role anonymous access must use require_permission(action,
        resource) instead, which performs the correct per-endpoint check.
        """
        user = UnauthenticatedUser(permissions={("can_read", "Dashboard")})
        conn = _make_mock_connection(user)
        with pytest.raises(NotAuthorizedException, match="Not authenticated"):
            require_authentication(conn, MagicMock())

    def test_rejects_anonymous_without_permissions(self) -> None:
        user = UnauthenticatedUser()
        conn = _make_mock_connection(user)
        with pytest.raises(NotAuthorizedException, match="Not authenticated"):
            require_authentication(conn, MagicMock())


class TestRequireAuthenticatedUser:
    def test_allows_authenticated_user(self) -> None:
        user = MagicMock(is_authenticated=True)
        conn = _make_mock_connection(user)
        require_authenticated_user(conn, MagicMock())

    def test_rejects_anonymous_even_with_permissions(self) -> None:
        user = UnauthenticatedUser(permissions={("can_read", "Dashboard")})
        conn = _make_mock_connection(user)
        with pytest.raises(NotAuthorizedException, match="Not authenticated"):
            require_authenticated_user(conn, MagicMock())

    def test_rejects_anonymous_without_permissions(self) -> None:
        user = UnauthenticatedUser()
        conn = _make_mock_connection(user)
        with pytest.raises(NotAuthorizedException, match="Not authenticated"):
            require_authenticated_user(conn, MagicMock())


class TestRequirePermissionWithPublicRole:
    def test_anonymous_with_matching_public_permission(self) -> None:
        """Anonymous user with can_read_Dashboard in Public role should pass."""
        guard = require_permission("can_read", "Dashboard")
        user = UnauthenticatedUser(permissions={("can_read", "Dashboard")})
        conn = _make_mock_connection(user)
        # Should not raise
        guard(conn, MagicMock())

    def test_anonymous_without_matching_permission(self) -> None:
        """Anonymous user without the required permission should be rejected."""
        guard = require_permission("can_write", "Dashboard")
        user = UnauthenticatedUser(permissions={("can_read", "Dashboard")})
        conn = _make_mock_connection(user)
        with pytest.raises(NotAuthorizedException, match="Not authenticated"):
            guard(conn, MagicMock())

    def test_anonymous_with_no_permissions(self) -> None:
        """Bare anonymous user should be rejected."""
        guard = require_permission("can_read", "Dashboard")
        user = UnauthenticatedUser()
        conn = _make_mock_connection(user)
        with pytest.raises(NotAuthorizedException, match="Not authenticated"):
            guard(conn, MagicMock())

    def test_authenticated_user_with_permission(self) -> None:
        guard = require_permission("can_read", "Chart")
        user = MagicMock(
            is_authenticated=True,
            permissions={("can_read", "Chart")},
            roles=[],
        )
        conn = _make_mock_connection(user)
        guard(conn, MagicMock())

    def test_authenticated_user_missing_permission(self) -> None:
        guard = require_permission("can_write", "Chart")
        user = MagicMock(
            is_authenticated=True,
            permissions={("can_read", "Chart")},
            roles=[],
        )
        conn = _make_mock_connection(user)
        with pytest.raises(PermissionDeniedException, match="can_write on Chart"):
            guard(conn, MagicMock())

    def test_admin_passes_through_its_seeded_permissions(self) -> None:
        """Holding the Admin role is not itself authorization.

        Upstream Flask-AppBuilder's ``has_access`` never special-cases
        ``AUTH_ROLE_ADMIN`` — Admin reaches a route because role seeding gave
        it the permission. Revoking that permission from the role must deny.
        """
        guard = require_permission("can_write", "Chart")
        admin_role = MockRole(id=1, name="Admin")

        granted = MagicMock(
            is_authenticated=True,
            permissions={("can_write", "Chart")},
            roles=[admin_role],
        )
        guard(_make_mock_connection(granted), MagicMock())

        revoked = MagicMock(
            is_authenticated=True,
            permissions=set(),
            roles=[admin_role],
        )
        with pytest.raises(PermissionDeniedException):
            guard(_make_mock_connection(revoked), MagicMock())


# ---------------------------------------------------------------------------
# Middleware: _build_anonymous_user tests
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_session_factory() -> MagicMock:
    session = AsyncMock()
    factory = MagicMock(return_value=session)
    factory.__aenter__ = AsyncMock(return_value=session)
    factory.__aexit__ = AsyncMock(return_value=False)
    return factory


@get("/check-anon")
async def check_anon_route(request: Request) -> dict:
    user = request.user
    return {
        "is_authenticated": getattr(user, "is_authenticated", False),
        "permissions": sorted(getattr(user, "permissions", set())),
        "has_roles": bool(getattr(user, "roles", [])),
    }


class TestBuildAnonymousUser:
    """Tests for the middleware's _build_anonymous_user method."""

    async def test_anonymous_gets_public_permissions_from_db(
        self, mock_session_factory: MagicMock
    ) -> None:
        """Anonymous user should receive Public role permissions from DB."""
        state = State(
            {
                "settings": _make_settings(),
                "session_factory": mock_session_factory,
                "redis": None,
            }
        )
        app = Litestar(
            route_handlers=[check_anon_route],
            middleware=[SupersetAuthMiddleware],
            state=state,
        )

        # Mock the DAO to return permissions for Public role
        with patch(
            "superset.middleware.auth.SupersetAuthMiddleware._resolve_public_permissions",
            return_value={("can_read", "Dashboard"), ("can_read", "Chart")},
        ):
            async with AsyncTestClient(app=app) as client:
                resp = await client.get("/check-anon")
                assert resp.status_code == 200
                data = resp.json()
                assert data["is_authenticated"] is False
                assert ["can_read", "Chart"] in data["permissions"]
                assert ["can_read", "Dashboard"] in data["permissions"]
                assert data["has_roles"] is True

    async def test_anonymous_no_permissions_when_public_role_empty(
        self, mock_session_factory: MagicMock
    ) -> None:
        """When Public role has no permissions, anonymous user gets none."""
        state = State(
            {
                "settings": _make_settings(),
                "session_factory": mock_session_factory,
                "redis": None,
            }
        )
        app = Litestar(
            route_handlers=[check_anon_route],
            middleware=[SupersetAuthMiddleware],
            state=state,
        )

        with patch(
            "superset.middleware.auth.SupersetAuthMiddleware._resolve_public_permissions",
            return_value=set(),
        ):
            async with AsyncTestClient(app=app) as client:
                resp = await client.get("/check-anon")
                assert resp.status_code == 200
                data = resp.json()
                assert data["is_authenticated"] is False
                assert data["permissions"] == []
                assert data["has_roles"] is False

    async def test_anonymous_no_permissions_when_role_name_empty(
        self, mock_session_factory: MagicMock
    ) -> None:
        """When auth_role_public is empty string, no DB lookup occurs."""
        state = State(
            {
                "settings": _make_settings(auth_role_public=""),
                "session_factory": mock_session_factory,
                "redis": None,
            }
        )
        app = Litestar(
            route_handlers=[check_anon_route],
            middleware=[SupersetAuthMiddleware],
            state=state,
        )

        async with AsyncTestClient(app=app) as client:
            resp = await client.get("/check-anon")
            assert resp.status_code == 200
            data = resp.json()
            assert data["is_authenticated"] is False
            assert data["permissions"] == []

    async def test_redis_cache_hit_for_public_permissions(
        self, mock_session_factory: MagicMock
    ) -> None:
        """When Redis has cached public permissions, DB is not queried."""
        mock_redis = AsyncMock()
        # The entry is a signed envelope keyed to the cache key and the current
        # epoch — an unsigned blob is discarded, so that a Redis write cannot
        # grant permissions to anonymous callers.
        mock_redis.mget = AsyncMock(
            return_value=[
                _signed_public_role_entry(
                    [["can_read", "Dashboard"], ["can_read", "Chart"]]
                ),
                None,
            ]
        )

        state = State(
            {
                "settings": _make_settings(),
                "session_factory": mock_session_factory,
                "redis": mock_redis,
            }
        )
        app = Litestar(
            route_handlers=[check_anon_route],
            middleware=[SupersetAuthMiddleware],
            state=state,
        )

        with patch(
            "superset.middleware.auth.SupersetAuthMiddleware._resolve_public_permissions"
        ) as mock_resolve:
            async with AsyncTestClient(app=app) as client:
                resp = await client.get("/check-anon")
                assert resp.status_code == 200
                data = resp.json()
                assert ["can_read", "Dashboard"] in data["permissions"]
                # DB should NOT be called since Redis had the data
                mock_resolve.assert_not_called()

    async def test_redis_cache_miss_falls_through_to_db(
        self, mock_session_factory: MagicMock
    ) -> None:
        """When Redis cache misses, permissions are loaded from DB and cached."""
        mock_redis = AsyncMock()
        # ``_get_cached_public_role_perms`` reads the entry and the cache
        # epoch in one MGET, not a plain GET -- an unconfigured ``mget`` on a
        # bare AsyncMock returns a non-iterable default, and unpacking it
        # would raise before ever reaching the real cache-miss branch this
        # test names.
        mock_redis.mget = AsyncMock(return_value=[None, None])
        mock_redis.get = AsyncMock(return_value=None)
        mock_redis.set = AsyncMock()

        state = State(
            {
                "settings": _make_settings(),
                "session_factory": mock_session_factory,
                "redis": mock_redis,
            }
        )
        app = Litestar(
            route_handlers=[check_anon_route],
            middleware=[SupersetAuthMiddleware],
            state=state,
        )

        with patch(
            "superset.middleware.auth.SupersetAuthMiddleware._resolve_public_permissions",
            return_value={("can_read", "Dashboard")},
        ):
            async with AsyncTestClient(app=app) as client:
                resp = await client.get("/check-anon")
                assert resp.status_code == 200
                data = resp.json()
                assert ["can_read", "Dashboard"] in data["permissions"]

                # Verify Redis was populated
                mock_redis.set.assert_called_once()
                call_args = mock_redis.set.call_args
                assert call_args[0][0] == _PUBLIC_ROLE_CACHE_KEY
                assert call_args[1]["ex"] == _PUBLIC_ROLE_CACHE_TTL

    async def test_redis_error_gracefully_degrades(
        self, mock_session_factory: MagicMock
    ) -> None:
        """Redis errors should not prevent anonymous user creation."""
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(side_effect=Exception("Redis down"))
        mock_redis.set = AsyncMock(side_effect=Exception("Redis down"))

        state = State(
            {
                "settings": _make_settings(),
                "session_factory": mock_session_factory,
                "redis": mock_redis,
            }
        )
        app = Litestar(
            route_handlers=[check_anon_route],
            middleware=[SupersetAuthMiddleware],
            state=state,
        )

        with patch(
            "superset.middleware.auth.SupersetAuthMiddleware._resolve_public_permissions",
            return_value={("can_read", "Dashboard")},
        ):
            async with AsyncTestClient(app=app) as client:
                resp = await client.get("/check-anon")
                assert resp.status_code == 200
                data = resp.json()
                assert ["can_read", "Dashboard"] in data["permissions"]


# ---------------------------------------------------------------------------
# DAO: get_permissions_for_role_name tests
# ---------------------------------------------------------------------------


class TestSecurityDAOPublicRole:
    """Tests for AsyncSecurityDAO.get_permissions_for_role_name and get_role_by_name.

    Uses real SQLAlchemy model classes since select() validates its arguments.
    The session.execute() is mocked to return expected results.
    """

    async def test_get_permissions_for_role_name_returns_tuples(self) -> None:
        """DAO should return (permission, view_menu) tuples for a role name."""
        from superset.models.security import (
            Permission,
            PermissionView,
            Role,
            ViewMenu,
        )
        from superset.security.dao import AsyncSecurityDAO

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.all.return_value = [
            ("can_read", "Dashboard"),
            ("can_read", "Chart"),
        ]
        mock_session.execute = AsyncMock(return_value=mock_result)

        dao = AsyncSecurityDAO(
            mock_session,
            role_model=Role,
            permission_model=Permission,
            view_menu_model=ViewMenu,
            permission_view_model=PermissionView,
        )
        result = await dao.get_permissions_for_role_name("Public")
        assert result == {("can_read", "Dashboard"), ("can_read", "Chart")}

    async def test_get_role_by_name(self) -> None:
        """DAO should return role object when found by name."""
        from superset.models.security import Role
        from superset.security.dao import AsyncSecurityDAO

        mock_session = AsyncMock()
        mock_role_obj = MagicMock(id=1)
        mock_role_obj.name = "Public"
        mock_result = MagicMock()
        mock_result.scalars.return_value.one_or_none.return_value = mock_role_obj
        mock_session.execute = AsyncMock(return_value=mock_result)

        dao = AsyncSecurityDAO(mock_session, role_model=Role)
        result = await dao.get_role_by_name("Public")
        assert result is mock_role_obj

    async def test_get_role_by_name_not_found(self) -> None:
        """DAO should return None when role not found."""
        from superset.models.security import Role
        from superset.security.dao import AsyncSecurityDAO

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result)

        dao = AsyncSecurityDAO(mock_session, role_model=Role)
        result = await dao.get_role_by_name("NonExistent")
        assert result is None
