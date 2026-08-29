from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from itsdangerous import URLSafeTimedSerializer
from litestar import get, Litestar
from litestar.connection import Request
from litestar.datastructures import State
from litestar.testing import AsyncTestClient
from sqlalchemy import Column, ForeignKey, insert, Integer, String, Table
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase, relationship

from superset.controllers.security import SecurityController
from superset.middleware.auth import SupersetAuthMiddleware
from superset.security.dao import AsyncSecurityDAO
from superset.security.guest import create_guest_access_token, GuestUser

SECRET_KEY = "integration-test-secret-key-32chr"


def _signed_cache_entry(payload: dict, cache_key: str | None = None) -> str:
    """Build a Redis auth-cache envelope the middleware will accept.

    Entries are HMAC-signed over both the payload *and* the Redis key they
    are stored under, so that Redis write access alone cannot grant
    permissions, and a legitimately-signed entry cannot be copied onto a
    different user's cache key.  Defaults *cache_key* to
    ``auth:user:{payload["id"]}`` to match how ``_cache_user`` writes real
    entries.
    """
    from superset.security.auth_cache import sign_keyed_payload

    body = json.dumps(payload)
    key = cache_key if cache_key is not None else f"auth:user:{payload.get('id')}"
    return json.dumps({"sig": sign_keyed_payload(key, body, SECRET_KEY), "data": body})


# --- Minimal FAB schema (prefixed to avoid pytest collection) ---


class Base(DeclarativeBase):
    pass


_ab_user_role = Table(
    "ab_user_role",
    Base.metadata,
    Column("id", Integer, primary_key=True),
    Column("user_id", Integer, ForeignKey("ab_user.id")),
    Column("role_id", Integer, ForeignKey("ab_role.id")),
)

_ab_permission_view_role = Table(
    "ab_permission_view_role",
    Base.metadata,
    Column("id", Integer, primary_key=True),
    Column("permission_view_id", Integer, ForeignKey("ab_permission_view.id")),
    Column("role_id", Integer, ForeignKey("ab_role.id")),
)


class FabUser(Base):
    __tablename__ = "ab_user"
    id = Column(Integer, primary_key=True)
    username = Column(String(256), unique=True, nullable=False)
    email = Column(String(256), unique=True, nullable=False)
    active = Column(Integer, default=1)
    is_authenticated: bool = True
    roles = relationship("FabRole", secondary=_ab_user_role, backref="users")


class FabRole(Base):
    __tablename__ = "ab_role"
    id = Column(Integer, primary_key=True)
    name = Column(String(256), unique=True, nullable=False)
    permissions = relationship(
        "FabPermissionView",
        secondary=_ab_permission_view_role,
        # Mirror the production model: FAB's backref is the singular
        # ``role`` (plural fakes masked a real AttributeError in the DAO).
        backref="role",
    )


class FabPermission(Base):
    __tablename__ = "ab_permission"
    id = Column(Integer, primary_key=True)
    name = Column(String(256), unique=True, nullable=False)


class FabViewMenu(Base):
    __tablename__ = "ab_view_menu"
    id = Column(Integer, primary_key=True)
    name = Column(String(256), unique=True, nullable=False)


class FabPermissionView(Base):
    __tablename__ = "ab_permission_view"
    id = Column(Integer, primary_key=True)
    permission_id = Column(Integer, ForeignKey("ab_permission.id"))
    view_menu_id = Column(Integer, ForeignKey("ab_view_menu.id"))
    permission = relationship("FabPermission")
    view_menu = relationship("FabViewMenu")


@dataclass
class MockRole:
    id: int = 1
    name: str = "Admin"


@dataclass
class MockUser:
    id: int = 1
    username: str = "admin"
    email: str = "admin@test.com"
    is_authenticated: bool = True
    is_active: bool = True
    is_guest: bool = False
    roles: list = field(default_factory=list)


@dataclass
class MockDashboard:
    id: int = 1
    dashboard_title: str = "Test Dashboard"
    published: bool = True
    roles: list = field(default_factory=list)
    owners: list = field(default_factory=list)
    created_by_fk: int | None = None


