"""Tests for AsyncSecurityDAO — async queries against FAB tables."""

from __future__ import annotations

import pytest
from sqlalchemy import Column, ForeignKey, insert, Integer, String, Table
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase, relationship

from superset.security.dao import AsyncSecurityDAO

# --- Minimal FAB-compatible models for testing ---


class Base(DeclarativeBase):
    pass


ab_user_role = Table(
    "ab_user_role",
    Base.metadata,
    Column("id", Integer, primary_key=True),
    Column("user_id", Integer, ForeignKey("ab_user.id")),
    Column("role_id", Integer, ForeignKey("ab_role.id")),
)

ab_permission_view_role = Table(
    "ab_permission_view_role",
    Base.metadata,
    Column("id", Integer, primary_key=True),
    Column("permission_view_id", Integer, ForeignKey("ab_permission_view.id")),
    Column("role_id", Integer, ForeignKey("ab_role.id")),
)

# Group RBAC tables (raw — no ORM model, matching FAB's schema)
ab_group = Table(
    "ab_group",
    Base.metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String(256), unique=True, nullable=False),
)

ab_user_group = Table(
    "ab_user_group",
    Base.metadata,
    Column("id", Integer, primary_key=True),
    Column("user_id", Integer, ForeignKey("ab_user.id")),
    Column("group_id", Integer, ForeignKey("ab_group.id")),
)

ab_group_role = Table(
    "ab_group_role",
    Base.metadata,
    Column("id", Integer, primary_key=True),
    Column("group_id", Integer, ForeignKey("ab_group.id")),
    Column("role_id", Integer, ForeignKey("ab_role.id")),
)


class FakeUser(Base):
    __tablename__ = "ab_user"
    id = Column(Integer, primary_key=True)
    username = Column(String(256), unique=True, nullable=False)
    email = Column(String(256), unique=True, nullable=False)
    active = Column(Integer, default=1)
    roles = relationship("FakeRole", secondary=ab_user_role, backref="users")


class FakeRole(Base):
    __tablename__ = "ab_role"
    id = Column(Integer, primary_key=True)
    name = Column(String(256), unique=True, nullable=False)
    permissions = relationship(
        "FakePermissionView",
        secondary=ab_permission_view_role,
        # Mirror the production model exactly: FAB uses the singular
        # ``role`` backref, so ``PermissionView.roles`` must NOT exist
        # (a plural backref here once masked an AttributeError in the
        # DAO's has_permission_view/get_role_permissions joins).
        backref="role",
    )


class FakePermission(Base):
    __tablename__ = "ab_permission"
    id = Column(Integer, primary_key=True)
    name = Column(String(256), unique=True, nullable=False)


class FakeViewMenu(Base):
    __tablename__ = "ab_view_menu"
    id = Column(Integer, primary_key=True)
    name = Column(String(256), unique=True, nullable=False)


class FakePermissionView(Base):
    __tablename__ = "ab_permission_view"
    id = Column(Integer, primary_key=True)
    permission_id = Column(Integer, ForeignKey("ab_permission.id"))
    view_menu_id = Column(Integer, ForeignKey("ab_view_menu.id"))
    permission = relationship("FakePermission")
    view_menu = relationship("FakeViewMenu")


