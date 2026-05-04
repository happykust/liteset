"""Tests for superset.security.sync_roles — async role synchronisation."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import selectinload

from superset.models.helpers import Base
from superset.models.security import (
    Permission,
    PermissionView,
    Role,
    ViewMenu,
)
from superset.security.permissions import (
    ACCESSIBLE_PERMS,
    CUSTOM_PERMISSION_VIEWS,
)
from superset.security.sync_roles import (
    _clean_perms,
    _get_all_pvms,
    _get_or_create_permission,
    _get_or_create_pvm,
    _get_or_create_role,
    _get_or_create_view_menu,
    _is_admin_pvm,
    _is_alpha_pvm,
    _is_gamma_pvm,
    _is_sql_lab_pvm,
    sync_role_definitions,
)


@pytest.fixture
async def engine():
    """Create an in-memory async engine with all security tables."""
    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine):
    """Provide a fresh AsyncSession for each test."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as sess:
        yield sess


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


async def test_get_or_create_permission_creates(session: AsyncSession):
    perm = await _get_or_create_permission(session, "can_read")
    assert perm.id is not None
    assert perm.name == "can_read"


async def test_get_or_create_permission_idempotent(session: AsyncSession):
    p1 = await _get_or_create_permission(session, "can_read")
    p2 = await _get_or_create_permission(session, "can_read")
    assert p1.id == p2.id


async def test_get_or_create_view_menu_creates(session: AsyncSession):
    vm = await _get_or_create_view_menu(session, "Chart")
    assert vm.id is not None
    assert vm.name == "Chart"


async def test_get_or_create_view_menu_idempotent(session: AsyncSession):
    v1 = await _get_or_create_view_menu(session, "Chart")
    v2 = await _get_or_create_view_menu(session, "Chart")
    assert v1.id == v2.id


async def test_get_or_create_pvm_creates(session: AsyncSession):
    pvm = await _get_or_create_pvm(session, "can_read", "Chart")
    assert pvm.id is not None
    assert pvm.permission_id is not None
    assert pvm.view_menu_id is not None


async def test_get_or_create_pvm_idempotent(session: AsyncSession):
    pvm1 = await _get_or_create_pvm(session, "can_read", "Chart")
    pvm2 = await _get_or_create_pvm(session, "can_read", "Chart")
    assert pvm1.id == pvm2.id


async def test_get_or_create_role_creates(session: AsyncSession):
    role = await _get_or_create_role(session, "Admin")
    assert role.id is not None
    assert role.name == "Admin"


async def test_get_or_create_role_idempotent(session: AsyncSession):
    r1 = await _get_or_create_role(session, "Admin")
    r2 = await _get_or_create_role(session, "Admin")
    assert r1.id == r2.id


# ---------------------------------------------------------------------------
# PVM predicate tests
# ---------------------------------------------------------------------------


class _FakePerm:
    """Lightweight stand-in for Permission (no SQLAlchemy instance state)."""

    def __init__(self, name: str) -> None:
        self.name = name


class _FakeVM:
    """Lightweight stand-in for ViewMenu (no SQLAlchemy instance state)."""

    def __init__(self, name: str) -> None:
        self.name = name


class _FakePVM:
    """Lightweight stand-in for PermissionView (no SQLAlchemy instance state)."""

    def __init__(self, perm_name: str, vm_name: str) -> None:
        self.permission = _FakePerm(perm_name)
        self.view_menu = _FakeVM(vm_name)


def _make_pvm(perm_name: str, vm_name: str):  # type: ignore[return]
    """Create a fake PermissionView for predicate testing."""
    return _FakePVM(perm_name, vm_name)


def test_is_admin_pvm_non_object_spec():
    """Admin gets everything except object-specific permissions."""
    pvm = _make_pvm("can_read", "Chart")
    assert _is_admin_pvm(pvm) is True


def test_is_admin_pvm_excludes_object_spec():
    """Admin does not get user-defined (object-specific) permissions."""
    pvm = _make_pvm("datasource_access", "[db].[table](id:1)")
    assert _is_admin_pvm(pvm) is False


def test_is_alpha_pvm_normal():
    pvm = _make_pvm("can_read", "Chart")
    assert _is_alpha_pvm(pvm) is True


def test_is_alpha_pvm_excludes_admin_only():
    """Alpha should NOT get Admin-only permissions."""
    pvm = _make_pvm("can_warm_up_cache", "SomeView")
    assert _is_alpha_pvm(pvm) is False


