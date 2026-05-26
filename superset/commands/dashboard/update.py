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
"""Async port of ``superset_old/commands/dashboard/update.py``."""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from superset.commands.base import AsyncBaseCommand
from superset.commands.utils import compute_owner_list, update_tags, validate_tags
from superset.exceptions import CommandInvalidError, ObjectNotFoundError
from superset.tags.core import sync_owner_tags_after_update
from superset.tags.models import ObjectType

if TYPE_CHECKING:
    from superset.db.daos.dashboard import AsyncDashboardDAO
    from superset.models.dashboard import Dashboard

logger = logging.getLogger(__name__)


class UpdateDashboardCommand(AsyncBaseCommand["Dashboard"]):
    def __init__(
        self,
        dao: AsyncDashboardDAO,
        dashboard_id: int,
        data: dict[str, Any],
        user_id: int | None = None,
        security_manager: Any | None = None,
    ) -> None:
        self._dao = dao
        self._dashboard_id = dashboard_id
        self._data = data
        self._user_id = user_id
        self._security_manager = security_manager
        self._dashboard: Any | None = None

    async def validate(self) -> None:
        # Eager-load the M2M relationships that are (re)assigned in
        # ``run()``. Without this, the `.owners = [...]` assignment
        # triggers a lazy reload of the existing values, which crashes
        # under asyncpg with ``MissingGreenlet``.
        from sqlalchemy.orm import selectinload

        from superset.models.dashboard import Dashboard

        self._dashboard = await self._dao.find_by_id_with_options(
            self._dashboard_id,
            options=[
                selectinload(Dashboard.owners),
                selectinload(Dashboard.roles),
                selectinload(Dashboard.tags),
            ],
        )
        if not self._dashboard:
            raise ObjectNotFoundError("Dashboard", self._dashboard_id)
        if self._security_manager is not None:
            await self._security_manager.raise_for_ownership(
                self._dashboard, self._user_id
            )
        slug = self._data.get("slug")
        if slug is not None:
            is_unique = await self._dao.validate_update_slug_uniqueness(
                self._dashboard_id, slug
            )
            if not is_unique:
                raise CommandInvalidError(f"slug '{slug}' already exists")

        # Validate tags — 1:1 with
        # ``superset_old/commands/dashboard/update.py::UpdateDashboardCommand.validate``
        # (lines 106-110). Checks the caller has permission to manage tags
        # and that every new tag id exists.  Raises ``TagForbiddenError``
        # (403) / ``TagNotFoundValidationError`` (422).
        if self._security_manager is not None:
            user = (
                await self._security_manager.find_user_by_id(self._user_id)
                if self._user_id is not None
                else None
            )
            await validate_tags(
                ObjectType.dashboard,
                list(self._dashboard.tags),
                self._data.get("tags"),
                self._security_manager,
                user,
            )

    async def run(self) -> "Dashboard":  # noqa: C901
        assert self._dashboard is not None

        # Capture the old position_json before mutation so that
        # _process_tab_diff can compute the tab diff correctly.
        old_position_json = getattr(self._dashboard, "position_json", None)

        for key, value in self._data.items():
            if key in ("owners", "roles", "tags"):
                continue
            if hasattr(self._dashboard, key):
                setattr(self._dashboard, key, value)
        if self._user_id is not None:
            self._dashboard.changed_by_fk = self._user_id

        # Update tags — 1:1 with
        # ``superset_old/commands/dashboard/update.py::UpdateDashboardCommand.run``
        # (lines 64-65): apply the add/remove of custom tags on the dashboard.
        tag_ids = self._data.get("tags")
        if tag_ids is not None:
            await update_tags(
                ObjectType.dashboard,
                self._dashboard.id,
                list(self._dashboard.tags),
                tag_ids,
                self._dao.session,
            )

        if "position_json" in self._data:
            await self._process_tab_diff(old_position_json)

        # Synchronise filter scopes, color maps and shared-label colors
        # when json_metadata is updated.
        if "json_metadata" in self._data:
            await self._dao.set_dash_metadata(self._dashboard, self._data)

        # Resolve owners — preserve existing when ``owners`` not in payload,
        # auto-add caller when non-admin, raise on unknown ids.
        if "owners" in self._data and self._security_manager is not None:
            await self._dao.session.refresh(self._dashboard, ["owners"])
            self._dashboard.owners = await compute_owner_list(
                self._security_manager,
                self._user_id,
                list(self._dashboard.owners),
                self._data.get("owners"),
            )

        # Resolve roles
        role_ids = self._data.get("roles", [])
        if role_ids and self._security_manager is not None:
            roles = []
            for rid in role_ids:
                role = await self._security_manager.find_role_by_id(rid)
                if role:
                    roles.append(role)
            self._dashboard.roles = roles

        await self._dao.session.flush()

        # Sync implicit owner: tags (async port of DashboardUpdater.after_update)
        await self._dao.session.refresh(self._dashboard, ["owners"])
        owner_ids = (
            [o.id for o in self._dashboard.owners]
            if hasattr(self._dashboard, "owners")
            else []
        )
        await sync_owner_tags_after_update(
            self._dao.session, "dashboard", self._dashboard.id, owner_ids
        )

        return self._dashboard

    async def _process_tab_diff(self, old_position_json: str | None) -> None:
        """Detect deleted tabs and deactivate report schedules anchored to them.

        Compares *old_position_json* (captured before the model was mutated)
        with the new value from ``self._data``.  Any tab IDs (keys starting
        with ``TAB-``) present in the old layout but absent from the new one
        are considered deleted.  Report schedules whose ``extra`` JSON
        references a deleted tab's anchor are deactivated (``active=False``).
        """
        import json as _json  # noqa: TID251

        assert self._dashboard is not None

        old_position = old_position_json or ""
        new_position = self._data.get("position_json", "")

        try:
            old_tabs = (
                {k for k in _json.loads(old_position) if k.startswith("TAB-")}
                if old_position
                else set()
            )
        except (ValueError, TypeError):
            old_tabs = set()

        try:
            new_tabs = (
                {k for k in _json.loads(new_position) if k.startswith("TAB-")}
                if new_position
                else set()
            )
        except (ValueError, TypeError):
            new_tabs = set()

        deleted_tabs = old_tabs - new_tabs
        if not deleted_tabs:
            return

        # Lazy-import the report DAO to avoid circular dependencies
        from superset.db.daos.report import AsyncReportScheduleDAO

        report_dao = AsyncReportScheduleDAO(session=self._dao.session)
        reports = await report_dao.find_by_dashboard_id(self._dashboard_id)

        for report in reports:
            extra_raw = getattr(report, "extra", None) or "{}"
            try:
                extra = _json.loads(extra_raw)
            except (ValueError, TypeError):
                continue

            anchor = extra.get("anchor")
            if anchor and anchor in deleted_tabs:
                report.active = False  # type: ignore[assignment]
                logger.info(
                    "Deactivated report schedule %s (id=%s) — anchor tab "
                    "'%s' was removed from dashboard %s",
                    report.name,
                    report.id,
                    anchor,
                    self._dashboard_id,
                )


