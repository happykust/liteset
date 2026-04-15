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
"""Dashboard command classes — business logic for dashboard CRUD and operations."""

from __future__ import annotations

import io
import json as _json
import logging
import random
import string
from typing import Any, TYPE_CHECKING
from uuid import UUID as _UUID

import yaml  # type: ignore[import-untyped]

from superset.commands.base import AsyncBaseCommand
from superset.commands.chart import (
    _get_filename,
    _import_chart,
    _import_database,
    _import_dataset,
    EXPORT_VERSION,
    update_chart_config_dataset,
)
from superset.exceptions import (
    CommandInvalidError,
    ImportFailedError,
    ObjectNotFoundError,
)
from superset.importexport.export_base import AsyncExportModelsCommand
from superset.importexport.import_base import AsyncImportModelsCommand
from superset.tags.core import (
    add_implicit_tags_after_insert,
    delete_tagged_objects,
    sync_owner_tags_after_update,
)
from superset.utils import mask_uri_password

logger = logging.getLogger(__name__)

# JSON keys that are stored as JSON strings in the DB but exported as dicts
_JSON_KEYS_EXPORT = {"position_json": "position", "json_metadata": "metadata"}
_JSON_KEYS_IMPORT = {"position": "position_json", "metadata": "json_metadata"}

DEFAULT_CHART_HEIGHT = 50
DEFAULT_CHART_WIDTH = 4


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from superset.db.daos.dashboard import AsyncDashboardDAO, AsyncEmbeddedDashboardDAO
    from superset.models.dashboard import Dashboard
    from superset.models.embedded_dashboard import EmbeddedDashboard


# ---------------------------------------------------------------------------
# Dashboard import/export helper functions (ported 1:1 from original)
# ---------------------------------------------------------------------------


def _suffix(length: int = 8) -> str:
    return "".join(
        random.SystemRandom().choice(string.ascii_uppercase + string.digits)
        for _ in range(length)
    )


def _get_default_position(title: str) -> dict[str, Any]:
    return {
        "DASHBOARD_VERSION_KEY": "v2",
        "ROOT_ID": {"children": ["GRID_ID"], "id": "ROOT_ID", "type": "ROOT"},
        "GRID_ID": {
            "children": [],
            "id": "GRID_ID",
            "parents": ["ROOT_ID"],
            "type": "GRID",
        },
        "HEADER_ID": {"id": "HEADER_ID", "meta": {"text": title}, "type": "HEADER"},
    }


def _append_charts(position: dict[str, Any], charts: set[Any]) -> dict[str, Any]:
    """Append orphan charts to a new row inside the grid."""
    chart_hashes = [f"CHART-{_suffix()}" for _ in charts]

    row_hash = None
    if "ROOT_ID" in position and "GRID_ID" in position["ROOT_ID"]["children"]:
        row_hash = f"ROW-N-{_suffix()}"
        position["GRID_ID"]["children"].append(row_hash)
        position[row_hash] = {
            "children": chart_hashes,
            "id": row_hash,
            "meta": {"0": "ROOT_ID", "background": "BACKGROUND_TRANSPARENT"},
            "type": "ROW",
            "parents": ["ROOT_ID", "GRID_ID"],
        }

    for chart_hash, chart in zip(chart_hashes, charts, strict=False):
        position[chart_hash] = {
            "children": [],
            "id": chart_hash,
            "meta": {
                "chartId": chart.id,
                "height": DEFAULT_CHART_HEIGHT,
                "sliceName": chart.slice_name,
                "uuid": str(chart.uuid),
                "width": DEFAULT_CHART_WIDTH,
            },
            "type": "CHART",
        }
        if row_hash:
            position[chart_hash]["parents"] = ["ROOT_ID", "GRID_ID", row_hash]

    return position


def find_chart_uuids(position: dict[str, Any]) -> set[str]:
    """Extract chart UUIDs from dashboard position dict."""
    return set(_build_uuid_to_id_map(position))


def find_native_filter_datasets(metadata: dict[str, Any]) -> set[str]:
    """Extract dataset UUIDs referenced by native filters."""
    uuids: set[str] = set()
    for native_filter in metadata.get("native_filter_configuration", []):
        for target in native_filter.get("targets", []):
            dataset_uuid = target.get("datasetUuid")
            if dataset_uuid:
                uuids.add(dataset_uuid)
    return uuids


def _build_uuid_to_id_map(position: dict[str, Any]) -> dict[str, int]:
    """Build mapping {chart_uuid: chart_id} from position dict."""
    return {
        child["meta"]["uuid"]: child["meta"]["chartId"]
        for child in position.values()
        if (
            isinstance(child, dict)
            and child.get("type") == "CHART"
            and "uuid" in child.get("meta", {})
        )
    }


