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
from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from superset.db.base_dao import BaseAsyncDAO
from superset.models.connectors import RowLevelSecurityFilter


class AsyncSecurityDAO(BaseAsyncDAO[RowLevelSecurityFilter]):
    model_cls = RowLevelSecurityFilter


class AsyncRoleDAO:
    """Async DAO for FAB Role model.

    Uses lazy imports for the FAB Role model to avoid triggering the
    Flask import chain at module level.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def search(
        self,
        name_filter: str | None = None,
        order_column: str = "id",
        order_direction: str = "asc",
        page: int = 0,
        page_size: int = 10,
    ) -> tuple[list[Any], int]:
        """Search roles with optional name filter and pagination.

        Returns:
            Tuple of (roles list, total count).
        """
        from sqlalchemy import func, select
        from sqlalchemy.orm import selectinload

        from superset.models.security import Role

        # Base query
        stmt = select(Role)
        count_stmt = select(func.count()).select_from(Role)

        # Apply name filter (case-insensitive substring match)
        if name_filter:
            from superset.utils import escape_like

            escaped = escape_like(name_filter)
            stmt = stmt.where(Role.name.ilike(f"%{escaped}%"))
            count_stmt = count_stmt.where(Role.name.ilike(f"%{escaped}%"))

        # Count
        total = await self.session.scalar(count_stmt) or 0

        # Ordering — only allow known columns
        order_col = getattr(Role, order_column, Role.id)
        if order_direction == "desc":
            stmt = stmt.order_by(order_col.desc())
        else:
            stmt = stmt.order_by(order_col.asc())

        # Eager load relationships to avoid implicit IO
        stmt = stmt.options(
            selectinload(Role.permissions),
            selectinload(Role.user),  # type: ignore[attr-defined]
            selectinload(Role.groups),
        )

        # Pagination
        if page_size > 0:
            stmt = stmt.offset(page * page_size).limit(page_size)

        result = await self.session.execute(stmt)
        roles = list(result.scalars().unique().all())

        return roles, total

    async def find_by_id(self, role_id: int) -> Any | None:
        """Get a single role by ID with eager-loaded relationships."""
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        from superset.models.security import Role

        stmt = (
            select(Role)
            .where(Role.id == role_id)
            .options(
                selectinload(Role.permissions),
                selectinload(Role.user),  # type: ignore[attr-defined]
                selectinload(Role.groups),
            )
        )
        result = await self.session.execute(stmt)
        return result.scalars().unique().one_or_none()

    async def create(self, attributes: dict[str, Any]) -> Any:
        """Create a new role."""
        from superset.models.security import Role

        role = Role(**attributes)
        self.session.add(role)
        await self.session.flush()
        await self.session.refresh(
            role, attribute_names=["permissions", "user", "groups"]
        )
        return role

    async def update(self, role: Any, attributes: dict[str, Any]) -> Any:
        """Update role attributes in-place."""
        for key, value in attributes.items():
            setattr(role, key, value)
        await self.session.flush()
        await self.session.refresh(
            role, attribute_names=["permissions", "user", "groups"]
        )
        return role

    async def delete(self, role: Any) -> None:
        """Delete a single role."""
        await self.session.delete(role)
        await self.session.flush()

    async def get_permissions(self, role_id: int) -> list[Any]:
        """Get all permissions for a role, with permission and view_menu loaded."""
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        from superset.models.security import ab_permission_view_role, PermissionView

        stmt = (
            select(PermissionView)
            .join(
                ab_permission_view_role,
                PermissionView.id == ab_permission_view_role.c.permission_view_id,
            )
            .where(ab_permission_view_role.c.role_id == role_id)
            .options(
                selectinload(PermissionView.permission),
                selectinload(PermissionView.view_menu),
            )
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def set_permissions(
        self, role_id: int, permission_view_menu_ids: list[int]
    ) -> Any:
        """Replace all permission-view entries for a role."""
        from sqlalchemy import select

        from superset.models.security import PermissionView

        role = await self.find_by_id(role_id)
        if role is None:
            return None

        if permission_view_menu_ids:
            stmt = select(PermissionView).where(
                PermissionView.id.in_(permission_view_menu_ids)
            )
            result = await self.session.execute(stmt)
            pvs = list(result.scalars().all())
        else:
            pvs = []

        role.permissions = pvs
        await self.session.flush()
        await self.session.refresh(
            role, attribute_names=["permissions", "user", "groups"]
        )
        return role

    async def set_users(self, role_id: int, user_ids: list[int]) -> Any:
        """Replace all users assigned to a role."""
        from sqlalchemy import select

        from superset.models.security import User

        role = await self.find_by_id(role_id)
        if role is None:
            return None

        if user_ids:
            stmt = select(User).where(User.id.in_(user_ids))
            result = await self.session.execute(stmt)
            users = list(result.scalars().all())
            if len(users) != len(user_ids):
                return "not_found"
        else:
            users = []

        role.user = users
        await self.session.flush()
        await self.session.refresh(
            role, attribute_names=["permissions", "user", "groups"]
        )
        return role

    async def set_groups(self, role_id: int, group_ids: list[int]) -> Any:
        """Replace all groups assigned to a role."""
        from sqlalchemy import select

        from superset.models.security import Group

        role = await self.find_by_id(role_id)
        if role is None:
            return None

        if group_ids:
            stmt = select(Group).where(Group.id.in_(group_ids))
            result = await self.session.execute(stmt)
            groups = list(result.scalars().all())
            if len(groups) != len(group_ids):
                return "not_found"
        else:
            groups = []

        role.groups = groups
        await self.session.flush()
        await self.session.refresh(
            role, attribute_names=["permissions", "user", "groups"]
        )
        return role


class AsyncUserCrudDAO:
    """Full async DAO for FAB User model with CRUD + search.

    Separate from ``db.daos.user.AsyncUserDAO`` which provides
    avatar-specific helpers for the CurrentUser controller.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def search(
        self,
        filters: list[Any] | None = None,
        order_column: str = "id",
        order_direction: str = "asc",
        page: int = 0,
        page_size: int = 25,
    ) -> tuple[list[Any], int]:
        """Search users with filtering and pagination."""
        from sqlalchemy import func, select
        from sqlalchemy.orm import selectinload

        from superset.models.security import User

        stmt = select(User)
        count_stmt = select(func.count()).select_from(User)

        if filters:
            for f in filters:
                stmt = stmt.where(f)
                count_stmt = count_stmt.where(f)

        total = await self.session.scalar(count_stmt) or 0

        order_col = getattr(User, order_column, User.id)
        if order_direction == "desc":
            stmt = stmt.order_by(order_col.desc())
        else:
            stmt = stmt.order_by(order_col.asc())

        stmt = stmt.options(
            selectinload(User.roles),
            selectinload(User.groups),
        )

        if page_size > 0:
            stmt = stmt.offset(page * page_size).limit(page_size)

        result = await self.session.execute(stmt)
        return list(result.scalars().unique().all()), total

    async def find_by_id(self, user_id: int) -> Any | None:
        """Get a single user by ID with eager-loaded relationships."""
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        from superset.models.security import User

        stmt = (
            select(User)
            .where(User.id == user_id)
            .options(
                selectinload(User.roles),
                selectinload(User.groups),
            )
        )
        result = await self.session.execute(stmt)
        return result.scalars().unique().one_or_none()

    async def create(self, attributes: dict[str, Any]) -> Any:
        """Create a new user."""
        from superset.models.security import User

        role_ids = attributes.pop("role_ids", [])
        group_ids = attributes.pop("group_ids", [])

        user = User(**attributes)

        if role_ids:
            from sqlalchemy import select

            from superset.models.security import Role

            role_stmt = select(Role).where(Role.id.in_(role_ids))
            result = await self.session.execute(role_stmt)
            user.roles = list(result.scalars().all())

        if group_ids:
            from sqlalchemy import select

            from superset.models.security import Group

            group_stmt = select(Group).where(Group.id.in_(group_ids))
            result = await self.session.execute(group_stmt)
            user.groups = list(result.scalars().all())

        self.session.add(user)
        await self.session.flush()
        await self.session.refresh(user, attribute_names=["roles", "groups"])
        return user

    async def update(self, user: Any, attributes: dict[str, Any]) -> Any:
        """Update user attributes in-place."""
        role_ids = attributes.pop("role_ids", None)
        group_ids = attributes.pop("group_ids", None)

        for key, value in attributes.items():
            setattr(user, key, value)

        if role_ids is not None:
            from sqlalchemy import select

            from superset.models.security import Role

            role_stmt = select(Role).where(Role.id.in_(role_ids))
            result = await self.session.execute(role_stmt)
            user.roles = list(result.scalars().all())

        if group_ids is not None:
            from sqlalchemy import select

            from superset.models.security import Group

            group_stmt = select(Group).where(Group.id.in_(group_ids))
            result = await self.session.execute(group_stmt)
            user.groups = list(result.scalars().all())

        await self.session.flush()
        await self.session.refresh(user, attribute_names=["roles", "groups"])
        return user

    async def delete(self, user: Any) -> None:
        """Delete a single user."""
        await self.session.delete(user)
        await self.session.flush()