@pytest.fixture
async def populated_session():
    """Create in-memory DB and populate FAB tables with test data."""
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Populate via raw inserts to avoid lazy-load issues
    async with engine.begin() as conn:
        await conn.execute(
            insert(FakePermission).values(
                [
                    {"id": 1, "name": "can_read"},
                    {"id": 2, "name": "can_write"},
                ]
            )
        )
        await conn.execute(
            insert(FakeViewMenu).values(
                [
                    {"id": 1, "name": "Chart"},
                    {"id": 2, "name": "Dashboard"},
                ]
            )
        )
        await conn.execute(
            insert(FakePermissionView).values(
                [
                    {"id": 1, "permission_id": 1, "view_menu_id": 1},
                    {"id": 2, "permission_id": 2, "view_menu_id": 1},
                    {"id": 3, "permission_id": 1, "view_menu_id": 2},
                ]
            )
        )
        await conn.execute(
            insert(FakeRole).values(
                [
                    {"id": 1, "name": "Admin"},
                    {"id": 2, "name": "Viewer"},
                ]
            )
        )
        await conn.execute(
            insert(FakeUser).values(
                [
                    {
                        "id": 1,
                        "username": "admin",
                        "email": "admin@test.com",
                        "active": 1,
                    },
                    {
                        "id": 2,
                        "username": "viewer",
                        "email": "viewer@test.com",
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
        # User-role associations
        await conn.execute(
            insert(ab_user_role).values(
                [
                    {"id": 1, "user_id": 1, "role_id": 1},
                    {"id": 2, "user_id": 2, "role_id": 2},
                ]
            )
        )
        # Permission-view-role associations
        await conn.execute(
            insert(ab_permission_view_role).values(
                [
                    {"id": 1, "permission_view_id": 1, "role_id": 1},
                    {"id": 2, "permission_view_id": 2, "role_id": 1},
                    {"id": 3, "permission_view_id": 3, "role_id": 1},
                    {"id": 4, "permission_view_id": 1, "role_id": 2},
                ]
            )
        )

    async with AsyncSession(engine, expire_on_commit=False) as session:
        yield session

    await engine.dispose()


def _make_dao(session: AsyncSession) -> AsyncSecurityDAO:
    return AsyncSecurityDAO(
        session,
        user_model=FakeUser,
        role_model=FakeRole,
        permission_model=FakePermission,
        view_menu_model=FakeViewMenu,
        permission_view_model=FakePermissionView,
    )


async def test_get_user_by_id(populated_session):
    dao = _make_dao(populated_session)
    user = await dao.get_user_by_id(1)
    assert user is not None
    assert user.username == "admin"


async def test_get_user_by_id_not_found(populated_session):
    dao = _make_dao(populated_session)
    user = await dao.get_user_by_id(999)
    assert user is None


async def test_get_user_by_username(populated_session):
    dao = _make_dao(populated_session)
    user = await dao.get_user_by_username("viewer")
    assert user is not None
    assert user.email == "viewer@test.com"


async def test_get_user_by_email(populated_session):
    dao = _make_dao(populated_session)
    user = await dao.get_user_by_email("admin@test.com")
    assert user is not None
    assert user.username == "admin"


async def test_get_user_by_username_is_case_insensitive(populated_session):
    """AUTH_USERNAME_CI defaults to True (FAB parity): a differently-cased
    username still resolves the same user, so OAuth/LDAP self-registration
    cannot mint a case-variant duplicate."""
    dao = _make_dao(populated_session)
    user = await dao.get_user_by_username("VIEWER")
    assert user is not None
    assert user.username == "viewer"


async def test_get_user_by_username_multiple_case_variants_returns_none(
    populated_session,
):
    """A legacy DB holding case-variant duplicates yields MultipleResultsFound
    on the case-insensitive lookup; mirror FAB find_user by logging and
    degrading to None rather than raising."""
    await populated_session.execute(
        insert(FakeUser).values(
            {"id": 4, "username": "VIEWER", "email": "viewer2@test.com", "active": 1}
        )
    )
    await populated_session.commit()
    dao = _make_dao(populated_session)
    user = await dao.get_user_by_username("viewer")
    assert user is None


async def test_get_user_roles(populated_session):
    dao = _make_dao(populated_session)
    user = await dao.get_user_by_id(1)
    roles = await dao.get_user_roles(user)
    role_names = [r.name for r in roles]
    assert "Admin" in role_names


async def test_get_role_permissions(populated_session):
    dao = _make_dao(populated_session)
    perms = await dao.get_role_permissions(1)  # Admin role
    assert len(perms) == 3  # read_chart, write_chart, read_dashboard


async def test_has_permission_view(populated_session):
    dao = _make_dao(populated_session)
    assert await dao.has_permission_view("can_read", "Chart", role_ids=[1]) is True
    assert await dao.has_permission_view("can_write", "Chart", role_ids=[2]) is False


async def test_get_all_permissions_for_user(populated_session):
    dao = _make_dao(populated_session)
    perms = await dao.get_all_permissions_for_user(1)  # admin
    assert ("can_read", "Chart") in perms
    assert ("can_write", "Chart") in perms
    assert ("can_read", "Dashboard") in perms

    viewer_perms = await dao.get_all_permissions_for_user(2)
    assert ("can_read", "Chart") in viewer_perms
    assert ("can_write", "Chart") not in viewer_perms


# --- Group RBAC tests ---


@pytest.fixture
async def group_session():
    """DB with group tables populated for group RBAC testing."""
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with engine.begin() as conn:
        # Base data: permissions, views, roles, users
        await conn.execute(
            insert(FakePermission).values(
                [
                    {"id": 1, "name": "can_read"},
                    {"id": 2, "name": "can_write"},
                ]
            )
        )
        await conn.execute(
            insert(FakeViewMenu).values(
                [
                    {"id": 1, "name": "Chart"},
                    {"id": 2, "name": "Dashboard"},
                ]
            )
        )
        await conn.execute(
            insert(FakePermissionView).values(
                [
                    {"id": 1, "permission_id": 1, "view_menu_id": 1},  # can_read Chart
                    {
                        "id": 2,
                        "permission_id": 2,
                        "view_menu_id": 2,
                    },  # can_write Dashboard
                ]
            )
        )
        await conn.execute(
            insert(FakeRole).values(
                [
                    {"id": 1, "name": "GroupRole"},
                ]
            )
        )
        await conn.execute(
            insert(FakeUser).values(
                [
                    {
                        "id": 1,
                        "username": "groupuser",
                        "email": "gu@test.com",
                        "active": 1,
                    },
                ]
            )
        )
        # No direct role assignment — user gets perms only via group
        # GroupRole has can_read Chart and can_write Dashboard
        await conn.execute(
            insert(ab_permission_view_role).values(
                [
                    {"id": 1, "permission_view_id": 1, "role_id": 1},
                    {"id": 2, "permission_view_id": 2, "role_id": 1},
                ]
            )
        )
        # Group: "TeamA" → GroupRole; user 1 in TeamA
        await conn.execute(
            insert(ab_group).values(
                [
                    {"id": 1, "name": "TeamA"},
                ]
            )
        )
        await conn.execute(
            insert(ab_user_group).values(
                [
                    {"id": 1, "user_id": 1, "group_id": 1},
                ]
            )
        )
        await conn.execute(
            insert(ab_group_role).values(
                [
                    {"id": 1, "group_id": 1, "role_id": 1},
                ]
            )
        )

    async with AsyncSession(engine, expire_on_commit=False) as session:
        yield session

    await engine.dispose()


async def test_get_user_groups(group_session):
    dao = _make_dao(group_session)
    groups = await dao.get_user_groups(1)
    assert len(groups) == 1
    assert groups[0][1] == "TeamA"


async def test_get_group_roles(group_session):
    dao = _make_dao(group_session)
    roles = await dao.get_group_roles(1)  # group_id=1
    assert len(roles) == 1
    assert roles[0][1] == "GroupRole"


async def test_get_group_permissions(group_session):
    dao = _make_dao(group_session)
    perms = await dao.get_group_permissions(1)  # user_id=1
    assert ("can_read", "Chart") in perms
    assert ("can_write", "Dashboard") in perms


async def test_get_all_permissions_for_user_with_groups(group_session):
    dao = _make_dao(group_session)
    # User 1 has no direct roles, only group-inherited permissions
    direct = await dao.get_all_permissions_for_user(1)
    assert len(direct) == 0  # no direct role assignment

    combined = await dao.get_all_permissions_for_user_with_groups(1)
    assert ("can_read", "Chart") in combined
    assert ("can_write", "Dashboard") in combined


async def test_user_not_in_group_gets_no_group_perms(group_session):
    dao = _make_dao(group_session)
    # user_id=999 doesn't exist — should get empty sets
    groups = await dao.get_user_groups(999)
    assert len(groups) == 0
    perms = await dao.get_group_permissions(999)
    assert len(perms) == 0


# --- Regression: production FAB models (not fakes) ---------------------
#
# The fakes above once declared ``backref="roles"`` (plural) on
# ``FakeRole.permissions`` while the production ``PermissionView`` only
# has the FAB-faithful singular ``role`` backref — masking an
# ``AttributeError`` in the DAO's ``join(PV.roles)``. These tests run
# the hot-path queries against the *real* superset.models.security
# models so any drift between DAO joins and the production mapping
# fails here, not in production.


@pytest.fixture
async def prod_models_session():
    from superset.models import security as sec_models
    from superset.models.helpers import Base as ProdBase

    tables = [
        ProdBase.metadata.tables[name]
        for name in (
            "ab_user",
            "ab_role",
            "ab_permission",
            "ab_view_menu",
            "ab_permission_view",
            "ab_user_role",
            "ab_permission_view_role",
            "ab_group",
            "ab_user_group",
            "ab_group_role",
        )
    ]
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(ProdBase.metadata.create_all, tables=tables)

    from sqlalchemy.ext.asyncio import async_sessionmaker

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        perm = sec_models.Permission(name="can_read")
        view = sec_models.ViewMenu(name="Chart")
        session.add_all([perm, view])
        await session.flush()
        pv = sec_models.PermissionView(permission_id=perm.id, view_menu_id=view.id)
        session.add(pv)
        await session.flush()
        role = sec_models.Role(name="Alpha")
        role.permissions.append(pv)
        empty_role = sec_models.Role(name="Gamma")
        session.add_all([role, empty_role])
        await session.commit()
        yield session, role.id, empty_role.id
    await engine.dispose()


async def test_has_permission_view_with_production_models(prod_models_session):
    session, role_id, empty_role_id = prod_models_session
    dao = AsyncSecurityDAO(session)  # default models = production models
    assert await dao.has_permission_view("can_read", "Chart", role_ids=[role_id])
    assert not await dao.has_permission_view(
        "can_read", "Chart", role_ids=[empty_role_id]
    )
    assert not await dao.has_permission_view("can_write", "Chart", role_ids=[role_id])


async def test_get_role_permissions_with_production_models(prod_models_session):
    session, role_id, empty_role_id = prod_models_session
    dao = AsyncSecurityDAO(session)
    perms = await dao.get_role_permissions(role_id)
    assert [(p.permission.name, p.view_menu.name) for p in perms] == [
        ("can_read", "Chart")
    ]
    assert await dao.get_role_permissions(empty_role_id) == []


async def test_get_user_roles_includes_group_roles(group_session):
    """get_user_roles must include group-inherited roles (1:1 with FAB
    user.roles + group.roles). groupuser has its role only via a group."""
    dao = _make_dao(group_session)
    user = await dao.get_user_by_id(1)  # groupuser — no direct role
    roles = await dao.get_user_roles(user)
    role_ids = {r.id for r in roles}
    role_names = {r.name for r in roles}
    assert 1 in role_ids, "group-derived role must be present"
    assert "GroupRole" in role_names
