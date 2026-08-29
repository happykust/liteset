"""Tests for JWT REST API login (POST /api/v1/security/login) and
refresh (POST /api/v1/security/refresh) endpoints, plus middleware
support for API access tokens."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from litestar import get, Litestar
from litestar.connection import Request
from litestar.datastructures import State
from litestar.testing import AsyncTestClient

from superset.controllers.security import (
    _create_api_access_token,
    _create_api_refresh_token,
    _get_jwt_secret,
    LoginRequest,
    LoginResponse,
    RefreshResponse,
    SecurityController,
)
from superset.middleware.auth import CachedUser, SupersetAuthMiddleware

SECRET_KEY = "test-secret-key-at-least-16-chars"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_password_hash(password: str) -> str:
    """Create a werkzeug-compatible pbkdf2:sha256 password hash."""
    salt = "testsalt"
    iterations = 260000
    derived = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations
    )
    return f"pbkdf2:sha256:{iterations}${salt}${derived.hex()}"


@dataclass
class MockUser:
    id: int = 1
    username: str = "admin"
    email: str = "admin@test.com"
    password: str = ""
    active: int = 1
    is_authenticated: bool = True
    fail_login_count: int = 0
    login_count: int = 0
    last_login: Any = None
    roles: list = field(default_factory=list)


@dataclass
class MockRole:
    id: int = 1
    name: str = "Admin"


def _make_settings(**overrides: Any) -> MagicMock:
    defaults = {
        "secret_key": MagicMock(get_secret_value=MagicMock(return_value=SECRET_KEY)),
        "session_cookie_name": "session",
        "session_max_age": 86400,
        "embedded_superset": False,
        "guest_token_jwt_secret": "",
        "guest_token_jwt_algo": "HS256",
        "guest_token_jwt_audience": "",
        "auth_type": 1,  # AUTH_DB
        "api_login_allow_multiple_providers": False,
        "jwt_access_token_expires": 900,
        "jwt_refresh_token_expires": 86400 * 30,
        # Real types required by the guest-token branch in
        # SupersetAuthMiddleware (a bare MagicMock would make
        # feature_flags.get() truthy and header_name.lower() fail).
        "feature_flags": {},
        "guest_token_header_name": "X-GuestToken",
    }
    defaults.update(overrides)
    return MagicMock(**defaults)


def _make_mock_session_factory(user: MockUser | None = None) -> MagicMock:
    """Create a mock session factory that returns a mock session.

    The mock session's DAO.get_user_by_username will return the given user.
    """
    session = AsyncMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()

    # Context manager support
    factory = MagicMock()
    factory.return_value = session
    factory.__aenter__ = AsyncMock(return_value=session)
    factory.__aexit__ = AsyncMock(return_value=False)

    # Make it work as `async with factory() as session:`
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    factory.return_value = cm

    return factory


# ---------------------------------------------------------------------------
# Token creation tests
# ---------------------------------------------------------------------------


class TestTokenCreation:
    """Test the module-level token creation helpers."""

    def test_create_access_token_structure(self):
        token = _create_api_access_token(
            SECRET_KEY, user_id=42, expires_in=900, fresh=True
        )
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        assert payload["sub"] == "42"
        assert payload["type"] == "access"
        assert payload["fresh"] is True
        assert "iat" in payload
        assert "exp" in payload
        assert payload["exp"] - payload["iat"] == 900

    def test_create_access_token_non_fresh(self):
        token = _create_api_access_token(
            SECRET_KEY, user_id=1, expires_in=300, fresh=False
        )
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        assert payload["fresh"] is False

    def test_create_refresh_token_structure(self):
        token = _create_api_refresh_token(SECRET_KEY, user_id=42, expires_in=86400)
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        assert payload["sub"] == "42"
        assert payload["type"] == "refresh"
        assert "fresh" not in payload
        assert payload["exp"] - payload["iat"] == 86400

    def test_get_jwt_secret_with_secret_str(self):
        settings = MagicMock()
        settings.secret_key = MagicMock(
            get_secret_value=MagicMock(return_value="my-secret")
        )
        assert _get_jwt_secret(settings) == "my-secret"

    def test_get_jwt_secret_plain_string(self):
        settings = MagicMock(spec=[])
        settings.secret_key = "plain-secret"
        assert _get_jwt_secret(settings) == "plain-secret"


# ---------------------------------------------------------------------------
# Msgspec struct tests
# ---------------------------------------------------------------------------


class TestMsgspecStructs:
    """Test the request/response msgspec schemas."""

    def test_login_request_defaults(self):
        req = LoginRequest(username="admin", password="pass")
        assert req.provider == "db"
        assert req.refresh is True

    def test_login_request_custom(self):
        req = LoginRequest(
            username="admin", password="pass", provider="ldap", refresh=False
        )
        assert req.provider == "ldap"
        assert req.refresh is False

    def test_login_response(self):
        resp = LoginResponse(access_token="abc", refresh_token="def")
        assert resp.access_token == "abc"
        assert resp.refresh_token == "def"

    def test_login_response_no_refresh(self):
        resp = LoginResponse(access_token="abc")
        assert resp.refresh_token == ""

    def test_refresh_response(self):
        resp = RefreshResponse(access_token="xyz")
        assert resp.access_token == "xyz"


# ---------------------------------------------------------------------------
# Login endpoint integration tests
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_user():
    return MockUser(
        id=1,
        username="admin",
        password=_make_password_hash("password123"),
        active=1,
        roles=[MockRole()],
    )


class TestLoginEndpoint:
    """Test POST /api/v1/security/login via Litestar test client."""

    async def test_login_success(self, mock_user: MockUser):
        """Successful login returns access_token and refresh_token."""
        with patch("superset.security.dao.AsyncSecurityDAO") as mock_dao_cls:
            dao_instance = AsyncMock()
            dao_instance.get_user_by_username = AsyncMock(return_value=mock_user)
            mock_dao_cls.return_value = dao_instance

            app = _create_test_app()
            async with AsyncTestClient(app=app) as client:
                resp = await client.post(
                    "/api/v1/security/login",
                    json={
                        "username": "admin",
                        "password": "password123",
                        "provider": "db",
                        "refresh": True,
                    },
                )
                assert resp.status_code == 200
                data = resp.json()
                assert "access_token" in data
                assert "refresh_token" in data

                # Verify the access token payload
                payload = jwt.decode(
                    data["access_token"], SECRET_KEY, algorithms=["HS256"]
                )
                assert payload["sub"] == "1"
                assert payload["type"] == "access"
                assert payload["fresh"] is True

                # Verify the refresh token payload
                r_payload = jwt.decode(
                    data["refresh_token"], SECRET_KEY, algorithms=["HS256"]
                )
                assert r_payload["sub"] == "1"
                assert r_payload["type"] == "refresh"

    async def test_login_no_refresh_token(self, mock_user: MockUser):
        """Login with refresh=False omits refresh_token."""
        with patch("superset.security.dao.AsyncSecurityDAO") as mock_dao_cls:
            dao_instance = AsyncMock()
            dao_instance.get_user_by_username = AsyncMock(return_value=mock_user)
            mock_dao_cls.return_value = dao_instance

            app = _create_test_app()
            async with AsyncTestClient(app=app) as client:
                resp = await client.post(
                    "/api/v1/security/login",
                    json={
                        "username": "admin",
                        "password": "password123",
                        "provider": "db",
                        "refresh": False,
                    },
                )
                assert resp.status_code == 200
                data = resp.json()
                assert "access_token" in data
                assert "refresh_token" not in data

    async def test_login_empty_password_rejected(self, mock_user: MockUser):
        """An empty password is rejected with a validation error before any
        auth is attempted — prevents an LDAP unauthenticated (anonymous) bind
        from logging a caller in as any known user with a blank password.
        Without the guard, db-provider empty password reaches the hash check
        and returns 401 (not a validation error)."""
        with patch("superset.security.dao.AsyncSecurityDAO") as mock_dao_cls:
            dao_instance = AsyncMock()
            dao_instance.get_user_by_username = AsyncMock(return_value=mock_user)
            mock_dao_cls.return_value = dao_instance

            app = _create_test_app()
            async with AsyncTestClient(app=app) as client:
                resp = await client.post(
                    "/api/v1/security/login",
                    json={"username": "admin", "password": "", "provider": "db"},
                )
                assert resp.status_code in (400, 422)
                assert "access_token" not in resp.json()

    async def test_login_wrong_password(self, mock_user: MockUser):
        """Wrong password returns 401."""
        with patch("superset.security.dao.AsyncSecurityDAO") as mock_dao_cls:
            dao_instance = AsyncMock()
            dao_instance.get_user_by_username = AsyncMock(return_value=mock_user)
            mock_dao_cls.return_value = dao_instance

            app = _create_test_app()
            async with AsyncTestClient(app=app) as client:
                resp = await client.post(
                    "/api/v1/security/login",
                    json={
                        "username": "admin",
                        "password": "wrong-password",
                        "provider": "db",
                    },
                )
                assert resp.status_code == 401

    async def test_login_user_not_found(self):
        """Unknown user returns 401."""
        with patch("superset.security.dao.AsyncSecurityDAO") as mock_dao_cls:
            dao_instance = AsyncMock()
            dao_instance.get_user_by_username = AsyncMock(return_value=None)
            dao_instance.get_user_by_email = AsyncMock(return_value=None)
            dao_instance.get_first_user = AsyncMock(return_value=None)
            mock_dao_cls.return_value = dao_instance

            app = _create_test_app()
            async with AsyncTestClient(app=app) as client:
                resp = await client.post(
                    "/api/v1/security/login",
                    json={
                        "username": "nonexistent",
                        "password": "password",
                        "provider": "db",
                    },
                )
                assert resp.status_code == 401

    async def test_login_inactive_user(self, mock_user: MockUser):
        """Inactive user returns 401."""
        mock_user.active = 0
        with patch("superset.security.dao.AsyncSecurityDAO") as mock_dao_cls:
            dao_instance = AsyncMock()
            dao_instance.get_user_by_username = AsyncMock(return_value=mock_user)
            dao_instance.get_user_by_email = AsyncMock(return_value=None)
            dao_instance.get_first_user = AsyncMock(return_value=None)
            mock_dao_cls.return_value = dao_instance

            app = _create_test_app()
            async with AsyncTestClient(app=app) as client:
                resp = await client.post(
                    "/api/v1/security/login",
                    json={
                        "username": "admin",
                        "password": "password123",
                        "provider": "db",
                    },
                )
                assert resp.status_code == 401

    async def test_login_invalid_provider(self):
        """Invalid provider returns 400."""
        app = _create_test_app()
        async with AsyncTestClient(app=app) as client:
            resp = await client.post(
                "/api/v1/security/login",
                json={
                    "username": "admin",
                    "password": "password",
                    "provider": "oauth",
                },
            )
            assert resp.status_code == 400

    async def test_login_provider_not_allowed(self):
        """Wrong provider when multiple providers not allowed returns 400."""
        app = _create_test_app(
            auth_type=1,  # DB
            api_login_allow_multiple_providers=False,
        )
        async with AsyncTestClient(app=app) as client:
            resp = await client.post(
                "/api/v1/security/login",
                json={
                    "username": "admin",
                    "password": "password",
                    "provider": "ldap",
                },
            )
            assert resp.status_code == 400

    async def test_login_ldap_provider_implemented(self):
        """LDAP provider is fully implemented (1:1 FAB port of auth_user_ldap).

        The provider passes validation (NOT a 400 "not implemented") and is
        routed to ``AsyncSecurityManager.auth_user_ldap``. With no reachable
        LDAP server the bind fails and the user is denied with 401 — not a
        "not implemented" 400.
        """
        with patch(
            "superset.security.manager.AsyncSecurityManager.auth_user_ldap",
            new=AsyncMock(return_value=None),
        ):
            app = _create_test_app(
                auth_type=2,  # LDAP
                api_login_allow_multiple_providers=True,
            )
            async with AsyncTestClient(app=app) as client:
                resp = await client.post(
                    "/api/v1/security/login",
                    json={
                        "username": "admin",
                        "password": "password",
                        "provider": "ldap",
                    },
                )
                # Implemented path: auth failure → 401 (not a 400 "not implemented").
                assert resp.status_code == 401

    async def test_login_empty_username(self):
        """Empty username returns 400."""
        app = _create_test_app()
        async with AsyncTestClient(app=app) as client:
            resp = await client.post(
                "/api/v1/security/login",
                json={
                    "username": "",
                    "password": "password",
                    "provider": "db",
                },
            )
            assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Refresh endpoint integration tests
# ---------------------------------------------------------------------------


class TestRefreshEndpoint:
    """Test POST /api/v1/security/refresh via Litestar test client."""

    async def test_refresh_success(self):
        """Valid refresh token returns new access token."""
        refresh_token = _create_api_refresh_token(
            SECRET_KEY, user_id=1, expires_in=86400
        )
        app = _create_test_app()
        async with AsyncTestClient(app=app) as client:
            resp = await client.post(
                "/api/v1/security/refresh",
                headers={"Authorization": f"Bearer {refresh_token}"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert "access_token" in data

            # Verify the new access token is non-fresh
            payload = jwt.decode(data["access_token"], SECRET_KEY, algorithms=["HS256"])
            assert payload["sub"] == "1"
            assert payload["type"] == "access"
            assert payload["fresh"] is False

    async def test_refresh_with_access_token_fails(self):
        """Using an access token for refresh returns 401."""
        access_token = _create_api_access_token(
            SECRET_KEY, user_id=1, expires_in=900, fresh=True
        )
        app = _create_test_app()
        async with AsyncTestClient(app=app) as client:
            resp = await client.post(
                "/api/v1/security/refresh",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            assert resp.status_code == 401

    async def test_refresh_expired_token(self):
        """Expired refresh token returns 401."""
        expired_token = jwt.encode(
            {
                "sub": "1",
                "iat": int(time.time()) - 100,
                "exp": int(time.time()) - 10,
                "type": "refresh",
            },
            SECRET_KEY,
            algorithm="HS256",
        )
        app = _create_test_app()
        async with AsyncTestClient(app=app) as client:
            resp = await client.post(
                "/api/v1/security/refresh",
                headers={"Authorization": f"Bearer {expired_token}"},
            )
            assert resp.status_code == 401

    async def test_refresh_invalid_token(self):
        """Garbage token returns 401."""
        app = _create_test_app()
        async with AsyncTestClient(app=app) as client:
            resp = await client.post(
                "/api/v1/security/refresh",
                headers={"Authorization": "Bearer not-a-real-jwt"},
            )
            assert resp.status_code == 401

    async def test_refresh_no_bearer(self):
        """Missing Bearer prefix returns 401."""
        app = _create_test_app()
        async with AsyncTestClient(app=app) as client:
            resp = await client.post("/api/v1/security/refresh")
            assert resp.status_code == 401

    async def test_refresh_missing_sub_claim(self):
        """Refresh token without sub claim returns 401."""
        bad_token = jwt.encode(
            {
                "iat": int(time.time()),
                "exp": int(time.time()) + 3600,
                "type": "refresh",
            },
            SECRET_KEY,
            algorithm="HS256",
        )
        app = _create_test_app()
        async with AsyncTestClient(app=app) as client:
            resp = await client.post(
                "/api/v1/security/refresh",
                headers={"Authorization": f"Bearer {bad_token}"},
            )
            assert resp.status_code == 401

    async def test_refresh_rejects_blacklisted_token(self):
        """M22 (1/4): a refresh token stolen before logout must not go on
        minting fresh access tokens -- the endpoint must consult
        ``auth:token_blacklist:{user_id}`` like the cookie/access-token
        auth paths do."""
        issued_at = int(time.time()) - 100
        refresh_token = jwt.encode(
            {
                "sub": "1",
                "iat": issued_at,
                "exp": int(time.time()) + 86400,
                "type": "refresh",
            },
            SECRET_KEY,
            algorithm="HS256",
        )
        mock_redis = AsyncMock()
        # Logout happened after the token was issued.
        mock_redis.get = AsyncMock(return_value=str(issued_at + 50))

        app = _create_test_app(redis=mock_redis)
        async with AsyncTestClient(app=app) as client:
            resp = await client.post(
                "/api/v1/security/refresh",
                headers={"Authorization": f"Bearer {refresh_token}"},
            )
            assert resp.status_code == 401

    async def test_refresh_accepts_token_issued_after_logout(self):
        """A refresh token minted after the blacklist timestamp (a fresh
        login post-logout) must still work."""
        issued_at = int(time.time())
        refresh_token = jwt.encode(
            {
                "sub": "1",
                "iat": issued_at,
                "exp": issued_at + 86400,
                "type": "refresh",
            },
            SECRET_KEY,
            algorithm="HS256",
        )
        mock_redis = AsyncMock()
        # Blacklist entry predates this token's iat.
        mock_redis.get = AsyncMock(return_value=str(issued_at - 100))

        app = _create_test_app(redis=mock_redis)
        async with AsyncTestClient(app=app) as client:
            resp = await client.post(
                "/api/v1/security/refresh",
                headers={"Authorization": f"Bearer {refresh_token}"},
            )
            assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Middleware: access token authentication tests
# ---------------------------------------------------------------------------


@get("/api-protected")
async def api_protected_route(request: Request) -> dict:
    user = request.user
    # The anonymous user (UnauthenticatedUser) has ``username == ""`` and
    # ``is_authenticated == False``; report it as "anon" for the assertions.
    if not getattr(user, "is_authenticated", False):
        username = "anon"
    else:
        username = getattr(user, "username", "anon")
    return {
        "user_id": getattr(user, "id", None),
        "username": username,
        "auth": request.auth,
    }


class TestAccessTokenMiddleware:
    """Test that SupersetAuthMiddleware authenticates API access tokens."""

    async def test_access_token_authenticates_user(self):
        """Valid access token resolves user from DB via middleware."""
        access_token = _create_api_access_token(
            SECRET_KEY, user_id=42, expires_in=900, fresh=True
        )

        cached_user = CachedUser(
            id=42,
            username="testuser",
            email="test@test.com",
            active=1,
            roles=[],
            permissions=set(),
        )

        with patch.object(
            SupersetAuthMiddleware,
            "_resolve_user_from_db",
            return_value=cached_user,
        ):
            app = _create_middleware_test_app()
            async with AsyncTestClient(app=app) as client:
                resp = await client.get(
                    "/api-protected",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                assert resp.status_code == 200
                data = resp.json()
                assert data["username"] == "testuser"
                assert data["user_id"] == 42
                assert data["auth"] == "jwt"

    async def test_expired_access_token_returns_anonymous(self):
        """Expired access token falls through to anonymous."""
        expired_token = jwt.encode(
            {
                "sub": "1",
                "iat": int(time.time()) - 1000,
                "exp": int(time.time()) - 100,
                "type": "access",
                "fresh": True,
            },
            SECRET_KEY,
            algorithm="HS256",
        )

        app = _create_middleware_test_app()
        async with AsyncTestClient(app=app) as client:
            resp = await client.get(
                "/api-protected",
                headers={"Authorization": f"Bearer {expired_token}"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["username"] == "anon"

    async def test_refresh_token_not_accepted_as_access(self):
        """Refresh tokens must not be used for API access."""
        refresh_token = _create_api_refresh_token(
            SECRET_KEY, user_id=1, expires_in=86400
        )

        app = _create_middleware_test_app()
        async with AsyncTestClient(app=app) as client:
            resp = await client.get(
                "/api-protected",
                headers={"Authorization": f"Bearer {refresh_token}"},
            )
            assert resp.status_code == 200
            data = resp.json()
            # Refresh token has type="refresh", not "access",
            # so _resolve_user_from_access_token returns None.
            # Without embedded_superset, guest token also returns None.
            assert data["username"] == "anon"

    async def test_guest_token_still_works_with_embedded_flag(self):
        """Guest tokens (type=guest) still work when embedded_superset is on."""
        from superset.security.guest import create_guest_access_token

        guest_token = create_guest_access_token(
            secret_key=SECRET_KEY,
            user={"username": "embed-user"},
            resources=[{"type": "dashboard", "id": "abc"}],
            rls=[],
            exp_seconds=3600,
        )

        app = _create_middleware_test_app(embedded_superset=True)
        async with AsyncTestClient(app=app) as client:
            # Guest tokens are read from the dedicated X-GuestToken header,
            # NOT the Authorization: Bearer header (which carries API access tokens).
            resp = await client.get(
                "/api-protected",
                headers={"X-GuestToken": guest_token},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["username"] == "embed-user"
            # Guest-token auth sets the auth scope to "guest_token"
            # (API access tokens use "jwt").
            assert data["auth"] == "guest_token"

    async def test_access_token_uses_redis_cache(self):
        """Access token auth uses Redis cache when available."""
        import json

        access_token = _create_api_access_token(
            SECRET_KEY, user_id=10, expires_in=900, fresh=True
        )

        from superset.security.auth_cache import sign_keyed_payload

        body = json.dumps(
            {
                "id": 10,
                "username": "cached-user",
                "email": "cached@test.com",
                "active": 1,
                "roles": [{"id": 1, "name": "Admin"}],
                "permissions": ["can_read_Chart"],
            }
        )
        # Entries are HMAC-signed over both the payload and the Redis key
        # they are stored under: Redis write access alone must not be
        # enough to hand a request a set of permissions, or to replay
        # another user's entry under this key.
        cache_key = "auth:user:10"
        cached_data = json.dumps(
            {"sig": sign_keyed_payload(cache_key, body, SECRET_KEY), "data": body}
        )
        mock_redis = AsyncMock()
        # ``_get_cached_user`` fetches the entry and the cache epoch in one MGET.
        mock_redis.mget = AsyncMock(return_value=[cached_data, None])

        app = _create_middleware_test_app(redis=mock_redis)
        async with AsyncTestClient(app=app) as client:
            resp = await client.get(
                "/api-protected",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["username"] == "cached-user"
            assert data["user_id"] == 10


# ---------------------------------------------------------------------------
# Test app factories
# ---------------------------------------------------------------------------


def _create_test_app(redis: Any = None, **settings_overrides: Any) -> Litestar:
    """Create a Litestar app with SecurityController for testing."""
    settings = _make_settings(**settings_overrides)
    mock_factory = _make_mock_session_factory()

    return Litestar(
        route_handlers=[SecurityController],
        state=State(
            {
                "settings": settings,
                "session_factory": mock_factory,
                "redis": redis,
            }
        ),
    )


def _create_middleware_test_app(
    embedded_superset: bool = False,
    redis: Any = None,
) -> Litestar:
    """Create a Litestar app with auth middleware for testing."""
    settings = _make_settings(embedded_superset=embedded_superset)
    mock_factory = _make_mock_session_factory()

    return Litestar(
        route_handlers=[api_protected_route],
        middleware=[SupersetAuthMiddleware],
        state=State(
            {
                "settings": settings,
                "session_factory": mock_factory,
                "redis": redis,
            }
        ),
    )
