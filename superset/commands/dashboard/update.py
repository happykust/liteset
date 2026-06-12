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

import asyncio
import logging
import textwrap
from typing import Any, TYPE_CHECKING

from superset.commands.base import AsyncBaseCommand
from superset.commands.utils import compute_owner_list, update_tags, validate_tags
from superset.exceptions import ObjectNotFoundError
from superset.tags.core import sync_owner_tags_after_update
from superset.tags.models import ObjectType
from superset.utils.feature_flags import feature_flag_manager

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

        # Validate tags — 1:1 with
        # ``superset_old/commands/dashboard/update.py::UpdateDashboardCommand.validate``
        # (lines 106-110). Checks the caller has permission to manage tags
        # and that every new tag id exists.  Raises ``TagForbiddenError``
        # (403) / ``TagNotFoundValidationError`` (422).
        # NOT gated on TAGGING_SYSTEM — upstream validates explicit tags in
        # the payload unconditionally; the flag only gates the implicit
        # owner/type tag event-listeners (see chart/update.py for the same).
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
        # NOT gated on TAGGING_SYSTEM — upstream applies explicit payload tags
        # unconditionally; only the implicit owner/type tag listeners are
        # flag-gated (sync_owner_tags_after_update below).
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

        # Resolve roles — use the shared ``populate_roles`` helper so a
        # missing role id raises ``RolesNotFoundValidationError`` (422)
        # instead of being silently dropped. Upstream's
        # superset_old/commands/dashboard/update.py:116 uses the same
        # helper for exactly this reason.
        if "roles" in self._data:
            from superset.commands.utils import populate_roles

            self._dashboard.roles = await populate_roles(
                self._dao.session, self._data["roles"]
            )

        await self._dao.session.flush()

        # Sync implicit owner: tags (async port of DashboardUpdater.after_update)
        # Only when TAGGING_SYSTEM is enabled — 1:1 with upstream where the
        # after_update event listener only fires when the flag is on.
        if feature_flag_manager.is_feature_enabled("TAGGING_SYSTEM"):
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

    @staticmethod
    def _extract_tab_ids(position_json: str) -> set[str]:
        """Parse a position JSON blob and return all ``TAB-*`` key IDs."""
        import json as _json  # noqa: TID251

        if not position_json:
            return set()
        try:
            return {k for k in _json.loads(position_json) if k.startswith("TAB-")}
        except (ValueError, TypeError):
            return set()

    async def _notify_deactivated_reports(self, reports_to_notify: list[Any]) -> None:
        """Eager-load report owners and email each owner about deactivation.

        1:1 with
        ``superset_old/commands/dashboard/update.py::send_deactivated_email_warning``
        (lines 142-187).  Runs SMTP in a thread to avoid blocking the loop.
        """
        from sqlalchemy import select as sa_select
        from sqlalchemy.orm import selectinload

        from superset.models.reports import ReportSchedule
        from superset.reports.notifications import _build_notification_config
        from superset.reports.notifications.email import _send_email_smtp

        report_ids = [r.id for r in reports_to_notify]
        stmt = (
            sa_select(ReportSchedule)
            .where(ReportSchedule.id.in_(report_ids))
            .options(selectinload(ReportSchedule.owners))
        )
        result = await self._dao.session.execute(stmt)
        loaded_reports = {r.id: r for r in result.scalars().all()}

        config = _build_notification_config()

        # Implicit string concatenation keeps the exact text produced by the
        # previous ``textwrap.dedent`` block while staying within the line limit.
        description = (
            "\n"
            "The dashboard tab used in this report has been deleted "
            "and your report has been deactivated.\n"
            "Please update your report settings to remove or change "
            "the tab used.\n"
        )
        html_content = textwrap.dedent(
            f"""
                <html>
                <head>
                    <style type="text/css">
                    table, th, td {{
                        border-collapse: collapse;
                        border-color: rgb(200, 212, 227);
                        color: rgb(42, 63, 95);
                        padding: 4px 8px;
                    }}
                    .image{{
                        margin-bottom: 18px;
                    }}
                    </style>
                </head>
                <body>
                    <div>{description}</div>
                    <br>
                </body>
                </html>
                """
        )

        for report in reports_to_notify:
            loaded = loaded_reports.get(report.id, report)
            for report_owner in getattr(loaded, "owners", []):
                email = getattr(report_owner, "email", None)
                if email:
                    subject = f"[Report: {report.name}] Deactivated"
                    await asyncio.to_thread(
                        _send_email_smtp,
                        email,
                        subject,
                        html_content,
                        config,
                    )

    async def _process_tab_diff(self, old_position_json: str | None) -> None:
        """Detect deleted tabs and deactivate report schedules anchored to them.

        Compares *old_position_json* (captured before the model was mutated)
        with the new value from ``self._data``.  Any tab IDs (keys starting
        with ``TAB-``) present in the old layout but absent from the new one
        are considered deleted.  Report schedules whose ``extra`` JSON
        references a deleted tab's anchor are deactivated (``active=False``).
        """
        assert self._dashboard is not None

        old_position = old_position_json or ""
        new_position = self._data.get("position_json", "")

        # Mirror the original guard: `if position_json and current_tabs` in
        # superset_old/commands/dashboard/update.py:127.  When new_position is
        # an empty string the caller supplied no real layout, so treat it as
        # "no change" and do not deactivate any reports.
        if not new_position:
            return

        old_tabs = self._extract_tab_ids(old_position)
        new_tabs = self._extract_tab_ids(new_position)

        deleted_tabs = old_tabs - new_tabs
        if not deleted_tabs:
            return

        # 1:1 with ``superset_old/commands/dashboard/update.py::process_tab_diff``:
        # for each deleted tab, find reports whose ``extra_json`` *contains*
        # the tab id (``ReportScheduleDAO.find_by_extra_metadata`` →
        # ``extra_json LIKE '%tab%'``) and deactivate them. The previous port
        # loaded only this dashboard's reports and checked ``extra["anchor"]``
        # — but the anchor actually lives at ``extra["dashboard"]["anchor"]``
        # as a JSON-encoded LIST (see ``_validate_report_extra``), so the
        # top-level string check never matched → reports were never
        # deactivated. The substring search matches the tab id wherever it
        # sits in the metadata (anchor list, activeTabs, …), exactly upstream.
        from superset.db.daos.report import AsyncReportScheduleDAO

        report_dao = AsyncReportScheduleDAO(session=self._dao.session)
        reports_to_notify: list[Any] = []
        # NO per-report dedup across tabs — the original loops every deleted
        # tab independently and notifies owners once per MATCHED TAB
        # (superset_old/commands/dashboard/update.py:142-187); re-setting
        # ``active = False`` is idempotent.
        for tab in deleted_tabs:
            for report in await report_dao.find_by_extra_metadata(tab):
                report.active = False  # type: ignore[assignment]
                logger.info(
                    "Deactivated report schedule %s (id=%s) — tab '%s' was "
                    "removed from dashboard %s",
                    report.name,
                    report.id,
                    tab,
                    self._dashboard_id,
                )
                reports_to_notify.append(report)

        if reports_to_notify:
            # 1:1 with
            # ``superset_old/commands/dashboard/update.py``
            # ``::send_deactivated_email_warning`` (lines 142-187):
            # email each report owner when the report is deactivated
            # due to a deleted tab.  Run in a thread so SMTP (sync
            # smtplib) does not block the event-loop.
            await self._notify_deactivated_reports(reports_to_notify)


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

    @staticmethod
    def _apply_filter_updates(
        native_filter_configuration: list[dict[str, Any]],
        deleted: list[str],
        modified: list[dict[str, Any]],
        reordered_filter_ids: list[str],
    ) -> list[dict[str, Any]]:
        """Merge delete/modify/new-filter operations into the updated list.

        Mirrors the mutation logic in the original
        ``UpdateDashboardNativeFiltersCommand.run`` (sync port).
        """
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

        return updated_configuration

    @staticmethod
    def _reorder_and_clean_filters(
        updated_configuration: list[dict[str, Any]],
        reordered_filter_ids: list[str],
        metadata: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Reorder filters and strip legacy ``show_native_filters`` key.

        1:1 with ``DashboardJSONMetadataSchema.@pre_load
        remove_show_native_filters`` cleanup (apache/superset#23228).
        """
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

        # Strip the legacy flag from top-level metadata and each entry.
        metadata.pop("show_native_filters", None)
        for filter_conf in updated_configuration:
            if isinstance(filter_conf, dict):
                filter_conf.pop("show_native_filters", None)

        return updated_configuration

    async def run(self) -> list[dict[str, Any]]:
        assert self._dashboard is not None
        import json  # noqa: TID251

        metadata: dict[str, Any] = {}
        if self._dashboard.json_metadata:
            try:
                parsed = json.loads(self._dashboard.json_metadata)
                # Coerce a non-object value (imported/legacy ``[1,2]`` / ``"s"``)
                # to {} so ``metadata.get(...)`` below doesn't raise → 500.
                if isinstance(parsed, dict):
                    metadata = parsed
            except (json.JSONDecodeError, TypeError):
                metadata = {}

        native_filter_configuration: list[dict[str, Any]] = metadata.get(
            "native_filter_configuration", []
        )
        reordered_filter_ids: list[str] = list(self._data.get("reordered") or [])
        deleted: list[str] = list(self._data.get("deleted") or [])
        modified: list[dict[str, Any]] = list(self._data.get("modified") or [])

        updated_configuration = self._apply_filter_updates(
            native_filter_configuration,
            deleted,
            modified,
            reordered_filter_ids,
        )
        updated_configuration = self._reorder_and_clean_filters(
            updated_configuration,
            reordered_filter_ids,
            metadata,
        )

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

        # 1:1 with
        # ``superset_old/commands/dashboard/update.py``
        # ``UpdateDashboardColorsConfigCommand.run`` (lines 223-230):
        # when ``mark_updated=False``, capture ``changed_on``
        # *before* the color update is flushed.  The flush triggers SA's
        # ``onupdate=datetime.now`` on the ``changed_on`` column; then we
        # restore the captured value and flush again so the outer transaction
        # commits the original timestamp.  Without the intermediate flush the
        # SA ``onupdate`` fires during the final commit and overwrites the
        # restored value.
        original_changed_on = (
            self._dashboard.changed_on if not self._mark_updated else None
        )

        await self._dao.update_colors_config(
            self._dashboard, self._data, mark_updated=self._mark_updated
        )
        # First flush: persists json_metadata; SA onupdate stamps changed_on=now()
        await self._dao.session.flush()

        if not self._mark_updated and original_changed_on is not None:
            # Restore the original timestamp (mirrors the intermediate
            # db.session.commit() + reassignment in the original synchronous
            # implementation).
            self._dashboard.changed_on = original_changed_on  # type: ignore[assignment]
            # Second flush: writes the restored changed_on value.
            await self._dao.session.flush()

        return self._dashboard
