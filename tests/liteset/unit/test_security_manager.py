"""Tests for AsyncSecurityManager — async security checks."""
from __future__ import annotations

import pytest
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock

from liteset.security.manager import AsyncSecurityManager


@dataclass
class MockUser:
    id: int = 1
    username: str = "admin"
    email: str = "admin@test.com"
    is_active: bool = True
    roles: list = field(default_factory=list)


@dataclass
class MockRole:
    id: int = 1
    name: str = "Admin"


@dataclass
class MockGammaRole:
    id: int = 2
    name: str = "Gamma"


@dataclass
class MockPublicRole:
    id: int = 3
    name: str = "Public"


@pytest.fixture
def mock_dao():
    dao = AsyncMock()
    return dao


@pytest.fixture
def manager(mock_dao):
    return AsyncSecurityManager(dao=mock_dao, admin_role_name="Admin")


async def test_is_admin_true(manager, mock_dao):
    admin_user = MockUser(roles=[MockRole()])
    assert manager.is_admin(admin_user) is True


async def test_is_admin_false(manager, mock_dao):
    gamma_user = MockUser(roles=[MockGammaRole()])
    assert manager.is_admin(gamma_user) is False


async def test_has_access_admin_bypass(manager, mock_dao):
    admin_user = MockUser(roles=[MockRole()])
    result = await manager.has_access("can_read", "Chart", user=admin_user)
    assert result is True
    # Admin bypass — DAO should NOT be called
    mock_dao.has_permission_view.assert_not_called()


async def test_has_access_non_admin_checks_dao(manager, mock_dao):
    gamma_user = MockUser(roles=[MockGammaRole()])
    mock_dao.has_permission_view.return_value = True
    result = await manager.has_access("can_read", "Chart", user=gamma_user)
    assert result is True
    mock_dao.has_permission_view.assert_called_once()


async def test_has_access_denied(manager, mock_dao):
    gamma_user = MockUser(roles=[MockGammaRole()])
    mock_dao.has_permission_view.return_value = False
    result = await manager.has_access("can_write", "Chart", user=gamma_user)
    assert result is False


async def test_can_access_alias(manager, mock_dao):
    admin_user = MockUser(roles=[MockRole()])
    result = await manager.can_access("can_read", "Chart", user=admin_user)
    assert result is True


async def test_get_user_roles(manager, mock_dao):
    roles = [MockRole(), MockGammaRole()]
    user = MockUser(roles=roles)
    mock_dao.get_user_roles.return_value = roles
    result = await manager.get_user_roles(user)
    assert len(result) == 2


async def test_is_owner_true(manager):
    user = MockUser(id=5)
    resource = MagicMock()
    resource.owners = [MagicMock(id=5), MagicMock(id=10)]
    assert manager.is_owner(resource, user) is True


async def test_is_owner_false(manager):
    user = MockUser(id=99)
    resource = MagicMock()
    resource.owners = [MagicMock(id=5)]
    assert manager.is_owner(resource, user) is False


async def test_is_owner_created_by(manager):
    user = MockUser(id=5)
    resource = MagicMock()
    resource.owners = []
    resource.created_by_fk = 5
    assert manager.is_owner(resource, user) is True


async def test_raise_for_access_admin_passes(manager, mock_dao):
    admin_user = MockUser(roles=[MockRole()])
    # Should not raise for admin
    await manager.raise_for_access(user=admin_user, database=MagicMock())


async def test_raise_for_access_denied(manager, mock_dao):
    from liteset.exceptions import LitesetSecurityException

    gamma_user = MockUser(roles=[MockGammaRole()])
    mock_dao.has_permission_view.return_value = False

    database = MagicMock()
    database.perm = "[db].(id:1)"

    with pytest.raises(LitesetSecurityException):
        await manager.raise_for_access(user=gamma_user, database=database)


async def test_can_access_database(manager, mock_dao):
    admin_user = MockUser(roles=[MockRole()])
    database = MagicMock()
    result = await manager.can_access_database(database, user=admin_user)
    assert result is True


async def test_get_schemas_accessible_by_user_admin(manager, mock_dao):
    admin_user = MockUser(roles=[MockRole()])
    database = MagicMock()
    schemas = ["public", "private", "secret"]
    result = await manager.get_schemas_accessible_by_user(
        database, schemas, user=admin_user
    )
    assert result == schemas  # Admin gets all


async def test_get_schemas_accessible_by_user_filtered(manager, mock_dao):
    gamma_user = MockUser(roles=[MockGammaRole()])
    database = MagicMock()
    database.perm = "[db].(id:1)"
    database.database_name = "db"

    mock_dao.has_permission_view.return_value = False
    mock_dao.get_all_permissions_for_user.return_value = {
        ("schema_access", "[db].[public]"),
    }

    schemas = ["public", "private"]
    result = await manager.get_schemas_accessible_by_user(
        database, schemas, user=gamma_user
    )
    assert "public" in result
    assert "private" not in result