def test_is_alpha_pvm_excludes_admin_only_view():
    """Alpha should NOT get Admin-only view menus."""
    pvm = _make_pvm("can_read", "Security")
    assert _is_alpha_pvm(pvm) is False


def test_is_alpha_pvm_excludes_sql_lab_only():
    """Alpha should NOT get SQL-Lab-only permissions."""
    pvm = _make_pvm("can_read", "SavedQuery")
    assert _is_alpha_pvm(pvm) is False


def test_is_alpha_pvm_gets_accessible_to_all():
    """Alpha always gets ACCESSIBLE_PERMS regardless of other checks."""
    pvm = _make_pvm("can_userinfo", "SomeView")
    assert _is_alpha_pvm(pvm) is True


def test_is_gamma_pvm_normal():
    pvm = _make_pvm("can_read", "Chart")
    assert _is_gamma_pvm(pvm) is True


def test_is_gamma_pvm_excludes_admin_only():
    pvm = _make_pvm("can_grant_guest_token", "Security")
    assert _is_gamma_pvm(pvm) is False


def test_is_gamma_pvm_excludes_alpha_only_view():
    """Gamma should NOT get Alpha-only view menus."""
    pvm = _make_pvm("can_read", "ReportSchedule")
    assert _is_gamma_pvm(pvm) is False


def test_is_gamma_pvm_excludes_alpha_only_permission():
    """Gamma should NOT get Alpha-only permissions."""
    pvm = _make_pvm("muldelete", "Chart")
    assert _is_gamma_pvm(pvm) is False


def test_is_gamma_pvm_gets_accessible_to_all():
    """Gamma always gets ACCESSIBLE_PERMS."""
    pvm = _make_pvm("resetmypassword", "SomeView")
    assert _is_gamma_pvm(pvm) is True


def test_is_gamma_pvm_excludes_sql_lab():
    pvm = _make_pvm("can_sqllab", "Superset")
    assert _is_gamma_pvm(pvm) is False


def test_is_sql_lab_pvm_only():
    pvm = _make_pvm("can_read", "SavedQuery")
    assert _is_sql_lab_pvm(pvm) is True


def test_is_sql_lab_pvm_extra():
    pvm = _make_pvm("can_csv", "Superset")
    assert _is_sql_lab_pvm(pvm) is True


def test_is_sql_lab_pvm_unrelated():
    pvm = _make_pvm("can_read", "Chart")
    assert _is_sql_lab_pvm(pvm) is False


# ---------------------------------------------------------------------------
# clean_perms tests
# ---------------------------------------------------------------------------


async def test_clean_perms_removes_faulty(session: AsyncSession):
    """Faulty PVMs (NULL permission_id or view_menu_id) should be deleted."""
    # Create a normal PVM
    await _get_or_create_pvm(session, "can_read", "Chart")

    # Create faulty PVMs directly
    faulty1 = PermissionView(permission_id=None, view_menu_id=None)
    session.add(faulty1)
    await session.flush()

    pvms_before = await _get_all_pvms(session)
    all_before = (await session.execute(select(PermissionView))).scalars().all()

    assert len(pvms_before) == 1  # Only the valid one
    assert len(all_before) == 2  # Valid + faulty

    await _clean_perms(session)

    all_after = (await session.execute(select(PermissionView))).scalars().all()
    assert len(all_after) == 1


# ---------------------------------------------------------------------------
# Full sync_role_definitions tests
# ---------------------------------------------------------------------------


async def test_sync_role_definitions_creates_roles(session: AsyncSession):
    """sync_role_definitions should create Admin, Alpha, Gamma, sql_lab, Public."""
    await sync_role_definitions(session)

    for role_name in ["Admin", "Alpha", "Gamma", "sql_lab", "Public"]:
        result = await session.execute(select(Role).where(Role.name == role_name))
        role = result.scalars().one_or_none()
        assert role is not None, f"Role '{role_name}' should exist"


async def test_sync_role_definitions_creates_custom_permissions(
    session: AsyncSession,
):
    """All CUSTOM_PERMISSION_VIEWS should be created in the database."""
    await sync_role_definitions(session)

    for perm_name, vm_name in CUSTOM_PERMISSION_VIEWS:
        result = await session.execute(
            select(PermissionView)
            .join(PermissionView.permission)
            .join(PermissionView.view_menu)
            .where(Permission.name == perm_name, ViewMenu.name == vm_name)
        )
        pvm = result.scalars().one_or_none()
        assert pvm is not None, f"Custom PVM ({perm_name}, {vm_name}) should exist"


