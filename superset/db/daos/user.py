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

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import func, update
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


class AsyncUserDAO:
    """Async DAO for user operations.

    Does not inherit BaseAsyncDAO because the User model class depends
    on the upstream security manager configuration and is resolved
    at runtime rather than being a fixed class attribute.
    """

    def __init__(
        self,
        session: AsyncSession,
        user_model: type[Any] | None = None,
    ) -> None:
        self.session = session
        self._user_model = user_model

    @property
    def user_model(self) -> type[Any]:
        if self._user_model is None:
            from superset.models.security import User

            self._user_model = User
        return self._user_model

    async def get_by_id(self, user_id: int) -> Any | None:
        return await self.session.get(self.user_model, user_id)

    async def get_by_id_with_role_permissions(self, user_id: int) -> Any | None:
        """Get a user by ID with the full role/permission chain eager-loaded.

        Loads ``roles`` and ``groups -> roles`` down to each PermissionView's
        ``permission`` and ``view_menu`` so callers (e.g. ``get_my_roles``)
        can traverse the chain without triggering async lazy loads
        (``MissingGreenlet``).
        """
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        from superset.models.security import Group, PermissionView, Role

        stmt = (
            select(self.user_model)
            .where(self.user_model.id == user_id)
            .options(
                selectinload(self.user_model.roles)
                .selectinload(Role.permissions)
                .options(
                    selectinload(PermissionView.permission),
                    selectinload(PermissionView.view_menu),
                ),
                selectinload(self.user_model.groups)
                .selectinload(Group.roles)
                .selectinload(Role.permissions)
                .options(
                    selectinload(PermissionView.permission),
                    selectinload(PermissionView.view_menu),
                ),
            )
        )
        result = await self.session.execute(stmt)
        return result.scalars().unique().one_or_none()

    async def get_roles_with_permissions(self, role_ids: list[int]) -> list[Any]:
        """Load Role rows by id with the permission chain eager-loaded.

        Used for GuestUser callers of ``/me/roles/``: the middleware stores
        lightweight ``_CachedRole`` stubs without ``.permissions``, while
        GuestUser requires real ORM Roles with permissions fully loaded.
        """
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        from superset.models.security import PermissionView, Role

        stmt = (
            select(Role)
            .where(Role.id.in_(role_ids))
            .options(
                selectinload(Role.permissions).options(
                    selectinload(PermissionView.permission),
                    selectinload(PermissionView.view_menu),
                )
            )
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().unique().all())

    async def update_profile(
        self,
        user_id: int,
        attributes: dict[str, Any],
        hashed_password: str | None = None,
        changed_by_fk: int | None = None,
    ) -> bool:
        """Update user profile attributes and optionally set a new password.

        Side effects:
        - sets ``changed_on`` to ``datetime.now()``
        - sets ``changed_by_fk`` to the ID of the requesting user

        Returns True if the user was found and updated, False otherwise.
        """
        user = await self.get_by_id(user_id)
        if user is None:
            return False
        for attr, value in attributes.items():
            setattr(user, attr, value)
        if hashed_password is not None:
            user.password = hashed_password
        user.changed_on = datetime.now()
        if changed_by_fk is not None:
            user.changed_by_fk = changed_by_fk
        await self.session.flush()
        return True

    async def set_avatar_url(self, user: Any, avatar_url: str) -> None:
        try:
            await self.session.refresh(user, attribute_names=["extra_attributes"])
        except (InvalidRequestError, AttributeError):
            logger.debug("User model has no extra_attributes relationship")

        extra_attributes = getattr(user, "extra_attributes", [])
        if extra_attributes and hasattr(extra_attributes[0], "avatar_url"):
            extra_attributes[0].avatar_url = avatar_url
        else:
            try:
                from superset.models.user import UserAttribute

                attr = UserAttribute(
                    user_id=user.id,
                    avatar_url=avatar_url,
                )
                self.session.add(attr)
            except ImportError:
                logger.debug("UserAttribute model not available")

    async def update_login_count(self, user_id: int, login_count: int) -> None:
        """Set login_count; used to balance DB write timing during
        failed auth to prevent user enumeration."""
        User = self.user_model  # noqa: N806
        await self.session.execute(
            update(User).where(User.id == user_id).values(login_count=login_count)
        )

    async def increment_fail_login_count(self, user_id: int) -> None:
        User = self.user_model  # noqa: N806
        await self.session.execute(
            update(User)
            .where(User.id == user_id)
            .values(fail_login_count=func.coalesce(User.fail_login_count, 0) + 1)
        )

    async def record_successful_login(self, user_id: int) -> None:
        User = self.user_model  # noqa: N806
        await self.session.execute(
            update(User)
            .where(User.id == user_id)
            .values(
                last_login=datetime.now(),
                login_count=func.coalesce(User.login_count, 0) + 1,
                fail_login_count=0,
            )
        )
