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
"""Async Data Access Object for FAB security tables.

Queries ab_user, ab_role, ab_permission, ab_view_menu,
ab_permission_view, ab_user_role, and ab_permission_view_role
using AsyncSession. Models are imported from superset.models.security,
or injected via constructor for testing.

mypy: FAB models are injected as ``type`` (no generic parameter), so
attribute access (.id, .name, .roles, etc.) triggers attr-defined errors.
These are safe — the actual models always have these attributes.
"""

# mypy: disable-error-code="attr-defined, var-annotated"
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

logger = logging.getLogger(__name__)


class AsyncSecurityDAO:
    """Async queries against FAB security tables.

    Accepts model classes via constructor to avoid hard dependency
    on flask_appbuilder at import time. Production code resolves
    models from FAB; tests inject lightweight fakes.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        user_model: type | None = None,
        role_model: type | None = None,
        permission_model: type | None = None,
        view_menu_model: type | None = None,
        permission_view_model: type | None = None,
    ) -> None:
        self.session = session
        self._user_model = user_model
        self._role_model = role_model
        self._permission_model = permission_model
        self._view_menu_model = view_menu_model
        self._permission_view_model = permission_view_model

    @property
    def user_model(self) -> type:
        if self._user_model is None:
            from superset.models.security import User

            self._user_model = User
        return self._user_model

    @property
    def role_model(self) -> type:
        if self._role_model is None:
            from superset.models.security import Role

            self._role_model = Role
        return self._role_model

    @property
    def permission_model(self) -> type:
        if self._permission_model is None:
            from superset.models.security import Permission

            self._permission_model = Permission
        return self._permission_model

    @property
    def view_menu_model(self) -> type:
        if self._view_menu_model is None:
            from superset.models.security import ViewMenu

            self._view_menu_model = ViewMenu
        return self._view_menu_model

    @property
    def permission_view_model(self) -> type:
        if self._permission_view_model is None:
            from superset.models.security import PermissionView

            self._permission_view_model = PermissionView
        return self._permission_view_model

    async def get_user_by_id(self, user_id: int) -> Any | None:
        """Load user by primary key with roles eagerly loaded."""
        stmt = (
            select(self.user_model)
            .where(self.user_model.id == user_id)
            .options(selectinload(self.user_model.roles))
        )
        result = await self.session.execute(stmt)
        return result.scalars().one_or_none()

    async def get_user_by_username(self, username: str) -> Any | None:
        """Load user by username with roles eagerly loaded."""
        stmt = (
            select(self.user_model)
            .where(self.user_model.username == username)
            .options(selectinload(self.user_model.roles))
        )
        result = await self.session.execute(stmt)
        return result.scalars().one_or_none()

    async def get_user_by_email(self, email: str) -> Any | None:
        """Load user by email with roles eagerly loaded."""
        stmt = (
            select(self.user_model)
            .where(self.user_model.email == email)
            .options(selectinload(self.user_model.roles))
        )
        result = await self.session.execute(stmt)
        return result.scalars().one_or_none()

    async def get_first_user(self) -> Any | None:
        """Return the first user row (by id). Used for timing balance."""
        stmt = select(self.user_model).order_by(self.user_model.id).limit(1)
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def get_user_roles(self, user: Any) -> list[Any]:
        """Get all roles for a user. Assumes roles are already loaded."""
        return list(getattr(user, "roles", []))

    async def get_role_permissions(self, role_id: int) -> list[Any]:
        """Get all PermissionView entries for a role."""
        PV = self.permission_view_model  # noqa: N806
        Role = self.role_model  # noqa: N806
        # Join through the *forward* ``Role.permissions`` relationship —
        # the FAB-faithful ``PermissionView`` model only carries the
        # singular ``role`` backref, so ``PV.roles`` does not exist
        # (FAB's ``exist_permission_on_roles`` joins through the
        # association table for the same reason).
        stmt = (
            select(PV)
            .select_from(Role)
            .join(Role.permissions)
            .where(Role.id == role_id)
            .options(
                selectinload(PV.permission),
                selectinload(PV.view_menu),
            )
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def has_permission_view(
        self,
        permission_name: str,
        view_menu_name: str,
        *,
        role_ids: list[int],
    ) -> bool:
        """Check if any of the given roles have a specific permission on a view."""
        PV = self.permission_view_model  # noqa: N806
        P = self.permission_model  # noqa: N806
        VM = self.view_menu_model  # noqa: N806
        Role = self.role_model  # noqa: N806
        stmt = (
            select(PV.id)
            .select_from(Role)
            .join(Role.permissions)
            .join(PV.permission)
            .join(PV.view_menu)
            .where(
                P.name == permission_name,
                VM.name == view_menu_name,
                Role.id.in_(role_ids),
            )
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalars().first() is not None

    async def get_role_by_name(self, name: str) -> Any | None:
        """Load a role by name."""
        stmt = select(self.role_model).where(self.role_model.name == name)
        result = await self.session.execute(stmt)
        return result.scalars().one_or_none()

    async def get_permissions_for_role_name(
        self, role_name: str
    ) -> set[tuple[str, str]]:
        """Get all (permission_name, view_menu_name) tuples for a role by name.

        Single query: role -> permission_views -> permission + view_menu.
        """
        Role = self.role_model  # noqa: N806
        PV = self.permission_view_model  # noqa: N806
        P = self.permission_model  # noqa: N806
        VM = self.view_menu_model  # noqa: N806

        stmt = (
            select(P.name, VM.name)
            .select_from(Role)
            .join(Role.permissions)
            .join(PV.permission)
            .join(PV.view_menu)
            .where(Role.name == role_name)
            .distinct()
        )
        result = await self.session.execute(stmt)
        return {(row[0], row[1]) for row in result.all()}

    async def get_all_permissions_for_user(self, user_id: int) -> set[tuple[str, str]]:
        """Get all (permission_name, view_menu_name) tuples for a user.

        Traverses: user -> roles -> permission_views -> permission + view_menu.
        Returns a set of (permission_name, view_menu_name) tuples.
        """
        User = self.user_model  # noqa: N806
        Role = self.role_model  # noqa: N806
        PV = self.permission_view_model  # noqa: N806
        P = self.permission_model  # noqa: N806
        VM = self.view_menu_model  # noqa: N806

        stmt = (
            select(P.name, VM.name)
            .select_from(User)
            .join(User.roles)
            .join(Role.permissions)
            .join(PV.permission)
            .join(PV.view_menu)
            .where(User.id == user_id)
            .distinct()
        )
        result = await self.session.execute(stmt)
        return {(row[0], row[1]) for row in result.all()}

    async def get_user_groups(
        self,
        user_id: int,
    ) -> list[Any]:
        """Get groups for a user via ab_user_group."""
        stmt = text(
            "SELECT g.id, g.name FROM ab_group g "
            "JOIN ab_user_group ug ON ug.group_id = g.id "
            "WHERE ug.user_id = :user_id"
        )
        result = await self.session.execute(
            stmt,
            {"user_id": user_id},
        )
        return list(result.all())

    async def get_group_roles(
        self,
        group_id: int,
    ) -> list[Any]:
        """Get roles for a group via ab_group_role."""
        stmt = text(
            "SELECT r.id, r.name FROM ab_role r "
            "JOIN ab_group_role gr ON gr.role_id = r.id "
            "WHERE gr.group_id = :group_id"
        )
        result = await self.session.execute(
            stmt,
            {"group_id": group_id},
        )
        return list(result.all())

    async def get_group_permissions(
        self,
        user_id: int,
    ) -> set[tuple[str, str]]:
        """Get permissions inherited via groups."""
        stmt = text(
            "SELECT DISTINCT p.name, vm.name "
            "FROM ab_user_group ug "
            "JOIN ab_group_role gr "
            "  ON gr.group_id = ug.group_id "
            "JOIN ab_permission_view_role pvr "
            "  ON pvr.role_id = gr.role_id "
            "JOIN ab_permission_view pv "
            "  ON pv.id = pvr.permission_view_id "
            "JOIN ab_permission p "
            "  ON p.id = pv.permission_id "
            "JOIN ab_view_menu vm "
            "  ON vm.id = pv.view_menu_id "
            "WHERE ug.user_id = :user_id"
        )
        result = await self.session.execute(
            stmt,
            {"user_id": user_id},
        )
        return {(row[0], row[1]) for row in result.all()}

    async def get_all_permissions_for_user_with_groups(
        self,
        user_id: int,
    ) -> set[tuple[str, str]]:
        """Get all permissions for a user, including group-inherited."""
        direct = await self.get_all_permissions_for_user(
            user_id,
        )
        group = await self.get_group_permissions(user_id)
        return direct | group
