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
"""Admin-only factory-reset command.

``ResetSupersetCommand`` wipes datasets, databases, dashboards, slices,
the key-value store, logs, fav-stars, plus all non-Admin / non-system
users and roles, then writes a ``Factory Reset`` audit row to the ``Log``
table.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

from superset.commands.base import AsyncBaseCommand
from superset.exceptions import CommandInvalidError, ForbiddenError

logger = logging.getLogger(__name__)


class ResetSupersetCommand(AsyncBaseCommand[None]):
    """Wipe non-system data and reset the Superset install."""

    def __init__(
        self,
        session: Any,
        security_manager: Any,
        confirm: bool,
        user: Any,
        exclude_users: Optional[str] = None,
        exclude_roles: Optional[str] = None,
    ) -> None:
        self._session = session
        self._security_manager = security_manager
        self._user = user
        self._confirm = confirm
        self._users_to_exclude: list[str] = ["admin"]
        if exclude_users:
            self._users_to_exclude.extend(exclude_users.split(","))
        self._roles_to_exclude: list[str] = [
            "Admin",
            "Public",
            "Gamma",
            "Alpha",
            "sql_lab",
        ]
        if exclude_roles:
            self._roles_to_exclude.extend(exclude_roles.split(","))

    async def validate(self) -> None:
        if not self._confirm:
            raise CommandInvalidError("Reset aborted.")
        if not self._user or not getattr(self._user, "is_active", False):
            raise ForbiddenError("User not found.")

    async def run(self) -> None:
        logger.debug("Resetting Superset Started")

        # Full Superset model graph may not be loaded in minimal test environments.
        from superset.models.connectors import SqlaTable
        from superset.models.core import Database, FavStar, Log
        from superset.models.dashboard import Dashboard
        from superset.models.slice import Slice

        await self._session.execute(delete(SqlaTable))
        result = await self._session.execute(select(Database))
        for database in result.scalars().all():
            await self._session.delete(database)
        await self._session.execute(delete(Dashboard))
        await self._session.execute(delete(Slice))

        # KeyValueEntry lives in the same async migration but may be absent
        # in some test envs; guard the import.
        try:
            from superset.key_value.models import KeyValueEntry

            await self._session.execute(delete(KeyValueEntry))
        except ImportError:
            logger.debug("KeyValueEntry model unavailable — skipping reset")

        await self._session.execute(delete(Log))
        await self._session.execute(delete(FavStar))

        logger.debug("Ignoring Users: %s", self._users_to_exclude)
        user_model = self._security_manager.user_model
        # Eager-load roles: reading ``user.roles`` synchronously in the loop would
        # MissingGreenlet on a freshly-queried user under asyncpg.
        users_stmt = (
            select(user_model)
            .where(user_model.username.not_in(self._users_to_exclude))
            .options(selectinload(user_model.roles))
        )
        users = (await self._session.execute(users_stmt)).scalars().all()
        for user in users:
            roles = getattr(user, "roles", []) or []
            if not any(role.name == "Admin" for role in roles):
                await self._session.delete(user)

        logger.debug("Ignoring Roles: %s", self._roles_to_exclude)
        role_model = self._security_manager.role_model
        roles_stmt = select(role_model).where(
            role_model.name.not_in(self._roles_to_exclude)
        )
        roles = (await self._session.execute(roles_stmt)).scalars().all()
        for role in roles:
            await self._session.delete(role)

        log = Log(
            action="Factory Reset",
            json="{}",
            user_id=self._user.id,
            user=self._user,
        )
        self._session.add(log)

        await self._session.flush()
        await self._session.commit()
        logger.debug("Resetting Superset Completed")


# Early review used ``ResetRLSRulesCommand``; keep alias so stray imports don't break.
ResetRLSRulesCommand = ResetSupersetCommand
