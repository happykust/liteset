"""Tests for AsyncSecurityManager — async security checks."""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock

import pytest

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
    mock_dao.has_permission_view.assert_called_once_with(
        "can_read", "Chart", role_ids=[2]
    )


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
    """T3-9: is_owner checks owners M2M only, not created_by_fk."""
    user = MockUser(id=5)
    resource = MagicMock()
    resource.owners = []
    resource.created_by_fk = 5
    # created_by_fk is no longer checked — only owners M2M
    assert manager.is_owner(resource, user) is False
    # With user in owners list, it should return True
    resource.owners = [MockUser(id=5)]
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


async def test_raise_for_ownership_admin_passes(manager, mock_dao):
    admin_user = MockUser(roles=[MockRole()])
    mock_dao.get_user_by_id.return_value = admin_user
    resource = MagicMock()
    # Should not raise for admin
    await manager.raise_for_ownership(resource, admin_user.id)


async def test_raise_for_ownership_owner_passes(manager, mock_dao):
    user = MockUser(id=5, roles=[MockGammaRole()])
    mock_dao.get_user_by_id.return_value = user
    resource = MagicMock()
    resource.owners = [MagicMock(id=5)]
    await manager.raise_for_ownership(resource, user.id)


async def test_raise_for_ownership_denied(manager, mock_dao):
    from liteset.exceptions import LitesetSecurityException

    user = MockUser(id=99, roles=[MockGammaRole()])
    mock_dao.get_user_by_id.return_value = user
    resource = MagicMock()
    resource.owners = [MagicMock(id=5)]
    resource.created_by_fk = None
    with pytest.raises(LitesetSecurityException):
        await manager.raise_for_ownership(resource, user.id)


async def test_raise_for_ownership_unauthenticated(manager):
    from liteset.exceptions import LitesetSecurityException

    resource = MagicMock()
    with pytest.raises(LitesetSecurityException, match="Authentication required"):
        await manager.raise_for_ownership(resource, None)


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


# --- Guest chart access tests (C1 fix) ---


async def test_guest_denied_chart_without_dashboard(manager, mock_dao):
    """Guest user accessing a chart not associated with any dashboard should be denied."""
    from liteset.exceptions import LitesetSecurityException

    guest = MockGuestUser(resources=[{"type": "dashboard", "id": "abc-123"}])
    chart = MagicMock()
    chart.dashboards = []
    with pytest.raises(
        LitesetSecurityException, match="not associated with any dashboard"
    ):
        await manager.raise_for_access(user=guest, chart=chart)


async def test_guest_denied_chart_wrong_dashboard(manager, mock_dao):
    """Guest user accessing a chart whose dashboard is not in their resources."""
    from liteset.exceptions import LitesetSecurityException

    guest = MockGuestUser(resources=[{"type": "dashboard", "id": "abc-123"}])
    dashboard = MagicMock()
    dashboard.uuid = "different-uuid"
    dashboard.id = 999
    chart = MagicMock()
    chart.dashboards = [dashboard]
    with pytest.raises(LitesetSecurityException, match="Guest access denied to chart"):
        await manager.raise_for_access(user=guest, chart=chart)


async def test_guest_allowed_chart_with_dashboard(manager, mock_dao):
    """Guest user can access a chart whose dashboard is in their resources."""
    guest = MockGuestUser(resources=[{"type": "dashboard", "id": "42"}])
    dashboard = MagicMock()
    dashboard.id = 42
    dashboard.embedded = None
    chart = MagicMock()
    chart.dashboards = [dashboard]
    # Should not raise
    await manager.raise_for_access(user=guest, chart=chart)


# ---------------------------------------------------------------------------
# NEW-T10: has_access() with user without roles (early return False)
# ---------------------------------------------------------------------------


async def test_has_access_no_roles_returns_false(manager, mock_dao):
    """User with empty roles returns False without calling DAO."""
    user_no_roles = MockUser(roles=[])
    result = await manager.has_access("can_read", "Chart", user=user_no_roles)
    assert result is False
    mock_dao.has_permission_view.assert_not_called()