@pytest.fixture
async def db_engine():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with engine.begin() as conn:
        await conn.execute(
            insert(FabRole).values(
                [
                    {"id": 1, "name": "Admin"},
                    {"id": 2, "name": "Gamma"},
                ]
            )
        )
        await conn.execute(
            insert(FabUser).values(
                [
                    {
                        "id": 1,
                        "username": "admin",
                        "email": "admin@test.com",
                        "active": 1,
                    },
                    {
                        "id": 2,
                        "username": "gamma",
                        "email": "gamma@test.com",
                        "active": 1,
                    },
                    {
                        "id": 3,
                        "username": "inactive",
                        "email": "inactive@test.com",
                        "active": 0,
                    },
                ]
            )
        )
        await conn.execute(
            insert(FabPermission).values(
                [
                    {"id": 1, "name": "can_read"},
                ]
            )
        )
        await conn.execute(
            insert(FabViewMenu).values(
                [
                    {"id": 1, "name": "Chart"},
                ]
            )
        )
        await conn.execute(
            insert(FabPermissionView).values(
                [
                    {"id": 1, "permission_id": 1, "view_menu_id": 1},
                ]
            )
        )
        await conn.execute(
            insert(_ab_user_role).values(
                [
                    {"id": 1, "user_id": 1, "role_id": 1},
                    {"id": 2, "user_id": 2, "role_id": 2},
                ]
            )
        )
        await conn.execute(
            insert(_ab_permission_view_role).values(
                [
                    {"id": 1, "permission_view_id": 1, "role_id": 2},
                ]
            )
        )

    yield engine
    await engine.dispose()


def _make_cookie(user_id: int) -> str:
    # Flask's exact signer configuration (hmac key-derivation + SHA-1) —
    # the decoder no longer accepts itsdangerous' django-concat default.
    s = URLSafeTimedSerializer(
        SECRET_KEY,
        salt="cookie-session",
        signer_kwargs={"key_derivation": "hmac", "digest_method": hashlib.sha1},
    )
    return s.dumps({"_user_id": str(user_id)})


@get("/api/v1/test/whoami")
async def whoami(request: Request[Any, Any, Any]) -> dict[str, Any]:
    user = request.user
    return {
        "id": getattr(user, "id", None),
        "username": getattr(user, "username", None),
        "is_guest": getattr(user, "is_guest", False),
    }