class AsyncGroupDAO:
    """Async DAO for FAB Group model."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def search(
        self,
        filters: list[Any] | None = None,
        order_column: str = "id",
        order_direction: str = "asc",
        page: int = 0,
        page_size: int = 25,
    ) -> tuple[list[Any], int]:
        """Search groups with filtering and pagination."""
        from sqlalchemy import func, select
        from sqlalchemy.orm import selectinload

        from superset.models.security import Group

        stmt = select(Group)
        count_stmt = select(func.count()).select_from(Group)

        if filters:
            for f in filters:
                stmt = stmt.where(f)
                count_stmt = count_stmt.where(f)

        total = await self.session.scalar(count_stmt) or 0

        order_col = getattr(Group, order_column, Group.id)
        if order_direction == "desc":
            stmt = stmt.order_by(order_col.desc())
        else:
            stmt = stmt.order_by(order_col.asc())

        stmt = stmt.options(
            selectinload(Group.roles_),  # type: ignore[attr-defined]
            selectinload(Group.users),  # type: ignore[attr-defined]
        )

        if page_size > 0:
            stmt = stmt.offset(page * page_size).limit(page_size)

        result = await self.session.execute(stmt)
        return list(result.scalars().unique().all()), total

    async def find_by_id(self, group_id: int) -> Any | None:
        """Get a single group by ID with eager-loaded relationships."""
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        from superset.models.security import Group

        stmt = (
            select(Group)
            .where(Group.id == group_id)
            .options(
                selectinload(Group.roles_),  # type: ignore[attr-defined]
                selectinload(Group.users),  # type: ignore[attr-defined]
            )
        )
        result = await self.session.execute(stmt)
        return result.scalars().unique().one_or_none()

    async def create(self, attributes: dict[str, Any]) -> Any:
        """Create a new group."""
        from superset.models.security import Group

        role_ids = attributes.pop("role_ids", [])
        user_ids = attributes.pop("user_ids", [])

        group = Group(**attributes)

        if role_ids:
            from sqlalchemy import select

            from superset.models.security import Role

            role_stmt = select(Role).where(Role.id.in_(role_ids))
            result = await self.session.execute(role_stmt)
            group.roles_ = list(result.scalars().all())  # type: ignore[attr-defined]

        if user_ids:
            from sqlalchemy import select

            from superset.models.security import User

            user_stmt = select(User).where(User.id.in_(user_ids))
            result = await self.session.execute(user_stmt)
            group.users = list(result.scalars().all())  # type: ignore[attr-defined]

        self.session.add(group)
        await self.session.flush()
        await self.session.refresh(group, attribute_names=["roles_", "users"])
        return group

    async def update(self, group: Any, attributes: dict[str, Any]) -> Any:
        """Update group attributes in-place."""
        role_ids = attributes.pop("role_ids", None)
        user_ids = attributes.pop("user_ids", None)

        for key, value in attributes.items():
            setattr(group, key, value)

        if role_ids is not None:
            from sqlalchemy import select

            from superset.models.security import Role

            role_stmt = select(Role).where(Role.id.in_(role_ids))
            result = await self.session.execute(role_stmt)
            group.roles_ = list(result.scalars().all())

        if user_ids is not None:
            from sqlalchemy import select

            from superset.models.security import User

            user_stmt = select(User).where(User.id.in_(user_ids))
            result = await self.session.execute(user_stmt)
            group.users = list(result.scalars().all())

        await self.session.flush()
        await self.session.refresh(group, attribute_names=["roles_", "users"])
        return group

    async def delete(self, group: Any) -> None:
        """Delete a single group."""
        await self.session.delete(group)
        await self.session.flush()


class AsyncPermissionViewDAO:
    """Async DAO for FAB PermissionView model."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def search(
        self,
        filters: list[Any] | None = None,
        order_column: str = "id",
        order_direction: str = "asc",
        page: int = 0,
        page_size: int = 25,
    ) -> tuple[list[Any], int]:
        """Search permission-views with pagination."""
        from sqlalchemy import func, select
        from sqlalchemy.orm import selectinload

        from superset.models.security import PermissionView

        stmt = select(PermissionView)
        count_stmt = select(func.count()).select_from(PermissionView)

        if filters:
            for f in filters:
                stmt = stmt.where(f)
                count_stmt = count_stmt.where(f)

        total = await self.session.scalar(count_stmt) or 0

        order_col = getattr(PermissionView, order_column, PermissionView.id)
        if order_direction == "desc":
            stmt = stmt.order_by(order_col.desc())
        else:
            stmt = stmt.order_by(order_col.asc())

        stmt = stmt.options(
            selectinload(PermissionView.permission),
            selectinload(PermissionView.view_menu),
        )

        if page_size > 0:
            stmt = stmt.offset(page * page_size).limit(page_size)

        result = await self.session.execute(stmt)
        return list(result.scalars().all()), total

    async def find_by_id(self, pv_id: int) -> Any | None:
        """Get a single permission-view by ID."""
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        from superset.models.security import PermissionView

        stmt = (
            select(PermissionView)
            .where(PermissionView.id == pv_id)
            .options(
                selectinload(PermissionView.permission),
                selectinload(PermissionView.view_menu),
            )
        )
        result = await self.session.execute(stmt)
        return result.scalars().one_or_none()

    async def create(self, attributes: dict[str, Any]) -> Any:
        """Create a new permission-view mapping.

        Expects ``permission_id`` and ``view_menu_id`` in *attributes*.
        Mirrors FAB's ``ModelRestApi.post`` for PermissionViewMenu.
        """
        from superset.models.security import PermissionView

        pv = PermissionView(**attributes)
        self.session.add(pv)
        await self.session.flush()
        # Reload with relationships so the caller can access .permission / .view_menu
        reloaded = await self.find_by_id(int(pv.id))
        return reloaded

    async def update(self, pv: Any, attributes: dict[str, Any]) -> Any:
        """Update a permission-view mapping in-place.

        Mirrors FAB's ``ModelRestApi.put`` for PermissionViewMenu.
        """
        for key, value in attributes.items():
            setattr(pv, key, value)
        await self.session.flush()
        reloaded = await self.find_by_id(int(pv.id))
        return reloaded

    async def delete(self, pv: Any) -> None:
        """Delete a single permission-view mapping.

        Mirrors FAB's ``ModelRestApi.delete`` for PermissionViewMenu.
        """
        await self.session.delete(pv)
        await self.session.flush()


