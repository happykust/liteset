"""Tests for AsyncSecurityManager — async security checks."""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock

import pytest

from superset.security.manager import AsyncSecurityManager


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
    from superset.exceptions import SupersetSecurityException

    gamma_user = MockUser(roles=[MockGammaRole()])
    mock_dao.has_permission_view.return_value = False
    mock_dao.get_all_permissions_for_user_with_groups.return_value = set()
    # Table datasource lookup finds no matching SqlaTable -> no access.
    table_result = MagicMock()
    table_result.scalars.return_value.all.return_value = []
    mock_dao.session.execute = AsyncMock(return_value=table_result)

    database = MagicMock()
    database.perm = "[db].(id:1)"
    # Upstream Path-1 guard requires ``(database and table) or query`` — a
    # bare ``database=`` is a no-op. Provide a table to exercise the real
    # table-access denial path.
    table = MagicMock()
    table.catalog = None
    table.schema = None
    table.qualify.return_value = table

    with pytest.raises(SupersetSecurityException):
        await manager.raise_for_access(user=gamma_user, database=database, table=table)


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
    mock_dao.get_all_permissions_for_user_with_groups.return_value = {
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
        "auth:user:1", "auth:user:admin", "auth:user:admin@test.com"
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
    from superset.exceptions import SupersetSecurityException

    user = MockUser(id=99, roles=[MockGammaRole()])
    mock_dao.get_user_by_id.return_value = user
    resource = MagicMock()
    resource.owners = [MagicMock(id=5)]
    resource.created_by_fk = None
    with pytest.raises(SupersetSecurityException):
        await manager.raise_for_ownership(resource, user.id)


async def test_raise_for_ownership_unauthenticated(manager):
    from superset.exceptions import SupersetSecurityException

    resource = MagicMock()
    with pytest.raises(SupersetSecurityException, match="Authentication required"):
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


@pytest.fixture
def embedded_manager(mock_dao):
    """Security manager with EMBEDDED_SUPERSET enabled so is_guest_user works."""
    return AsyncSecurityManager(
        dao=mock_dao,
        admin_role_name="Admin",
        embedded_superset_enabled=True,
    )


async def test_raise_for_access_guest_datasource_denied(embedded_manager, mock_dao):
    """Guest user without datasource access is denied via the datasource path.

    Upstream ``raise_for_access`` has no guest-specific database/query denial;
    guests are simply non-admin users whose datasource access is checked
    normally (Path-3). With no granted permissions, access is denied.
    """
    from superset.exceptions import SupersetSecurityException

    guest = MockGuestUser()
    mock_dao.has_permission_view.return_value = False
    mock_dao.get_all_permissions_for_user_with_groups.return_value = set()

    datasource = MagicMock()
    datasource.perm = "[db].[t](id:1)"
    datasource.database = None
    datasource.catalog = None
    datasource.schema = None
    datasource.owners = []
    # No form_data / dashboardId -> no dashboard RBAC fallback.

    with pytest.raises(SupersetSecurityException):
        await embedded_manager.raise_for_access(user=guest, datasource=datasource)


async def test_raise_for_access_guest_query_context_modified_denied(
    embedded_manager, mock_dao
):
    """Guest user modifying a chart payload is denied (Path-2).

    This is the only guest-specific denial in upstream ``raise_for_access``.
    """
    from unittest.mock import patch

    from superset.exceptions import SupersetSecurityException

    guest = MockGuestUser()
    query_context = MagicMock()

    with patch("superset.security.manager.query_context_modified", return_value=True):
        with pytest.raises(
            SupersetSecurityException, match="Guest user cannot modify chart payload"
        ):
            await embedded_manager.raise_for_access(
                user=guest, query_context=query_context
            )


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


def test_get_guest_user_from_request(embedded_manager):
    # is_guest_user only returns True when EMBEDDED_SUPERSET is enabled.
    request = MagicMock()
    request.user = MockGuestUser()
    result = embedded_manager.get_guest_user_from_request(request)
    assert result is not None
    assert result.is_guest is True


def test_get_guest_user_from_request_not_guest(manager):
    request = MagicMock()
    request.user = MockUser()
    result = manager.get_guest_user_from_request(request)
    assert result is None


# --- Guest chart access tests (C1 fix) ---


async def test_guest_denied_chart_without_datasource_access(embedded_manager, mock_dao):
    """Guest (non-admin, non-owner) without datasource access is denied a chart.

    Upstream chart path (Path-5) has no guest-specific dashboard-association
    logic — guests are checked via owner/datasource access like any non-admin.
    """
    from superset.exceptions import SupersetSecurityException

    guest = MockGuestUser(resources=[{"type": "dashboard", "id": "abc-123"}])
    mock_dao.has_permission_view.return_value = False
    mock_dao.get_all_permissions_for_user_with_groups.return_value = set()

    chart = MagicMock()
    chart.owners = []
    chart.datasource = None  # no datasource -> cannot grant datasource access

    with pytest.raises(
        SupersetSecurityException, match="don't have access to this chart"
    ):
        await embedded_manager.raise_for_access(user=guest, chart=chart)


async def test_guest_allowed_chart_when_owner(embedded_manager, mock_dao):
    """Guest user listed as a chart owner can access it (Path-5 owner check)."""
    guest = MockGuestUser(id=5, resources=[{"type": "dashboard", "id": "42"}])
    chart = MagicMock()
    chart.owners = [MagicMock(id=5)]
    # Should not raise (owner bypass)
    await embedded_manager.raise_for_access(user=guest, chart=chart)


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
    """Non-RBAC path: dashboard with no datasources is accessible to all
    authenticated.
    """
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


# ---------------------------------------------------------------------------
# has_access slow path: group-inherited roles  (audit MEDIUM)
# ---------------------------------------------------------------------------


async def test_has_access_includes_group_roles(manager, mock_dao):
    """A user with no direct roles is granted via a group-inherited role.

    1:1 with FAB ``_has_view_access`` which resolves permissions across both
    ``ab_user_role`` and ``ab_user_group`` → ``ab_group_role``.
    """
    user = MockUser(id=5, roles=[])  # no direct roles
    mock_dao.get_user_groups = AsyncMock(return_value=[(10, "grp1")])
    mock_dao.get_group_roles = AsyncMock(return_value=[(99, "GroupRole")])
    mock_dao.has_permission_view = AsyncMock(return_value=True)

    result = await manager.has_access(
        "datasource_access", "[examples].[t](id:1)", user=user
    )

    assert result is True
    mock_dao.get_user_groups.assert_awaited_once_with(5)
    mock_dao.get_group_roles.assert_awaited_once_with(10)
    _, kwargs = mock_dao.has_permission_view.call_args
    assert 99 in kwargs["role_ids"]


async def test_has_access_combines_direct_and_group_roles(manager, mock_dao):
    """Direct roles and group-inherited roles are both checked."""
    user = MockUser(id=5, roles=[MockGammaRole()])  # direct role id 2
    mock_dao.get_user_groups = AsyncMock(return_value=[(10, "grp1")])
    mock_dao.get_group_roles = AsyncMock(return_value=[(99, "GroupRole")])
    mock_dao.has_permission_view = AsyncMock(return_value=True)

    await manager.has_access("can_read", "Chart", user=user)

    _, kwargs = mock_dao.has_permission_view.call_args
    assert set(kwargs["role_ids"]) == {2, 99}


# ---------------------------------------------------------------------------
# get_schema_perm: object → verbose_name, string → as-is  (audit MEDIUM)
# ---------------------------------------------------------------------------


def test_get_schema_perm_object_uses_name(manager):
    """A Database OBJECT resolves via ``str(database)`` → name
    (``verbose_name or database_name``), 1:1 with the original access check.
    """
    db = MagicMock()
    db.__str__.return_value = "Prod DB"  # mimics Database.__repr__ → name
    assert manager.get_schema_perm(db, "public") == "[Prod DB].[public]"


def test_get_schema_perm_string_used_as_is(manager):
    """A plain ``database_name`` string (PVM-creation callers) is used verbatim."""
    assert manager.get_schema_perm("examples", "public") == "[examples].[public]"


def test_get_schema_perm_with_catalog(manager):
    """Catalog is interpolated as ``[db].[catalog].[schema]``."""
    assert (
        manager.get_schema_perm("examples", "public", catalog="cat")
        == "[examples].[cat].[public]"
    )


# ---------------------------------------------------------------------------
# get_catalogs_accessible_by_user: admin bypass is inside hierarchical gate
# (audit MEDIUM — real regression fix: removed unconditional is_admin shortcut)
# ---------------------------------------------------------------------------


async def test_get_catalogs_accessible_by_user_admin_hierarchical_true(
    manager: AsyncSecurityManager, mock_dao: AsyncMock
) -> None:
    """Admin gets all catalogs when hierarchical=True (via can_access_database).

    1:1 with original: ``if hierarchical and self.can_access_database(database)``
    short-circuits for admin because can_access_database includes is_admin check.
    """
    admin_user = MockUser(roles=[MockRole()])
    database = MagicMock()
    database.perm = "[db].(id:1)"
    catalogs = ["cat1", "cat2", "cat3"]

    result = await manager.get_catalogs_accessible_by_user(
        database, catalogs, hierarchical=True, user=admin_user
    )

    assert result == catalogs


async def test_get_catalogs_accessible_by_user_admin_hierarchical_false_no_perms(
    manager: AsyncSecurityManager, mock_dao: AsyncMock
) -> None:
    """Admin is NOT exempt when hierarchical=False; perm filtering applies.

    Original (superset_old/security/manager.py:983): the admin shortcut is
    ``if hierarchical and self.can_access_database(database)``, so with
    hierarchical=False the shortcut is skipped even for admins and the full
    catalog_access / schema_access / datasource_access filtering runs.
    """
    admin_user = MockUser(roles=[MockRole()])
    database = MagicMock()
    database.id = 42
    database.database_name = "mydb"
    database.get_default_catalog = MagicMock(return_value=None)
    catalogs = ["cat1", "cat2"]

    # Admin has no catalog / schema / datasource perms
    mock_dao.get_all_permissions_for_user_with_groups.return_value = set()

    result = await manager.get_catalogs_accessible_by_user(
        database, catalogs, hierarchical=False, user=admin_user
    )

    # No matching perms → admin gets nothing (perm filter applied, not bypassed)
    assert result == []
    mock_dao.get_all_permissions_for_user_with_groups.assert_awaited_once_with(
        admin_user.id
    )


async def test_get_catalogs_accessible_by_user_admin_non_hierarchical_with_perm(
    manager: AsyncSecurityManager, mock_dao: AsyncMock
) -> None:
    """Admin with catalog_access gets only those catalogs when hierarchical=False.

    Confirms the perm-based path runs (not bypassed) for admins.
    """
    admin_user = MockUser(roles=[MockRole()])
    database = MagicMock()
    database.id = 42
    database.database_name = "mydb"
    database.get_default_catalog = MagicMock(return_value=None)
    catalogs = ["cat1", "cat2", "cat3"]

    # Admin has catalog_access only for cat1
    mock_dao.get_all_permissions_for_user_with_groups.return_value = {
        ("catalog_access", "[mydb].[cat1]"),
    }

    result = await manager.get_catalogs_accessible_by_user(
        database, catalogs, hierarchical=False, user=admin_user
    )

    assert result == ["cat1"]