async def test_sync_role_definitions_admin_gets_all_non_object_spec(
    session: AsyncSession,
):
    """Admin should get all PVMs except object-specific ones."""
    # Seed some PVMs including one object-specific
    await _get_or_create_pvm(session, "can_read", "Chart")
    await _get_or_create_pvm(session, "can_write", "Dashboard")
    await _get_or_create_pvm(session, "datasource_access", "[db].[table](id:1)")
    await session.flush()

    await sync_role_definitions(session)

    result = await session.execute(
        select(Role)
        .where(Role.name == "Admin")
        .options(
            selectinload(Role.permissions).selectinload(PermissionView.permission),
            selectinload(Role.permissions).selectinload(PermissionView.view_menu),
        )
    )
    admin = result.scalars().one()

    admin_perm_names = {
        (p.permission.name, p.view_menu.name) for p in admin.permissions
    }
    # Admin should have can_read/Chart and can_write/Dashboard
    assert ("can_read", "Chart") in admin_perm_names
    assert ("can_write", "Dashboard") in admin_perm_names
    # Admin should NOT have object-specific permissions
    assert ("datasource_access", "[db].[table](id:1)") not in admin_perm_names


async def test_sync_role_definitions_gamma_excludes_admin_and_alpha(
    session: AsyncSession,
):
    """Gamma should not get Admin-only or Alpha-only PVMs."""
    # Admin-only PVM
    await _get_or_create_pvm(session, "can_warm_up_cache", "SomeView")
    # Alpha-only view menu PVM
    await _get_or_create_pvm(session, "can_read", "ReportSchedule")
    # Normal PVM that Gamma should get
    await _get_or_create_pvm(session, "can_read", "Chart")
    await session.flush()

    await sync_role_definitions(session)

    result = await session.execute(
        select(Role)
        .where(Role.name == "Gamma")
        .options(
            selectinload(Role.permissions).selectinload(PermissionView.permission),
            selectinload(Role.permissions).selectinload(PermissionView.view_menu),
        )
    )
    gamma = result.scalars().one()
    gamma_perms = {(p.permission.name, p.view_menu.name) for p in gamma.permissions}

    assert ("can_read", "Chart") in gamma_perms
    assert ("can_warm_up_cache", "SomeView") not in gamma_perms
    assert ("can_read", "ReportSchedule") not in gamma_perms


async def test_sync_role_definitions_sql_lab_gets_only_sqllab(
    session: AsyncSession,
):
    """sql_lab role should only get SQL-Lab-related PVMs."""
    # SQL Lab only PVM
    await _get_or_create_pvm(session, "can_read", "SavedQuery")
    # SQL Lab extra PVM
    await _get_or_create_pvm(session, "can_csv", "Superset")
    # Non-SQL Lab PVM
    await _get_or_create_pvm(session, "can_read", "Chart")
    await session.flush()

    await sync_role_definitions(session)

    result = await session.execute(
        select(Role)
        .where(Role.name == "sql_lab")
        .options(
            selectinload(Role.permissions).selectinload(PermissionView.permission),
            selectinload(Role.permissions).selectinload(PermissionView.view_menu),
        )
    )
    sql_lab = result.scalars().one()
    sql_lab_perms = {(p.permission.name, p.view_menu.name) for p in sql_lab.permissions}

    assert ("can_read", "SavedQuery") in sql_lab_perms
    assert ("can_csv", "Superset") in sql_lab_perms
    assert ("can_read", "Chart") not in sql_lab_perms


async def test_sync_role_definitions_idempotent(session: AsyncSession):
    """Running sync twice should produce the same result."""
    summary1 = await sync_role_definitions(session)
    await session.flush()
    summary2 = await sync_role_definitions(session)

    assert summary1["admin_permissions"] == summary2["admin_permissions"]
    assert summary1["alpha_permissions"] == summary2["alpha_permissions"]
    assert summary1["gamma_permissions"] == summary2["gamma_permissions"]
    assert summary1["sql_lab_permissions"] == summary2["sql_lab_permissions"]