# ---------------------------------------------------------------------------
# NEW-T4: can_access_dashboard non-RBAC path (datasource-based)
# ---------------------------------------------------------------------------


async def test_can_access_dashboard_non_rbac_with_datasource_access(mock_dao):
    """Non-RBAC path: user with datasource access can access the dashboard."""
    mgr = AsyncSecurityManager(
        dao=mock_dao, admin_role_name="Admin", dashboard_rbac_enabled=False
    )
    gamma_user = MockUser(id=50, roles=[MockGammaRole()])

    ds = MagicMock()
    ds.perm = "[db].[table](id:1)"
    dashboard = MagicMock()
    dashboard.owners = []
    dashboard.roles = []
    dashboard.datasources = [ds]

    # Grant datasource access
    mock_dao.has_permission_view.return_value = True
    result = await mgr.can_access_dashboard(dashboard, user=gamma_user)
    assert result is True


async def test_can_access_dashboard_non_rbac_no_datasource_access(mock_dao):
    """Non-RBAC path: user without datasource access is denied."""
    mgr = AsyncSecurityManager(
        dao=mock_dao, admin_role_name="Admin", dashboard_rbac_enabled=False
    )
    gamma_user = MockUser(id=50, roles=[MockGammaRole()])

    ds = MagicMock()
    ds.perm = "[db].[table](id:1)"
    ds.database = None
    ds.schema = None
    dashboard = MagicMock()
    dashboard.owners = []
    dashboard.roles = []
    dashboard.datasources = [ds]

    mock_dao.has_permission_view.return_value = False
    result = await mgr.can_access_dashboard(dashboard, user=gamma_user)
    assert result is False


async def test_can_access_dashboard_non_rbac_empty_datasources(mock_dao):
    """Non-RBAC path: dashboard with no datasources is accessible to all authenticated."""
    mgr = AsyncSecurityManager(
        dao=mock_dao, admin_role_name="Admin", dashboard_rbac_enabled=False
    )
    gamma_user = MockUser(id=50, roles=[MockGammaRole()])

    dashboard = MagicMock()
    dashboard.owners = []
    dashboard.roles = []
    dashboard.datasources = []

    result = await mgr.can_access_dashboard(dashboard, user=gamma_user)
    assert result is True


# ---------------------------------------------------------------------------
# TST-I1: Unpublished RBAC dashboard — access denied
# ---------------------------------------------------------------------------


async def test_can_access_dashboard_rbac_unpublished_denied(mock_dao):
    """RBAC enabled + published=False -> access denied even with matching roles."""
    mgr = AsyncSecurityManager(
        dao=mock_dao, admin_role_name="Admin", dashboard_rbac_enabled=True
    )
    gamma_user = MockUser(id=50, roles=[MockGammaRole()])

    dashboard = MagicMock()
    dashboard.owners = []
    dashboard.published = False
    dashboard.roles = [MockGammaRole()]  # matching role

    result = await mgr.can_access_dashboard(dashboard, user=gamma_user)
    assert result is False


async def test_can_access_dashboard_rbac_published_matching_role(mock_dao):
    """RBAC enabled + published=True + matching role -> access granted."""
    mgr = AsyncSecurityManager(
        dao=mock_dao, admin_role_name="Admin", dashboard_rbac_enabled=True
    )
    gamma_user = MockUser(id=50, roles=[MockGammaRole()])

    dashboard = MagicMock()
    dashboard.owners = []
    dashboard.published = True
    dashboard.roles = [MockGammaRole()]

    result = await mgr.can_access_dashboard(dashboard, user=gamma_user)
    assert result is True


async def test_can_access_dashboard_rbac_published_no_matching_role(mock_dao):
    """RBAC enabled + published=True + no matching role -> access denied."""
    mgr = AsyncSecurityManager(
        dao=mock_dao, admin_role_name="Admin", dashboard_rbac_enabled=True
    )
    gamma_user = MockUser(id=50, roles=[MockGammaRole()])

    dashboard = MagicMock()
    dashboard.owners = []
    dashboard.published = True
    dashboard.roles = [MockPublicRole()]  # different role

    result = await mgr.can_access_dashboard(dashboard, user=gamma_user)
    assert result is False