def update_id_refs(  # noqa: C901
    config: dict[str, Any],
    chart_ids: dict[str, int],
    dataset_info: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Update dashboard metadata to use new IDs.

    Ported 1:1 from superset_old/commands/dashboard/importers/v1/utils.py.
    """
    fixed = config.copy()

    # Build map old_id => new_id
    old_ids = _build_uuid_to_id_map(fixed.get("position", {}))
    id_map: dict[int, int] = {
        old_id: chart_ids[uuid] for uuid, old_id in old_ids.items() if uuid in chart_ids
    }

    # Fix metadata
    metadata = fixed.get("metadata", {})
    if "timed_refresh_immune_slices" in metadata:
        metadata["timed_refresh_immune_slices"] = [
            id_map[old_id]
            for old_id in metadata["timed_refresh_immune_slices"]
            if old_id in id_map
        ]

    if "filter_scopes" in metadata:
        metadata["filter_scopes"] = {
            str(id_map[int(old_id)]): columns
            for old_id, columns in metadata["filter_scopes"].items()
            if int(old_id) in id_map
        }
        for columns in metadata["filter_scopes"].values():
            for attributes in columns.values():
                attributes["immune"] = [
                    id_map[old_id]
                    for old_id in attributes["immune"]
                    if old_id in id_map
                ]

    if "expanded_slices" in metadata:
        metadata["expanded_slices"] = {
            str(id_map[int(old_id)]): value
            for old_id, value in metadata["expanded_slices"].items()
            if int(old_id) in id_map
        }

    if "default_filters" in metadata:
        default_filters = _json.loads(metadata["default_filters"])
        metadata["default_filters"] = _json.dumps(
            {
                str(id_map[int(old_id)]): value
                for old_id, value in default_filters.items()
                if int(old_id) in id_map
            }
        )

    # Fix position — update chartId in each CHART component
    position = fixed.get("position", {})
    for child in position.values():
        if (
            isinstance(child, dict)
            and child.get("type") == "CHART"
            and "uuid" in child.get("meta", {})
            and child["meta"]["uuid"] in chart_ids
        ):
            child["meta"]["chartId"] = chart_ids[child["meta"]["uuid"]]

    # Fix native filter references
    native_filter_configuration = fixed.get("metadata", {}).get(
        "native_filter_configuration", []
    )
    for native_filter in native_filter_configuration:
        targets = native_filter.get("targets", [])
        for target in targets:
            dataset_uuid = target.pop("datasetUuid", None)
            if dataset_uuid and dataset_uuid in dataset_info:
                target["datasetId"] = dataset_info[dataset_uuid]["datasource_id"]

        scope_excluded = native_filter.get("scope", {}).get("excluded", [])
        if scope_excluded:
            native_filter["scope"]["excluded"] = [
                id_map[old_id] for old_id in scope_excluded if old_id in id_map
            ]

    fixed = _update_cross_filter_scoping(fixed, id_map)
    return fixed


def _update_cross_filter_scoping(
    config: dict[str, Any],
    id_map: dict[int, int],
) -> dict[str, Any]:
    """Fix cross-filter references in dashboard metadata.

    Ported 1:1 from superset_old/commands/dashboard/importers/v1/utils.py.
    """
    fixed = config.copy()

    cross_filter_global_config = fixed.get("metadata", {}).get(
        "global_chart_configuration", {}
    )
    scope_excluded = cross_filter_global_config.get("scope", {}).get("excluded", [])
    if scope_excluded:
        cross_filter_global_config["scope"]["excluded"] = [
            id_map[old_id] for old_id in scope_excluded if old_id in id_map
        ]

    if "chart_configuration" in (metadata := fixed.get("metadata", {})):
        new_chart_configuration: dict[str, Any] = {}
        for old_id_str, chart_config in metadata["chart_configuration"].items():
            try:
                old_id_int = int(old_id_str)
            except (TypeError, ValueError):
                continue

            new_id = id_map.get(old_id_int)
            if new_id is None:
                continue

            if isinstance(chart_config, dict):
                chart_config["id"] = new_id
                scope = chart_config.get("crossFilters", {}).get("scope", {})
                if isinstance(scope, dict):
                    excluded_scope = scope.get("excluded", [])
                    if excluded_scope:
                        chart_config["crossFilters"]["scope"]["excluded"] = [
                            id_map[old_id]
                            for old_id in excluded_scope
                            if old_id in id_map
                        ]

            new_chart_configuration[str(new_id)] = chart_config

        metadata["chart_configuration"] = new_chart_configuration
    return fixed


async def _import_dashboard(  # noqa: C901
    session: AsyncSession,
    config: dict[str, Any],
    overwrite: bool = False,
    security_manager: Any | None = None,
    current_user: Any | None = None,
) -> Dashboard:
    """Import a single dashboard from config dict.

    Ported 1:1 from superset_old/commands/dashboard/importers/v1/utils.py.
    Handles UUID-based dedup, JSON serialization, and owner management.
    """
    from sqlalchemy import select as sa_select

    from superset.models.dashboard import Dashboard

    can_write = True
    if security_manager is not None:
        can_write = await security_manager.can_access("can_write", "Dashboard")

    # UUID-based dedup
    stmt = sa_select(Dashboard).where(Dashboard.uuid == _UUID(str(config["uuid"])))
    result = await session.execute(stmt)
    existing = result.scalars().one_or_none()

    if existing:
        if overwrite and can_write and current_user:
            if security_manager is not None:
                can_access = await security_manager.can_access_dashboard(existing)
                is_admin = await security_manager.is_admin()
                await session.refresh(existing, ["owners"])
                if not can_access or (
                    current_user not in existing.owners and not is_admin
                ):
                    raise ImportFailedError(
                        "A dashboard already exists and user doesn't "
                        "have permissions to overwrite it"
                    )
        elif not overwrite or not can_write:
            return existing
        config["id"] = existing.id
    elif not can_write:
        raise ImportFailedError(
            "Dashboard doesn't exist and user doesn't "
            "have permission to create dashboards"
        )

    config = config.copy()

    # Remove deprecated show_native_filters
    if "metadata" in config and "show_native_filters" in config.get("metadata", {}):
        del config["metadata"]["show_native_filters"]

    # Serialize position/metadata dicts to JSON strings for DB storage
    for key, new_name in _JSON_KEYS_IMPORT.items():
        if config.get(key) is not None:
            value = config.pop(key)
            try:
                config[new_name] = _json.dumps(value)
            except TypeError:
                logger.info("Unable to encode `%s` field: %s", key, value)

    # Build the dashboard model
    dashboard_id = config.pop("id", None)
    _NON_MODEL_FIELDS = {  # noqa: N806
        "dataset_uuid",
        "database_uuid",
        "version",
        "tags",
        "theme_uuid",
    }
    model_data = {k: v for k, v in config.items() if k not in _NON_MODEL_FIELDS}

    if dashboard_id is not None:
        stmt = sa_select(Dashboard).where(Dashboard.id == dashboard_id)
        result = await session.execute(stmt)
        dashboard = result.scalars().one()
        for key, value in model_data.items():
            if hasattr(dashboard, key):
                setattr(dashboard, key, value)
    else:
        dashboard = Dashboard(
            **{k: v for k, v in model_data.items() if hasattr(Dashboard, k)}
        )
        session.add(dashboard)

    await session.flush()

    # Owner management
    if current_user is not None:
        await session.refresh(dashboard, ["owners"])
        if current_user not in dashboard.owners:
            dashboard.owners.append(current_user)

    return dashboard


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
                raise CommandInvalidError(f"slug '{slug}' already exists")

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

        # Resolve owners — refresh first to avoid MissingGreenlet
        # on the lazy-loaded collection in async context.
        await self._dao.session.refresh(dashboard, ["owners"])
        owner_ids = self._data.get("owners", [])
        resolved_owner_ids: list[int] = []
        if owner_ids and self._security_manager is not None:
            owners = []
            for oid in owner_ids:
                user = await self._security_manager.find_user_by_id(oid)
                if user:
                    owners.append(user)
                    resolved_owner_ids.append(user.id)
            dashboard.owners = owners
        elif (
            not owner_ids
            and self._user_id is not None
            and self._security_manager is not None
        ):
            user = await self._security_manager.find_user_by_id(self._user_id)
            if user:
                dashboard.owners = [user]
                resolved_owner_ids.append(user.id)

        # Resolve roles
        role_ids = self._data.get("roles", [])
        if role_ids and self._security_manager is not None:
            roles = []
            for rid in role_ids:
                role = await self._security_manager.find_role_by_id(rid)
                if role:
                    roles.append(role)
            dashboard.roles = roles

        # Add implicit type: and owner: tags
        # (async port of DashboardUpdater.after_insert)
        owner_ids = resolved_owner_ids
        await add_implicit_tags_after_insert(
            self._dao.session, "dashboard", dashboard.id, owner_ids
        )

        return dashboard


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

        if "position_json" in self._data:
            await self._process_tab_diff(old_position_json)

        # Synchronise filter scopes, color maps and shared-label colors
        # when json_metadata is updated.
        if "json_metadata" in self._data:
            await self._dao.set_dash_metadata(self._dashboard, self._data)

        # Resolve owners
        owner_ids = self._data.get("owners", [])
        if owner_ids and self._security_manager is not None:
            owners = []
            for oid in owner_ids:
                user = await self._security_manager.find_user_by_id(oid)
                if user:
                    owners.append(user)
            self._dashboard.owners = owners

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


class DeleteDashboardCommand(AsyncBaseCommand[None]):
    def __init__(
        self,
        dao: AsyncDashboardDAO,
        dashboard_id: int,
        security_manager: Any | None = None,
        user_id: int | None = None,
    ) -> None:
        self._dao = dao
        self._dashboard_id = dashboard_id
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
        # Check for associated report schedules
        if hasattr(self._dao, "find_report_schedules_by_dashboard_id"):
            reports = await self._dao.find_report_schedules_by_dashboard_id(
                self._dashboard_id
            )
            if reports:
                raise CommandInvalidError(
                    "Cannot delete: associated report schedules exist"
                )

    async def run(self) -> None:
        assert self._dashboard is not None
        dashboard_id = self._dashboard.id
        # Remove implicit tags before deleting
        # (async port of DashboardUpdater.after_delete)
        await delete_tagged_objects(self._dao.session, "dashboard", dashboard_id)
        await self._dao.delete([self._dashboard])
        await self._dao.session.flush()


class BulkDeleteDashboardsCommand(AsyncBaseCommand[None]):
    def __init__(
        self,
        dao: AsyncDashboardDAO,
        dashboard_ids: list[int],
        security_manager: Any | None = None,
        user_id: int | None = None,
    ) -> None:
        self._dao = dao
        self._dashboard_ids = dashboard_ids
        self._security_manager = security_manager
        self._user_id = user_id
        self._dashboards: list[Any] = []

    async def validate(self) -> None:
        if not self._dashboard_ids:
            raise CommandInvalidError("No dashboard IDs provided")
        self._dashboards = await self._dao.find_by_ids(self._dashboard_ids)
        found_ids = {int(d.id) for d in self._dashboards}
        missing = set(self._dashboard_ids) - found_ids
        if missing:
            raise ObjectNotFoundError("Dashboard", str(missing))
        # Ownership check
        if self._security_manager is not None:
            for dashboard in self._dashboards:
                await self._security_manager.raise_for_ownership(
                    dashboard, self._user_id
                )
        # Report schedule check
        if hasattr(self._dao, "find_report_schedules_by_dashboard_id"):
            for dashboard in self._dashboards:
                reports = await self._dao.find_report_schedules_by_dashboard_id(
                    dashboard.id
                )
                if reports:
                    raise CommandInvalidError(
                        f"Cannot delete dashboard {dashboard.id}: "
                        "associated report schedules exist"
                    )

    async def run(self) -> None:
        await self._dao.delete(self._dashboards)
        await self._dao.session.flush()


class CopyDashboardCommand(AsyncBaseCommand["Dashboard"]):
    def __init__(
        self,
        dao: AsyncDashboardDAO,
        dashboard_id: int,
        data: dict[str, Any],
        current_user: Any | None = None,
        security_manager: Any | None = None,
    ) -> None:
        self._dao = dao
        self._dashboard_id = dashboard_id
        self._data = data
        self._current_user = current_user
        self._security_manager = security_manager
        self._dashboard: Any | None = None

    async def validate(self) -> None:
        self._dashboard = await self._dao.get_by_id_or_slug(self._dashboard_id)
        if not self._dashboard:
            raise ObjectNotFoundError("Dashboard", self._dashboard_id)
        if not self._data.get("dashboard_title"):
            raise CommandInvalidError("dashboard_title is required for copy")
        if not self._data.get("json_metadata"):
            raise CommandInvalidError("json_metadata is required for copy")

    async def run(self) -> "Dashboard":
        assert self._dashboard is not None
        new_dash = await self._dao.copy_dashboard(
            self._dashboard,
            self._data,
            current_user=self._current_user,
        )
        await self._dao.session.flush()
        return new_dash


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
                if (
                    reordered_filter_ids
                    and new_filter_id not in reordered_filter_ids
                ):
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


class ExportDashboardsCommand(AsyncExportModelsCommand):
    """Export dashboards to a ZIP bundle.

    Ported 1:1 from superset_old/commands/dashboard/export.py.
    Includes JSON key conversion, native filter dataset UUID resolution,
    position handling with orphan chart support, and related chart/dataset/database
    export.
    """

    _resource_type = "Dashboard"

    def __init__(
        self, model_ids: list[int], dao: AsyncDashboardDAO | None = None
    ) -> None:
        super().__init__(model_ids)
        self._dao = dao

    async def _export_single(self, model_id: int) -> list[tuple[str, str]]:  # noqa: C901
        from sqlalchemy import select as sa_select
        from sqlalchemy.orm import selectinload

        if self._dao is None:
            raise CommandInvalidError("DAO not provided for export")

        from superset.models.connectors import SqlaTable
        from superset.models.dashboard import Dashboard
        from superset.models.slice import Slice

        # Eagerly load slices -> table -> database
        stmt = (
            sa_select(Dashboard)
            .where(Dashboard.id == model_id)
            .options(
                selectinload(Dashboard.slices)
                .selectinload(Slice.table)
                .selectinload(SqlaTable.database),
            )
        )
        result = await self._dao.session.execute(stmt)
        dashboard = result.scalars().one_or_none()
        if not dashboard:
            raise ObjectNotFoundError("Dashboard", model_id)

        # Build payload — convert JSON string fields to dicts
        payload: dict[str, Any] = {
            "dashboard_title": dashboard.dashboard_title,
            "description": dashboard.description,
            "css": dashboard.css,
            "slug": dashboard.slug,
            "uuid": str(dashboard.uuid) if dashboard.uuid else None,
            "certified_by": dashboard.certified_by,
            "certification_details": dashboard.certification_details,
            "published": dashboard.published,
            "is_managed_externally": getattr(dashboard, "is_managed_externally", False),
            "external_url": getattr(dashboard, "external_url", None),
        }

        # Convert position_json and json_metadata from JSON strings to dicts
        for key, new_name in _JSON_KEYS_EXPORT.items():
            value: str | None = getattr(dashboard, key, None)
            if value:
                try:
                    payload[new_name] = _json.loads(value)
                except (_json.JSONDecodeError, TypeError):
                    logger.info("Unable to decode `%s` field: %s", key, value)
                    payload[new_name] = {}
            else:
                payload[new_name] = {}

        # Replace native filter dataset IDs with UUIDs
        for native_filter in payload.get("metadata", {}).get(
            "native_filter_configuration", []
        ):
            for target in native_filter.get("targets", []):
                dataset_id = target.pop("datasetId", None)
                if dataset_id is not None:
                    # Look up dataset UUID
                    ds_stmt = sa_select(SqlaTable).where(SqlaTable.id == dataset_id)
                    ds_result = await self._dao.session.execute(ds_stmt)
                    ds = ds_result.scalars().one_or_none()
                    if ds:
                        target["datasetUuid"] = str(ds.uuid)

        # Ensure position exists — if not, create a default
        if not payload.get("position"):
            payload["position"] = _get_default_position(dashboard.dashboard_title or "")

        # Find orphan charts not referenced in position and append them
        referenced_charts = find_chart_uuids(payload["position"])
        slices = dashboard.slices or []
        orphan_charts = {
            chart for chart in slices if str(chart.uuid) not in referenced_charts
        }
        if orphan_charts:
            payload["position"] = _append_charts(payload["position"], orphan_charts)

        payload["version"] = EXPORT_VERSION

        file_name = _get_filename(dashboard.dashboard_title or "", dashboard.id)
        dash_yaml = yaml.safe_dump(payload, sort_keys=False)
        files: list[tuple[str, str]] = [(f"dashboards/{file_name}.yaml", dash_yaml)]

        # Bundle dependent resources: charts + datasets + databases
        seen_datasets: set[int] = set()
        seen_databases: set[int] = set()

        for chart in slices:
            chart_payload: dict[str, Any] = {
                "slice_name": chart.slice_name,
                "viz_type": chart.viz_type,
                "params": chart.params,
                "query_context": chart.query_context,
                "cache_timeout": chart.cache_timeout,
                "uuid": str(chart.uuid) if chart.uuid else None,
                "certified_by": chart.certified_by,
                "certification_details": chart.certification_details,
                "description": chart.description,
                "version": EXPORT_VERSION,
            }
            # Decode params
            if chart_payload.get("params"):
                try:
                    chart_payload["params"] = _json.loads(chart_payload["params"])
                except (_json.JSONDecodeError, TypeError):
                    pass

            ds = chart.table
            if ds:
                chart_payload["dataset_uuid"] = str(ds.uuid)

            chart_file = _get_filename(chart.slice_name, chart.id)
            files.append(
                (
                    f"charts/{chart_file}.yaml",
                    yaml.safe_dump(chart_payload, sort_keys=False),
                )
            )

            if ds and ds.id not in seen_datasets:
                seen_datasets.add(ds.id)
                ds_data: dict[str, Any] = {
                    "table_name": ds.table_name,
                    "schema": getattr(ds, "schema", None),
                    "sql": getattr(ds, "sql", None),
                    "description": getattr(ds, "description", None),
                    "cache_timeout": getattr(ds, "cache_timeout", None),
                    "uuid": str(ds.uuid) if ds.uuid else None,
                    "version": EXPORT_VERSION,
                }
                db = getattr(ds, "database", None)
                if db:
                    ds_data["database_uuid"] = str(db.uuid) if db.uuid else None
                    ds_file = _get_filename(ds.table_name, ds.id)
                    files.append(
                        (
                            f"datasets/{ds_file}.yaml",
                            yaml.safe_dump(ds_data, sort_keys=False),
                        )
                    )
                    if db.id not in seen_databases:
                        seen_databases.add(db.id)
                        db_data: dict[str, Any] = {
                            "database_name": db.database_name,
                            "sqlalchemy_uri": mask_uri_password(
                                getattr(db, "sqlalchemy_uri", "")
                            ),
                            "uuid": str(db.uuid) if db.uuid else None,
                            "version": EXPORT_VERSION,
                        }
                        db_file = _get_filename(db.database_name, db.id)
                        files.append(
                            (
                                f"databases/{db_file}.yaml",
                                yaml.safe_dump(db_data, sort_keys=False),
                            )
                        )

        # Also export native-filter-referenced datasets that aren't chart datasources
        for native_filter in payload.get("metadata", {}).get(
            "native_filter_configuration", []
        ):
            for target in native_filter.get("targets", []):
                ds_uuid = target.get("datasetUuid")
                if ds_uuid:
                    ds_stmt = (
                        sa_select(SqlaTable)
                        .where(SqlaTable.uuid == _UUID(ds_uuid))
                        .options(selectinload(SqlaTable.database))
                    )
                    ds_result = await self._dao.session.execute(ds_stmt)
                    ds = ds_result.scalars().one_or_none()
                    if ds and ds.id not in seen_datasets:
                        seen_datasets.add(ds.id)
                        ds_data = {
                            "table_name": ds.table_name,
                            "schema": getattr(ds, "schema", None),
                            "sql": getattr(ds, "sql", None),
                            "uuid": str(ds.uuid) if ds.uuid else None,
                            "version": EXPORT_VERSION,
                        }
                        db = getattr(ds, "database", None)
                        if db:
                            ds_data["database_uuid"] = str(db.uuid) if db.uuid else None
                        ds_file = _get_filename(ds.table_name, ds.id)
                        files.append(
                            (
                                f"datasets/{ds_file}.yaml",
                                yaml.safe_dump(ds_data, sort_keys=False),
                            )
                        )
                        if db and db.id not in seen_databases:
                            seen_databases.add(db.id)
                            db_data = {
                                "database_name": db.database_name,
                                "sqlalchemy_uri": mask_uri_password(
                                    getattr(db, "sqlalchemy_uri", "")
                                ),
                                "uuid": str(db.uuid) if db.uuid else None,
                                "version": EXPORT_VERSION,
                            }
                            db_file = _get_filename(db.database_name, db.id)
                            files.append(
                                (
                                    f"databases/{db_file}.yaml",
                                    yaml.safe_dump(db_data, sort_keys=False),
                                )
                            )

        return files


class ImportDashboardsCommand(AsyncImportModelsCommand):
    """Import dashboards from a ZIP bundle.

    Ported 1:1 from superset_old/commands/dashboard/importers/v1/.
    This is the MOST COMPLEX import. Resolves dependencies:
    databases -> datasets -> charts -> dashboards.

    Handles:
    - UUID-based dedup at every level
    - position_json and json_metadata JSON deserialization/serialization
    - update_id_refs — full ID reference update (chart IDs, filter scopes,
      expanded slices, default filters, native filters, cross-filter scoping)
    - dashboard_slices M2M relationship management via explicit inserts
    - All dashboard fields (css, certified_by, certification_details, etc.)
    - Owner management
    """

    def __init__(
        self,
        contents: io.BytesIO,
        dao: AsyncDashboardDAO | None = None,
        security_manager: Any | None = None,
        current_user: Any | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(contents, **kwargs)
        self._dao = dao
        self._security_manager = security_manager
        self._current_user = current_user

    async def _validate(self, configs: dict[str, dict[str, Any]]) -> None:
        for name, config in configs.items():
            if name.startswith("dashboards/") and not config.get("dashboard_title"):
                raise CommandInvalidError(f"Missing dashboard_title in {name}")

    async def _check_existing(self, uuid_val: str) -> bool:
        if self._dao is None:
            return False
        result = await self._dao.find_one_or_none(uuid=_UUID(uuid_val))
        return result is not None

    async def run(self) -> None:  # noqa: C901
        """Orchestrate import: databases -> datasets -> charts -> dashboards.

        Ported 1:1 from ImportDashboardsCommand._import in the original.
        """
        if self._configs is None:
            raise CommandInvalidError("validate() must be called before run()")
        if self._dao is None:
            raise CommandInvalidError("DAO not provided for import")

        from sqlalchemy import select as sa_select

        from superset.models.dashboard import dashboard_slices

        configs = self._configs
        session = self._dao.session

        # 1. Discover charts, datasets associated with dashboards
        chart_uuids: set[str] = set()
        dataset_uuids: set[str] = set()
        for file_name, config in configs.items():
            if file_name.startswith("dashboards/") and isinstance(config, dict):
                chart_uuids.update(find_chart_uuids(config.get("position", {})))
                dataset_uuids.update(
                    find_native_filter_datasets(config.get("metadata", {}))
                )

        # 2. Discover datasets associated with needed charts
        for file_name, config in configs.items():
            if (
                file_name.startswith("charts/")
                and isinstance(config, dict)
                and config.get("uuid") in chart_uuids
            ):
                ds_uuid = config.get("dataset_uuid")
                if ds_uuid:
                    dataset_uuids.add(ds_uuid)

        # 3. Discover databases associated with needed datasets
        database_uuids: set[str] = set()
        for file_name, config in configs.items():
            if (
                file_name.startswith("datasets/")
                and isinstance(config, dict)
                and config.get("uuid") in dataset_uuids
            ):
                db_uuid = config.get("database_uuid")
                if db_uuid:
                    database_uuids.add(db_uuid)

        # 4. Import related databases (overwrite=False)
        database_ids: dict[str, int] = {}
        for file_name, config in configs.items():
            if (
                file_name.startswith("databases/")
                and isinstance(config, dict)
                and config.get("uuid") in database_uuids
            ):
                db = await _import_database(session, config)
                database_ids[str(db.uuid)] = db.id

        # 5. Import datasets with correct parent ref (overwrite=False)
        dataset_info: dict[str, dict[str, Any]] = {}
        for file_name, config in configs.items():
            if (
                file_name.startswith("datasets/")
                and isinstance(config, dict)
                and config.get("database_uuid") in database_ids
            ):
                config["database_id"] = database_ids[config["database_uuid"]]
                dataset = await _import_dataset(session, config)
                dataset_info[str(dataset.uuid)] = {
                    "datasource_id": dataset.id,
                    "datasource_type": getattr(dataset, "datasource_type", "table"),
                    "datasource_name": dataset.table_name,
                }

        # 6. Import charts with correct parent ref (overwrite=False)
        charts: list[Any] = []
        chart_ids: dict[str, int] = {}
        for file_name, config in configs.items():
            if (
                file_name.startswith("charts/")
                and isinstance(config, dict)
                and config.get("dataset_uuid") in dataset_info
            ):
                # Update datasource id, type, and name
                dataset_dict = dataset_info[config["dataset_uuid"]]
                config = update_chart_config_dataset(config, dataset_dict)

                chart = await _import_chart(
                    session,
                    config,
                    overwrite=False,
                    security_manager=self._security_manager,
                    current_user=self._current_user,
                )
                charts.append(chart)
                chart_ids[str(chart.uuid)] = chart.id

        # 7. Get existing dashboard-chart relationships
        existing_relationships_stmt = sa_select(
            dashboard_slices.c.dashboard_id,
            dashboard_slices.c.slice_id,
        )
        existing_result = await session.execute(existing_relationships_stmt)
        existing_relationships = set(existing_result.fetchall())

        # 8. Import dashboards
        dashboards: list[Dashboard] = []
        dashboard_chart_ids: list[tuple[int, int]] = []
        for file_name, config in configs.items():
            if file_name.startswith("dashboards/") and isinstance(config, dict):
                config = update_id_refs(config, chart_ids, dataset_info)
                dashboard = await _import_dashboard(
                    session,
                    config,
                    overwrite=self._overwrite,
                    security_manager=self._security_manager,
                    current_user=self._current_user,
                )
                dashboards.append(dashboard)

                # Build M2M dashboard-chart entries
                for uuid_str in find_chart_uuids(config.get("position", {})):
                    if uuid_str not in chart_ids:
                        continue
                    chart_id = chart_ids[uuid_str]
                    if (dashboard.id, chart_id) not in existing_relationships:
                        dashboard_chart_ids.append((dashboard.id, chart_id))

        # 9. Insert dashboard_slices M2M relationships via explicit inserts
        if dashboard_chart_ids:
            values = [
                {"dashboard_id": dashboard_id, "slice_id": chart_id}
                for (dashboard_id, chart_id) in dashboard_chart_ids
            ]
            await session.execute(dashboard_slices.insert(), values)

        # 10. Remove obsolete filter-box charts
        for chart in charts:
            if getattr(chart, "viz_type", None) == "filter_box":
                await session.delete(chart)

    async def _import_single(self, file_name: str, content: dict[str, Any]) -> None:
        # Not used — run() handles the full orchestration
        pass


def parse_tab_structure(position_json: str | None) -> list[dict[str, Any]]:
    """Extract tab structure from dashboard position_json.

    Returns list of {tab_id, tab_title, charts} dicts.
    Moved from controller to keep controllers thin.
    """
    import json  # noqa: TID251

    if not position_json:
        return []
    try:
        positions = json.loads(position_json)
    except (ValueError, TypeError):
        return []
    tabs = []
    for key, value in positions.items():
        if isinstance(value, dict) and value.get("type") == "TAB":
            meta = value.get("meta", {})
            chart_ids = [
                child.get("meta", {}).get("chartId")
                for child in positions.values()
                if isinstance(child, dict)
                and child.get("meta", {}).get("chartId")
                and child.get("parents")
                and key in child.get("parents", [])
            ]
            tabs.append(
                {
                    "tab_id": key,
                    "tab_title": meta.get("text", ""),
                    "charts": [c for c in chart_ids if c],
                }
            )
    return tabs


class UpsertEmbeddedDashboardCommand(AsyncBaseCommand["EmbeddedDashboard"]):
    def __init__(
        self,
        dao: AsyncDashboardDAO,
        embedded_dao: AsyncEmbeddedDashboardDAO,
        dashboard_id: int,
        allowed_domains: list[str],
    ) -> None:
        self._dao = dao
        self._embedded_dao = embedded_dao
        self._dashboard_id = dashboard_id
        self._allowed_domains = allowed_domains
        self._dashboard: Any | None = None

    async def validate(self) -> None:
        self._dashboard = await self._dao.get_by_id_or_slug(self._dashboard_id)
        if not self._dashboard:
            raise ObjectNotFoundError("Dashboard", self._dashboard_id)

    async def run(self) -> "EmbeddedDashboard":
        assert self._dashboard is not None
        embedded = await self._embedded_dao.upsert(
            self._dashboard.id,
            self._allowed_domains,
        )
        await self._embedded_dao.session.flush()
        return embedded


class DeleteEmbeddedDashboardCommand(AsyncBaseCommand[None]):
    def __init__(
        self,
        dao: AsyncDashboardDAO,
        embedded_dao: AsyncEmbeddedDashboardDAO,
        dashboard_id: int,
    ) -> None:
        self._dao = dao
        self._embedded_dao = embedded_dao
        self._dashboard_id = dashboard_id
        self._dashboard: Any | None = None

    async def validate(self) -> None:
        self._dashboard = await self._dao.get_by_id_or_slug(self._dashboard_id)
        if not self._dashboard:
            raise ObjectNotFoundError("Dashboard", self._dashboard_id)

    async def run(self) -> None:
        assert self._dashboard is not None
        embedded = await self._embedded_dao.find_by_dashboard_id(self._dashboard.id)
        if embedded:
            await self._embedded_dao.session.delete(embedded)
            await self._embedded_dao.session.flush()


class AddFavoriteDashboardCommand(AsyncBaseCommand[None]):
    """Add a dashboard to a user's favorites.

    Ported 1:1 from superset_old/commands/dashboard/fave.py.
    The original validates dashboard existence, then delegates
    to DashboardDAO.add_favorite.
    """

    def __init__(
        self,
        dao: AsyncDashboardDAO,
        dashboard_id: int,
        user_id: int,
    ) -> None:
        self._dao = dao
        self._dashboard_id = dashboard_id
        self._user_id = user_id

    async def validate(self) -> None:
        dashboard = await self._dao.get_by_id_or_slug(self._dashboard_id)
        if not dashboard:
            raise ObjectNotFoundError("Dashboard", self._dashboard_id)

    async def run(self) -> None:
        await self._dao.add_favorite(self._dashboard_id, user_id=self._user_id)


class RemoveFavoriteDashboardCommand(AsyncBaseCommand[None]):
    """Remove a dashboard from a user's favorites.

    Ported 1:1 from superset_old/commands/dashboard/unfave.py.
    The original validates dashboard existence, then delegates
    to DashboardDAO.remove_favorite.
    """

    def __init__(
        self,
        dao: AsyncDashboardDAO,
        dashboard_id: int,
        user_id: int,
    ) -> None:
        self._dao = dao
        self._dashboard_id = dashboard_id
        self._user_id = user_id

    async def validate(self) -> None:
        dashboard = await self._dao.get_by_id_or_slug(self._dashboard_id)
        if not dashboard:
            raise ObjectNotFoundError("Dashboard", self._dashboard_id)

    async def run(self) -> None:
        await self._dao.remove_favorite(self._dashboard_id, user_id=self._user_id)
