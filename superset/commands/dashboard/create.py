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
# mypy: ignore-errors
"""Dashboard create command."""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from superset.commands.base import AsyncBaseCommand
from superset.commands.utils import populate_owner_list
from superset.tags.core import add_implicit_tags_after_insert
from superset.utils.feature_flags import feature_flag_manager

if TYPE_CHECKING:
    from superset.db.daos.dashboard import AsyncDashboardDAO
    from superset.models.dashboard import Dashboard

logger = logging.getLogger(__name__)


class CreateDashboardCommand(AsyncBaseCommand["Dashboard"]):
    def __init__(
        self,
        dao: AsyncDashboardDAO,
        data: dict[str, Any],
        user_id: int | None = None,
        security_manager: Any | None = None,
    ) -> None:
        self._dao = dao
        self._data = data
        self._user_id = user_id
        self._security_manager = security_manager

    async def validate(self) -> None:
        slug = self._data.get("slug")
        if slug:
            is_unique = await self._dao.validate_slug_uniqueness(slug)
            if not is_unique:
                from superset.commands.dashboard.exceptions import (
                    DashboardInvalidError,
                    DashboardSlugExistsValidationError,
                )

                raise DashboardInvalidError(
                    exceptions=[DashboardSlugExistsValidationError()]
                )

    async def run(self) -> "Dashboard":
        from superset.models.dashboard import Dashboard

        dashboard = Dashboard(
            **{
                k: v
                for k, v in self._data.items()
                if k not in ("owners", "roles", "tags")
            }
        )
        if self._user_id is not None:
            dashboard.created_by_fk = self._user_id
            dashboard.changed_by_fk = self._user_id
        self._dao.session.add(dashboard)
        await self._dao.session.flush()

        # Refresh M2M collections before assignment — without this SA fires a
        # sync lazy-load on the async session (MissingGreenlet / asyncpg crash).
        await self._dao.session.refresh(dashboard, ["owners", "roles"])
        resolved_owner_ids: list[int] = []
        if self._security_manager is not None:
            owners = await populate_owner_list(
                self._security_manager,
                self._user_id,
                self._data.get("owners"),
                default_to_user=True,
            )
            dashboard.owners = owners
            resolved_owner_ids = [o.id for o in owners]

        # populate_roles raises RolesNotFoundValidationError (422) on unknown ids
        # rather than silently creating a dashboard with no roles attached.
        role_ids = self._data.get("roles")
        if role_ids:
            from superset.commands.utils import populate_roles

            dashboard.roles = await populate_roles(self._dao.session, role_ids)

        if feature_flag_manager.is_feature_enabled("TAGGING_SYSTEM"):
            owner_ids = resolved_owner_ids
            await add_implicit_tags_after_insert(
                self._dao.session, "dashboard", dashboard.id, owner_ids
            )

        return dashboard