async def test_invalidate_user_cache(manager):
    mock_redis = AsyncMock()
    user = MockUser(id=1, username="admin", email="admin@test.com")
    await manager.invalidate_user_cache(mock_redis, user)
    mock_redis.delete.assert_called_once_with(
        "auth:user:1",
        "auth:user:admin",
        "auth:user:admin@test.com",
    )


async def test_raise_for_ownership_admin_passes(manager):
    admin_user = MockUser(roles=[MockRole()])
    resource = MagicMock()
    # Should not raise for admin
    manager.raise_for_ownership(resource, user=admin_user)


async def test_raise_for_ownership_owner_passes(manager):
    user = MockUser(id=5, roles=[MockGammaRole()])
    resource = MagicMock()
    resource.owners = [MagicMock(id=5)]
    manager.raise_for_ownership(resource, user=user)


async def test_raise_for_ownership_denied(manager):
    from liteset.exceptions import LitesetSecurityException

    user = MockUser(id=99, roles=[MockGammaRole()])
    resource = MagicMock()
    resource.owners = [MagicMock(id=5)]
    resource.created_by_fk = None
    with pytest.raises(LitesetSecurityException):
        manager.raise_for_ownership(resource, user=user)


async def test_can_access_chart_admin(manager, mock_dao):
    admin_user = MockUser(roles=[MockRole()])
    chart = MagicMock()
    result = await manager.can_access_chart(chart, user=admin_user)
    assert result is True


async def test_can_access_chart_owner(manager, mock_dao):
    user = MockUser(id=5, roles=[MockGammaRole()])
    chart = MagicMock()
    chart.owners = [MagicMock(id=5)]
    result = await manager.can_access_chart(chart, user=user)
    assert result is True


async def test_can_access_chart_denied(manager, mock_dao):
    user = MockUser(id=99, roles=[MockGammaRole()])
    chart = MagicMock()
    chart.owners = []
    chart.created_by_fk = None
    mock_dao.has_permission_view.return_value = False
    result = await manager.can_access_chart(chart, user=user)
    assert result is False


# --- Guest user + raise_for_access tests ---

@dataclass
class MockGuestUser:
    id: int = 0
    username: str = "guest"
    is_guest: bool = True
    roles: list = field(default_factory=list)
    resources: list = field(default_factory=list)


async def test_raise_for_access_guest_database_denied(manager, mock_dao):
    """Guest user accessing database should be denied."""
    from liteset.exceptions import LitesetSecurityException

    guest = MockGuestUser()
    with pytest.raises(LitesetSecurityException, match="Guest users"):
        await manager.raise_for_access(user=guest, database=MagicMock())


async def test_raise_for_access_guest_datasource_denied(manager, mock_dao):
    """Guest user accessing datasource should be denied."""
    from liteset.exceptions import LitesetSecurityException

    guest = MockGuestUser()
    with pytest.raises(LitesetSecurityException, match="Guest users"):
        await manager.raise_for_access(user=guest, datasource=MagicMock())


async def test_raise_for_access_guest_query_denied(manager, mock_dao):
    """Guest user accessing query should be denied."""
    from liteset.exceptions import LitesetSecurityException

    guest = MockGuestUser()
    with pytest.raises(LitesetSecurityException, match="Guest users"):
        await manager.raise_for_access(user=guest, query=MagicMock())


# --- Guest token manager methods ---

def test_create_guest_access_token_via_manager():
    token = AsyncSecurityManager.create_guest_access_token(
        secret_key="test-secret-key-at-least-16-chars",
        user={"username": "embed"},
        resources=[{"type": "dashboard", "id": "abc"}],
        rls=[],
    )
    assert isinstance(token, str)
    assert len(token) > 0


def test_parse_jwt_guest_token_via_manager():
    secret = "test-secret-key-at-least-16-chars"
    token = AsyncSecurityManager.create_guest_access_token(
        secret_key=secret,
        user={"username": "embed"},
        resources=[{"type": "dashboard", "id": "abc"}],
        rls=[],
    )
    payload = AsyncSecurityManager.parse_jwt_guest_token(token, secret)
    assert payload is not None
    assert payload["user"]["username"] == "embed"


def test_get_guest_user_from_request(manager):
    request = MagicMock()
    request.user = MockGuestUser()
    result = manager.get_guest_user_from_request(request)
    assert result is not None
    assert result.is_guest is True


def test_get_guest_user_from_request_not_guest(manager):
    request = MagicMock()
    request.user = MockUser()
    result = manager.get_guest_user_from_request(request)
    assert result is None