class AsyncPermissionDAO:
    """Async DAO for FAB Permission model (ab_permission table).

    Read-only in the original FAB PermissionApi (include_route_methods
    only allows info, get, get_list).
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def search(
        self,
        filters: list[Any] | None = None,
        order_column: str = "id",
        order_direction: str = "asc",
        page: int = 0,
        page_size: int = 25,
    ) -> tuple[list[Any], int]:
        """Search permissions with pagination."""
        from sqlalchemy import func, select

        from superset.models.security import Permission

        stmt = select(Permission)
        count_stmt = select(func.count()).select_from(Permission)

        if filters:
            for f in filters:
                stmt = stmt.where(f)
                count_stmt = count_stmt.where(f)

        total = await self.session.scalar(count_stmt) or 0

        order_col = getattr(Permission, order_column, Permission.id)
        if order_direction == "desc":
            stmt = stmt.order_by(order_col.desc())
        else:
            stmt = stmt.order_by(order_col.asc())

        if page_size > 0:
            stmt = stmt.offset(page * page_size).limit(page_size)

        result = await self.session.execute(stmt)
        return list(result.scalars().all()), total

    async def find_by_id(self, permission_id: int) -> Any | None:
        """Get a single permission by ID."""
        from sqlalchemy import select

        from superset.models.security import Permission

        stmt = select(Permission).where(Permission.id == permission_id)
        result = await self.session.execute(stmt)
        return result.scalars().one_or_none()


class AsyncViewMenuDAO:
    """Async DAO for FAB ViewMenu model (ab_view_menu table).

    Full CRUD — mirrors the original FAB ViewMenuApi which exposes
    all ModelRestApi methods (get_list, get, info, post, put, delete).
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def search(
        self,
        filters: list[Any] | None = None,
        order_column: str = "id",
        order_direction: str = "asc",
        page: int = 0,
        page_size: int = 25,
    ) -> tuple[list[Any], int]:
        """Search view menus (resources) with pagination."""
        from sqlalchemy import func, select

        from superset.models.security import ViewMenu

        stmt = select(ViewMenu)
        count_stmt = select(func.count()).select_from(ViewMenu)

        if filters:
            for f in filters:
                stmt = stmt.where(f)
                count_stmt = count_stmt.where(f)

        total = await self.session.scalar(count_stmt) or 0

        order_col = getattr(ViewMenu, order_column, ViewMenu.id)
        if order_direction == "desc":
            stmt = stmt.order_by(order_col.desc())
        else:
            stmt = stmt.order_by(order_col.asc())

        if page_size > 0:
            stmt = stmt.offset(page * page_size).limit(page_size)

        result = await self.session.execute(stmt)
        return list(result.scalars().all()), total

    async def find_by_id(self, vm_id: int) -> Any | None:
        """Get a single view menu by ID."""
        from sqlalchemy import select

        from superset.models.security import ViewMenu

        stmt = select(ViewMenu).where(ViewMenu.id == vm_id)
        result = await self.session.execute(stmt)
        return result.scalars().one_or_none()

    async def create(self, attributes: dict[str, Any]) -> Any:
        """Create a new view menu (resource).

        Mirrors FAB's ``ModelRestApi.post`` for ViewMenu.
        """
        from superset.models.security import ViewMenu

        vm = ViewMenu(**attributes)
        self.session.add(vm)
        await self.session.flush()
        return vm

    async def update(self, vm: Any, attributes: dict[str, Any]) -> Any:
        """Update a view menu in-place.

        Mirrors FAB's ``ModelRestApi.put`` for ViewMenu.
        """
        for key, value in attributes.items():
            setattr(vm, key, value)
        await self.session.flush()
        return vm

    async def delete(self, vm: Any) -> None:
        """Delete a single view menu.

        Mirrors FAB's ``ModelRestApi.delete`` for ViewMenu.
        """
        await self.session.delete(vm)
        await self.session.flush()


