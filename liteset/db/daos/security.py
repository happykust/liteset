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

from liteset.db.base_dao import BaseAsyncDAO
from liteset.models.connectors import RowLevelSecurityFilter


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
        from liteset.models.security import Role

        from sqlalchemy import func, select
        from sqlalchemy.orm import selectinload

        # Base query
        stmt = select(Role)
        count_stmt = select(func.count()).select_from(Role)

        # Apply name filter (case-insensitive substring match)
        if name_filter:
            from liteset.utils import escape_like

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
            selectinload(Role.user),
        )

        # Pagination
        if page_size > 0:
            stmt = stmt.offset(page * page_size).limit(page_size)

        result = await self.session.execute(stmt)
        roles = list(result.scalars().unique().all())

        return roles, total

    async def find_by_id(self, role_id: int) -> Any | None:
        """Get a single role by ID with eager-loaded relationships."""
        from liteset.models.security import Role

        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        stmt = (
            select(Role)
            .where(Role.id == role_id)
            .options(
                selectinload(Role.permissions),
                selectinload(Role.user),
            )
        )
        result = await self.session.execute(stmt)
        return result.scalars().unique().one_or_none()

    async def create(self, attributes: dict[str, Any]) -> Any:
        """Create a new role."""
        from liteset.models.security import Role

        role = Role(**attributes)
        self.session.add(role)
        await self.session.flush()
        return role

    async def update(self, role: Any, attributes: dict[str, Any]) -> Any:
        """Update role attributes in-place."""
        for key, value in attributes.items():
            setattr(role, key, value)
        await self.session.flush()
        return role

    async def delete(self, role: Any) -> None:
        """Delete a single role."""
        await self.session.delete(role)
        await self.session.flush()

    async def get_permissions(self, role_id: int) -> list[Any]:
        """Get all permissions for a role, with permission and view_menu loaded."""
        from liteset.models.security import PermissionView, ab_permission_view_role

        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

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