async def test_sync_role_definitions_returns_summary(session: AsyncSession):
    """Summary dict should have the expected keys."""
    summary = await sync_role_definitions(session)

    assert "roles_synced" in summary
    assert "admin_permissions" in summary
    assert "alpha_permissions" in summary
    assert "gamma_permissions" in summary
    assert "sql_lab_permissions" in summary
    assert "public_permissions" in summary
    assert "total_pvms" in summary
    assert isinstance(summary["admin_permissions"], int)
    assert summary["admin_permissions"] >= 0


async def test_sync_role_definitions_public_role_like(session: AsyncSession):
    """PUBLIC_ROLE_LIKE="Gamma" should copy Gamma's permissions to Public."""
    # Create some normal PVMs that Gamma would get
    await _get_or_create_pvm(session, "can_read", "Chart")
    await _get_or_create_pvm(session, "can_read", "Dashboard")
    await session.flush()

    await sync_role_definitions(session, public_role_like="Gamma")

    result = await session.execute(
        select(Role)
        .where(Role.name == "Public")
        .options(selectinload(Role.permissions))
    )
    public = result.scalars().one()

    gamma_result = await session.execute(
        select(Role).where(Role.name == "Gamma").options(selectinload(Role.permissions))
    )
    gamma = gamma_result.scalars().one()

    # Public should have at least as many permissions as Gamma
    # (it may have more if data-access permissions were preserved)
    public_pvm_ids = {p.id for p in public.permissions}
    gamma_pvm_ids = {p.id for p in gamma.permissions}
    assert gamma_pvm_ids.issubset(public_pvm_ids)


async def test_sync_role_definitions_accessible_perms_in_all_roles(
    session: AsyncSession,
):
    """ACCESSIBLE_PERMS should appear in both Alpha and Gamma roles."""
    # Create PVMs for accessible perms
    for perm_name in ACCESSIBLE_PERMS:
        await _get_or_create_pvm(session, perm_name, "TestView")
    await session.flush()

    await sync_role_definitions(session)

    for role_name in ["Alpha", "Gamma"]:
        result = await session.execute(
            select(Role)
            .where(Role.name == role_name)
            .options(
                selectinload(Role.permissions).selectinload(PermissionView.permission)
            )
        )
        role = result.scalars().one()
        role_perm_names = {p.permission.name for p in role.permissions}
        for perm_name in ACCESSIBLE_PERMS:
            assert perm_name in role_perm_names, f"{perm_name} should be in {role_name}"


async def test_sync_role_definitions_alpha_excludes_sql_lab_only(
    session: AsyncSession,
):
    """Alpha should NOT get SQL-Lab-only PVMs."""
    await _get_or_create_pvm(session, "can_read", "SavedQuery")
    await _get_or_create_pvm(session, "can_execute_sql_query", "SQLLab")
    await session.flush()

    await sync_role_definitions(session)

    result = await session.execute(
        select(Role)
        .where(Role.name == "Alpha")
        .options(
            selectinload(Role.permissions).selectinload(PermissionView.permission),
            selectinload(Role.permissions).selectinload(PermissionView.view_menu),
        )
    )
    alpha = result.scalars().one()
    alpha_perms = {(p.permission.name, p.view_menu.name) for p in alpha.permissions}

    assert ("can_read", "SavedQuery") not in alpha_perms
    assert ("can_execute_sql_query", "SQLLab") not in alpha_perms


async def test_sync_preserves_existing_data_access_on_public(
    session: AsyncSession,
):
    """PUBLIC_ROLE_LIKE merge should preserve existing data-access permissions."""
    # Pre-create the Public role with a data-access PVM
    public_role = await _get_or_create_role(session, "Public")
    ds_pvm = await _get_or_create_pvm(
        session, "datasource_access", "[db].[table](id:1)"
    )
    public_role.permissions = [ds_pvm]
    await session.flush()

    # Normal PVM for Gamma
    await _get_or_create_pvm(session, "can_read", "Chart")
    await session.flush()

    await sync_role_definitions(session, public_role_like="Gamma")

    result = await session.execute(
        select(Role)
        .where(Role.name == "Public")
        .options(
            selectinload(Role.permissions).selectinload(PermissionView.permission),
            selectinload(Role.permissions).selectinload(PermissionView.view_menu),
        )
    )
    public = result.scalars().one()
    public_perms = {(p.permission.name, p.view_menu.name) for p in public.permissions}

    # The data-access permission should be preserved
    assert ("datasource_access", "[db].[table](id:1)") in public_perms
