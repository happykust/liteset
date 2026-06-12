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
"""Async port of ``superset_old/commands/dashboard/create.py``."""

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
                # Field-keyed 422 — 1:1 with upstream
                # ``DashboardInvalidError(exceptions=[DashboardSlugExists
                # ValidationError()])`` → ``{"slug": ["Must be unique"]}``.
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

        # Resolve owners + roles — refresh first to avoid MissingGreenlet
        # on the lazy-loaded collections in async context. Both are M2M
        # and the post-flush ``dashboard.<rel> = [...]`` assignment
        # otherwise triggers SA's diff-load (sync IO → asyncpg crash).
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

        # Resolve roles — 1:1 with upstream
        # ``superset_old/commands/dashboard/create.py`` which calls
        # ``populate_roles(roles_ids)`` (raises ``RolesNotFoundValidation
        # Error`` 422 on a missing id). Previous loop silently dropped
        # invalid ids, allowing a POST with ``roles=[99999]`` to create
        # a dashboard with no roles attached at all.
        role_ids = self._data.get("roles")
        if role_ids:
            from superset.commands.utils import populate_roles

            dashboard.roles = await populate_roles(self._dao.session, role_ids)

        # Add implicit type: and owner: tags — 1:1 with
        # ``DashboardUpdater.after_insert`` which only fires when the
        # TAGGING_SYSTEM feature flag is enabled (see ``superset_old/app.py:158``).
        if feature_flag_manager.is_feature_enabled("TAGGING_SYSTEM"):
            owner_ids = resolved_owner_ids
            await add_implicit_tags_after_insert(
                self._dao.session, "dashboard", dashboard.id, owner_ids
            )

        return dashboard