async def test_cookie_auth_full_flow(db_engine):
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async def mock_resolve(self, connection, user_id):
        async with session_factory() as sess:
            dao = AsyncSecurityDAO(
                sess,
                user_model=FabUser,
                role_model=FabRole,
                permission_model=FabPermission,
                view_menu_model=FabViewMenu,
                permission_view_model=FabPermissionView,
            )
            user = await dao.get_user_by_id(user_id)
            if user and getattr(user, "active", True):
                return user
        return None

    with patch.object(SupersetAuthMiddleware, "_resolve_user_from_db", mock_resolve):
        app = Litestar(
            route_handlers=[whoami],
            middleware=[SupersetAuthMiddleware],
            state=State(
                {
                    "settings": MagicMock(
                        secret_key=MagicMock(
                            get_secret_value=MagicMock(return_value=SECRET_KEY)
                        ),
                        session_cookie_name="session",
                        session_max_age=86400,
                        embedded_superset=False,
                        feature_flags={},
                        guest_token_header_name="X-GuestToken",
                    ),
                    "session_factory": session_factory,
                    "redis": None,
                }
            ),
        )

        async with AsyncTestClient(app=app) as client:
            cookie = _make_cookie(1)
            resp = await client.get(
                "/api/v1/test/whoami",
                cookies={"session": cookie},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["username"] == "admin"


async def test_inactive_user_rejected(db_engine):
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async def mock_resolve(self, connection, user_id):
        async with session_factory() as sess:
            dao = AsyncSecurityDAO(
                sess,
                user_model=FabUser,
                role_model=FabRole,
                permission_model=FabPermission,
                view_menu_model=FabViewMenu,
                permission_view_model=FabPermissionView,
            )
            user = await dao.get_user_by_id(user_id)
            if user and getattr(user, "active", True):
                return user
        return None

    with patch.object(SupersetAuthMiddleware, "_resolve_user_from_db", mock_resolve):
        app = Litestar(
            route_handlers=[whoami],
            middleware=[SupersetAuthMiddleware],
            state=State(
                {
                    "settings": MagicMock(
                        secret_key=MagicMock(
                            get_secret_value=MagicMock(return_value=SECRET_KEY)
                        ),
                        session_cookie_name="session",
                        session_max_age=86400,
                        embedded_superset=False,
                        feature_flags={},
                        guest_token_header_name="X-GuestToken",
                    ),
                    "session_factory": session_factory,
                    "redis": None,
                }
            ),
        )

        async with AsyncTestClient(app=app) as client:
            cookie = _make_cookie(3)  # inactive user
            resp = await client.get(
                "/api/v1/test/whoami",
                cookies={"session": cookie},
            )
            # Inactive user → _resolve_user_from_db returns None → middleware
            # falls through to UnauthenticatedUser (anonymous). The whoami
            # endpoint has no RBAC guard so it still returns 200, but the
            # resolved user must be anonymous: the anonymous user serializes
            # with an empty username (id=0), NOT the inactive user's name.
            assert resp.status_code == 200
            data = resp.json()
            assert not data.get("username"), (
                "Inactive user must not be resolved; expected anonymous user "
                f"but got username={data.get('username')!r}"
            )
            assert data.get("username") != "inactive"


async def test_csrf_token_endpoint():
    """CSRF token endpoint requires ``can_read on SecurityRestApi``.

    The PVM is part of ``_STANDARD_VIEW_PERMISSIONS`` (sync_roles.py:405) so every
    standard role carries it. Supply an authenticated user holding the PVM via
    middleware so the request reaches the handler and returns a token.
    """
    from litestar.middleware import ASGIMiddleware

    class _InjectAuthedUser(ASGIMiddleware):
        async def handle(self, scope, receive, send, next_app):
            if scope["type"] in ("http", "websocket"):
                user = MagicMock()
                user.is_authenticated = True
                user.id = 1
                user.permissions = {("can_read", "SecurityRestApi")}
                scope["user"] = user
                scope["auth"] = "mock"
            await next_app(scope, receive, send)

    app = Litestar(
        route_handlers=[SecurityController],
        middleware=[_InjectAuthedUser()],
        state=State(
            {
                "settings": MagicMock(
                    secret_key=MagicMock(
                        get_secret_value=MagicMock(return_value=SECRET_KEY)
                    ),
                    session_cookie_name="session",
                ),
            }
        ),
    )
    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/api/v1/security/csrf_token/")
        assert resp.status_code == 200
        data = resp.json()
        assert "result" in data
        assert isinstance(data["result"], str)


async def test_csrf_token_endpoint_requires_auth():
    from litestar.middleware import ASGIMiddleware

    class _InjectAnonUser(ASGIMiddleware):
        async def handle(self, scope, receive, send, next_app):
            if scope["type"] in ("http", "websocket"):
                user = MagicMock()
                user.is_authenticated = False
                user.permissions = set()
                scope["user"] = user
                scope["auth"] = None
            await next_app(scope, receive, send)

    app = Litestar(
        route_handlers=[SecurityController],
        middleware=[_InjectAnonUser()],
    )
    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/api/v1/security/csrf_token/")
        assert resp.status_code == 401


async def test_guest_token_flow():
    create_guest_access_token(
        secret_key=SECRET_KEY,
        user={"username": "embed_user", "first_name": "Embed", "last_name": "User"},
        resources=[{"type": "dashboard", "id": "dash-uuid-123"}],
        rls=[{"clause": "org_id = 42"}],
    )
    guest = GuestUser.from_token_payload(
        {
            "user": {
                "username": "embed_user",
                "first_name": "Embed",
                "last_name": "User",
            },
            "resources": [{"type": "dashboard", "id": "dash-uuid-123"}],
            "rls_rules": [{"clause": "org_id = 42"}],
        }
    )
    assert guest.is_guest is True
    assert guest.username == "embed_user"
    assert len(guest.resources) == 1
    assert guest.rls_rules[0]["clause"] == "org_id = 42"


async def test_redis_cache_hit():
    mock_redis = AsyncMock()
    cached_user_data = _signed_cache_entry(
        {
            "id": 1,
            "username": "cached_admin",
            "email": "admin@test.com",
            "active": 1,
            "is_authenticated": True,
            "roles": [{"id": 1, "name": "Admin"}],
        }
    )
    # ``_get_cached_user`` fetches the entry and the cache epoch in one MGET.
    mock_redis.mget = AsyncMock(return_value=[cached_user_data, None])
    # Legacy-cookie blacklist check: no entry present -> None.
    mock_redis.get = AsyncMock(return_value=None)

    app = Litestar(
        route_handlers=[whoami],
        middleware=[SupersetAuthMiddleware],
        state=State(
            {
                "settings": MagicMock(
                    secret_key=MagicMock(
                        get_secret_value=MagicMock(return_value=SECRET_KEY)
                    ),
                    session_cookie_name="session",
                    session_max_age=86400,
                    embedded_superset=False,
                    feature_flags={},
                    guest_token_header_name="X-GuestToken",
                ),
                "session_factory": MagicMock(),
                "redis": mock_redis,
            }
        ),
    )

    async with AsyncTestClient(app=app) as client:
        cookie = _make_cookie(1)
        resp = await client.get(
            "/api/v1/test/whoami",
            cookies={"session": cookie},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["username"] == "cached_admin"
        mock_redis.mget.assert_called_once()


async def test_redis_cache_rejects_inactive():
    mock_redis = AsyncMock()
    cached_user_data = _signed_cache_entry(
        {
            "id": 3,
            "username": "inactive",
            "email": "x@x.com",
            "active": 0,
            "roles": [],
        }
    )
    # ``_get_cached_user`` fetches the entry and the cache epoch in one MGET.
    mock_redis.mget = AsyncMock(return_value=[cached_user_data, None])
    # Legacy-cookie blacklist check: no entry present -> None.
    mock_redis.get = AsyncMock(return_value=None)

    app = Litestar(
        route_handlers=[whoami],
        middleware=[SupersetAuthMiddleware],
        state=State(
            {
                "settings": MagicMock(
                    secret_key=MagicMock(
                        get_secret_value=MagicMock(return_value=SECRET_KEY)
                    ),
                    session_cookie_name="session",
                    session_max_age=86400,
                    embedded_superset=False,
                    feature_flags={},
                    guest_token_header_name="X-GuestToken",
                ),
                "session_factory": MagicMock(),
                "redis": mock_redis,
            }
        ),
    )

    with patch.object(
        SupersetAuthMiddleware,
        "_resolve_user_from_db",
        return_value=None,
    ):
        async with AsyncTestClient(app=app) as client:
            cookie = _make_cookie(3)
            resp = await client.get(
                "/api/v1/test/whoami",
                cookies={"session": cookie},
            )
            # Inactive cached user → falls through to anonymous user
            assert resp.status_code == 200
            assert resp.json().get("username", "") != "inactive"


async def test_redis_cache_invalidation():
    from superset.security.manager import AsyncSecurityManager

    mock_redis = AsyncMock()
    mock_dao = AsyncMock()
    manager = AsyncSecurityManager(dao=mock_dao)

    user = MockUser()
    await manager.invalidate_user_cache(mock_redis, user)

    mock_redis.delete.assert_called_once_with(
        "auth:user:1",
        "auth:user:admin",
        "auth:user:admin@test.com",
    )


async def test_anonymous_public_user_fallback():
    from superset.security.manager import AsyncSecurityManager

    mock_dao = AsyncMock()
    manager = AsyncSecurityManager(dao=mock_dao, public_role_name="Public")

    anon = manager.get_anonymous_user()
    assert anon.is_authenticated is False


async def test_group_based_access(db_engine):
    async with AsyncSession(db_engine, expire_on_commit=False) as sess:
        dao = AsyncSecurityDAO(
            sess,
            user_model=FabUser,
            role_model=FabRole,
            permission_model=FabPermission,
            view_menu_model=FabViewMenu,
            permission_view_model=FabPermissionView,
        )
        assert hasattr(dao, "get_all_permissions_for_user_with_groups")
        assert hasattr(dao, "get_user_groups")
        assert hasattr(dao, "get_group_roles")
        assert hasattr(dao, "get_group_permissions")

        # group tables don't exist in test DB, but direct perms should work
        perms = await dao.get_all_permissions_for_user(2)
        assert ("can_read", "Chart") in perms


async def test_dashboard_rbac_enabled():
    from superset.security.manager import AsyncSecurityManager

    mock_dao = AsyncMock()
    manager = AsyncSecurityManager(
        dao=mock_dao,
        admin_role_name="Admin",
        dashboard_rbac_enabled=True,
    )

    user_role = MockRole(id=2, name="Gamma")
    dash_role = MockRole(id=2, name="Gamma")
    user = MockUser(id=5, roles=[user_role])
    dashboard = MockDashboard(roles=[dash_role])

    result = await manager.can_access_dashboard(dashboard, user=user)
    assert result is True


async def test_dashboard_rbac_enabled_no_match():
    from superset.security.manager import AsyncSecurityManager

    mock_dao = AsyncMock()
    manager = AsyncSecurityManager(
        dao=mock_dao,
        admin_role_name="Admin",
        dashboard_rbac_enabled=True,
    )

    user_role = MockRole(id=2, name="Gamma")
    dash_role = MockRole(id=3, name="Alpha")
    user = MockUser(id=5, roles=[user_role])
    dashboard = MockDashboard(roles=[dash_role], owners=[], created_by_fk=None)

    result = await manager.can_access_dashboard(dashboard, user=user)
    assert result is False


async def test_dashboard_rbac_disabled():
    from superset.security.manager import AsyncSecurityManager

    mock_dao = AsyncMock()
    mock_dao.has_permission_view.return_value = True
    manager = AsyncSecurityManager(
        dao=mock_dao,
        admin_role_name="Admin",
        dashboard_rbac_enabled=False,
    )

    user_role = MockRole(id=2, name="Gamma")
    user = MockUser(id=5, roles=[user_role])
    dashboard = MockDashboard(roles=[], published=True, owners=[], created_by_fk=None)

    result = await manager.can_access_dashboard(dashboard, user=user)
    assert result is True


async def test_catalog_access_hierarchy():
    from superset.security.manager import AsyncSecurityManager

    mock_dao = AsyncMock()
    mock_dao.has_permission_view.return_value = False

    manager = AsyncSecurityManager(dao=mock_dao, admin_role_name="Admin")

    user = MockUser(id=5, roles=[MockRole(id=2, name="Gamma")])
    database = MagicMock()
    database.database_name = "mydb"
    database.perm = "[mydb].(id:1)"

    result = await manager.can_access_catalog(database, "my_catalog", user=user)
    assert isinstance(result, bool)


async def test_guest_token_with_invalid_resources():
    from superset.security.guest import validate_guest_token_resources_schema

    errors = validate_guest_token_resources_schema(
        [
            {"type": "dashboard", "id": "valid-uuid"},
            {"type": "invalid_type", "id": "some-id"},
            {"id": "missing-type"},
            {"type": "dashboard"},
        ]
    )
    assert len(errors) == 3


async def test_user_with_no_roles():
    from superset.security.manager import AsyncSecurityManager

    mock_dao = AsyncMock()
    manager = AsyncSecurityManager(dao=mock_dao, admin_role_name="Admin")

    user = MockUser(id=5, roles=[])

    result = await manager.has_access("can_read", "Chart", user=user)
    assert result is False
    mock_dao.has_permission_view.assert_not_called()


async def test_can_access_all_databases_admin():
    from superset.security.manager import AsyncSecurityManager

    mock_dao = AsyncMock()
    manager = AsyncSecurityManager(dao=mock_dao, admin_role_name="Admin")

    admin_user = MockUser(id=1, roles=[MockRole(id=1, name="Admin")])

    # Admin passes through its seeded ``all_database_access`` permission, not
    # through its role name — and revoking that permission denies.
    mock_dao.has_permission_view.return_value = True
    assert await manager.can_access_all_databases(user=admin_user) is True

    mock_dao.has_permission_view.return_value = False
    assert await manager.can_access_all_databases(user=admin_user) is False