class AsyncRegisterUserDAO:
    """Async DAO for FAB RegisterUser model (ab_register_user table).

    Mirrors the original ``UserRegistrationsRestAPI`` which uses
    ``SQLAInterface(RegisterUser)`` through FAB's ``ModelRestApi``.
    Provides full CRUD + search + distinct for pending registrations.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def search(
        self,
        filters: list[Any] | None = None,
        order_column: str = "id",
        order_direction: str = "asc",
        page: int = 0,
        page_size: int = 25,
    ) -> tuple[list[Any], int]:
        """Search pending registrations with filtering and pagination."""
        from sqlalchemy import func, select

        from superset.models.security import RegisterUser

        stmt = select(RegisterUser)
        count_stmt = select(func.count()).select_from(RegisterUser)

        if filters:
            for f in filters:
                stmt = stmt.where(f)
                count_stmt = count_stmt.where(f)

        total = await self.session.scalar(count_stmt) or 0

        order_col = getattr(RegisterUser, order_column, RegisterUser.id)
        if order_direction == "desc":
            stmt = stmt.order_by(order_col.desc())
        else:
            stmt = stmt.order_by(order_col.asc())

        if page_size > 0:
            stmt = stmt.offset(page * page_size).limit(page_size)

        result = await self.session.execute(stmt)
        return list(result.scalars().all()), total

    async def find_by_id(self, reg_id: int) -> Any | None:
        """Get a single registration by ID."""
        from sqlalchemy import select

        from superset.models.security import RegisterUser

        stmt = select(RegisterUser).where(RegisterUser.id == reg_id)
        result = await self.session.execute(stmt)
        return result.scalars().one_or_none()

    async def find_by_username(self, username: str) -> Any | None:
        """Find a registration by username (unique constraint check)."""
        from sqlalchemy import select

        from superset.models.security import RegisterUser

        stmt = select(RegisterUser).where(RegisterUser.username == username)
        result = await self.session.execute(stmt)
        return result.scalars().one_or_none()

    async def find_by_email(self, email: str) -> Any | None:
        """Find a registration by email."""
        from sqlalchemy import select

        from superset.models.security import RegisterUser

        stmt = select(RegisterUser).where(RegisterUser.email == email)
        result = await self.session.execute(stmt)
        return result.scalars().one_or_none()

    async def create(self, attributes: dict[str, Any]) -> Any:
        """Create a new registration request.

        Mirrors FAB's ``ModelRestApi.post_headless``: simply persists the
        data as-is via ``session.add() + session.flush()``. Password hashing
        and ``registration_hash`` generation only happen in the register
        FORM flow (``SecurityManager.add_register_user``), NOT in the
        REST API path.
        """
        from superset.models.security import RegisterUser

        reg = RegisterUser(**attributes)
        self.session.add(reg)
        await self.session.flush()
        return reg

    async def update(self, reg: Any, attributes: dict[str, Any]) -> Any:
        """Update a registration record in-place.

        Mirrors FAB's ``ModelRestApi.put`` for RegisterUser.
        """
        for key, value in attributes.items():
            setattr(reg, key, value)
        await self.session.flush()
        return reg

    async def delete(self, reg: Any) -> None:
        """Delete a single registration record.

        Mirrors FAB's ``SecurityManager.del_register_user``.
        """
        await self.session.delete(reg)
        await self.session.flush()

    async def get_distinct_values(
        self,
        column_name: str,
        page: int = 0,
        page_size: int = 25,
        filter_value: str = "",
    ) -> tuple[list[Any], int]:
        """Get distinct values for a column (for filter dropdowns).

        Mirrors FAB's ``ModelRestApi.distinct`` endpoint.
        """
        from sqlalchemy import func, select

        from superset.models.security import RegisterUser

        col = getattr(RegisterUser, column_name, None)
        if col is None:
            return [], 0

        base_stmt = select(func.distinct(col))

        if filter_value:
            from superset.utils import escape_like

            base_stmt = base_stmt.where(col.ilike(f"%{escape_like(filter_value)}%"))

        total = await self.session.scalar(
            select(func.count()).select_from(base_stmt.subquery())
        )
        stmt = base_stmt.offset(page * page_size).limit(page_size)
        result = await self.session.execute(stmt)
        values = result.scalars().all()

        return list(values), total or 0