class UpdateDashboardFiltersCommand(AsyncBaseCommand[list[dict[str, Any]]]):
    def __init__(
        self,
        dao: AsyncDashboardDAO,
        dashboard_id: int,
        data: dict[str, Any],
        security_manager: Any | None = None,
        user_id: int | None = None,
    ) -> None:
        self._dao = dao
        self._dashboard_id = dashboard_id
        self._data = data
        self._security_manager = security_manager
        self._user_id = user_id
        self._dashboard: Any | None = None

    async def validate(self) -> None:
        self._dashboard = await self._dao.find_by_id(self._dashboard_id)
        if not self._dashboard:
            raise ObjectNotFoundError("Dashboard", self._dashboard_id)
        if self._security_manager is not None:
            await self._security_manager.raise_for_ownership(
                self._dashboard, self._user_id
            )

    async def run(self) -> list[dict[str, Any]]:
        assert self._dashboard is not None
        import json  # noqa: TID251

        metadata: dict[str, Any] = {}
        if self._dashboard.json_metadata:
            try:
                metadata = json.loads(self._dashboard.json_metadata)
            except (json.JSONDecodeError, TypeError):
                metadata = {}

        native_filter_configuration: list[dict[str, Any]] = metadata.get(
            "native_filter_configuration", []
        )
        reordered_filter_ids: list[str] = list(self._data.get("reordered") or [])
        deleted: list[str] = list(self._data.get("deleted") or [])
        modified: list[dict[str, Any]] = list(self._data.get("modified") or [])

        updated_configuration: list[dict[str, Any]] = []
        # Modify / Delete existing filters
        for conf in native_filter_configuration:
            if any(f == conf.get("id") for f in deleted):
                continue
            modified_filter = next(
                (f for f in modified if f.get("id") == conf.get("id")),
                None,
            )
            if modified_filter is not None:
                updated_configuration.append(modified_filter)
            else:
                updated_configuration.append(conf)

        # Append new filters (present in `modified` but not in the existing config)
        for new_filter in modified:
            new_filter_id = new_filter.get("id")
            if new_filter_id not in [f.get("id") for f in updated_configuration]:
                updated_configuration.append(new_filter)
                if reordered_filter_ids and new_filter_id not in reordered_filter_ids:
                    reordered_filter_ids.append(new_filter_id)

        # Reorder filters
        if reordered_filter_ids:
            filter_map = {
                filter_config["id"]: filter_config
                for filter_config in updated_configuration
            }
            updated_configuration = [
                filter_map[filter_id]
                for filter_id in reordered_filter_ids
                if filter_id in filter_map
            ]

        # 1:1 with the original ``DashboardJSONMetadataSchema.@pre_load
        # remove_show_native_filters`` cleanup (apache/superset#23228) —
        # strip the legacy ``show_native_filters`` flag from the top-level
        # metadata blob and from each ``native_filter_configuration``
        # entry so it never re-enters storage on a filters update.
        metadata.pop("show_native_filters", None)
        for filter_conf in updated_configuration:
            if isinstance(filter_conf, dict):
                filter_conf.pop("show_native_filters", None)

        metadata["native_filter_configuration"] = updated_configuration
        self._dashboard.json_metadata = json.dumps(metadata)
        await self._dao.session.flush()
        return updated_configuration


class UpdateDashboardColorsCommand(AsyncBaseCommand["Dashboard"]):
    def __init__(
        self,
        dao: AsyncDashboardDAO,
        dashboard_id: int,
        data: dict[str, Any],
        security_manager: Any | None = None,
        user_id: int | None = None,
        mark_updated: bool = True,
    ) -> None:
        self._dao = dao
        self._dashboard_id = dashboard_id
        self._data = data
        self._security_manager = security_manager
        self._user_id = user_id
        self._mark_updated = mark_updated
        self._dashboard: Any | None = None

    async def validate(self) -> None:
        self._dashboard = await self._dao.find_by_id(self._dashboard_id)
        if not self._dashboard:
            raise ObjectNotFoundError("Dashboard", self._dashboard_id)
        if self._security_manager is not None:
            await self._security_manager.raise_for_ownership(
                self._dashboard, self._user_id
            )

    async def run(self) -> "Dashboard":
        assert self._dashboard is not None
        await self._dao.update_colors_config(
            self._dashboard, self._data, mark_updated=self._mark_updated
        )
        await self._dao.session.flush()
        return self._dashboard
