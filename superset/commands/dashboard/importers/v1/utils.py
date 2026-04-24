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
"""Async port of ``superset_old/commands/dashboard/importers/v1/utils.py``."""

from __future__ import annotations

import json as _json
import logging
import random
import string
from typing import Any, TYPE_CHECKING
from uuid import UUID as _UUID

from superset.exceptions import ImportFailedError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from superset.models.dashboard import Dashboard

logger = logging.getLogger(__name__)

# JSON keys that are stored as JSON strings in the DB but exported as dicts
_JSON_KEYS_EXPORT = {"position_json": "position", "json_metadata": "metadata"}
_JSON_KEYS_IMPORT = {"position": "position_json", "metadata": "json_metadata"}

DEFAULT_CHART_HEIGHT = 50
DEFAULT_CHART_WIDTH = 4


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

    # Fix metadata — match the original 1:1 (no extra ``if old_id in id_map``
    # guards on these lookups; missing entries surface as KeyError so callers
    # see a stale bundle.)
    metadata = fixed.get("metadata", {})
    if "timed_refresh_immune_slices" in metadata:
        metadata["timed_refresh_immune_slices"] = [
            id_map[old_id] for old_id in metadata["timed_refresh_immune_slices"]
        ]

    if "filter_scopes" in metadata:
        # in filter_scopes the key is the chart ID as a string; we need to update
        # them to be the new ID as a string:
        metadata["filter_scopes"] = {
            str(id_map[int(old_id)]): columns
            for old_id, columns in metadata["filter_scopes"].items()
            if int(old_id) in id_map
        }

        # now update columns to use new IDs:
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
            if dataset_uuid:
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
        "uuid",
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
        # Preserve the bundle's UUID if provided.
        if config.get("uuid"):
            dashboard.uuid = _UUID(str(config["uuid"]))  # type: ignore[assignment]
        session.add(dashboard)

    await session.flush()

    # Owner management
    if current_user is not None:
        await session.refresh(dashboard, ["owners"])
        if current_user not in dashboard.owners:
            dashboard.owners.append(current_user)

    return dashboard
