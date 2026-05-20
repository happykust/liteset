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
"""Async port of ``superset_old/commands/dashboard/export.py``."""

from __future__ import annotations

import json as _json
import logging
import random
import string
from typing import Any, TYPE_CHECKING
from uuid import UUID as _UUID

import yaml  # type: ignore[import-untyped]
from superset.utils.file import secure_filename

from superset.commands.dashboard.importers.v1.utils import (
    _append_charts,
    _get_default_position,
    find_chart_uuids,
)
from superset.exceptions import CommandInvalidError, ObjectNotFoundError
from superset.importexport.export_base import AsyncExportModelsCommand
from superset.models.tags import TagType
from superset.utils.feature_flags import feature_flag_manager

if TYPE_CHECKING:
    from superset.db.daos.dashboard import AsyncDashboardDAO

logger = logging.getLogger(__name__)

EXPORT_VERSION = "1.0.0"

# JSON keys stored as strings on the model but exposed as dicts in the export.
_JSON_KEYS = {"position_json": "position", "json_metadata": "metadata"}


def _suffix(length: int = 8) -> str:
    return "".join(
        random.SystemRandom().choice(string.ascii_uppercase + string.digits)
        for _ in range(length)
    )


class ExportDashboardsCommand(AsyncExportModelsCommand):
    """Export dashboards to a ZIP bundle.

    Ported 1:1 from ``superset_old/commands/dashboard/export.py``: uses
    ``Dashboard.export_to_dict(recursive=False, include_defaults=True,
    export_uuids=True)`` to build the payload, then decodes the JSON
    string fields, replaces native filter dataset IDs with UUIDs, fills
    in a default position when missing, appends orphan charts, stamps
    ``theme_uuid``, and stamps the export version.

    Bundles related charts, datasets, and databases alongside.
    """

    _resource_type = "Dashboard"

    def __init__(
        self, model_ids: list[int], dao: AsyncDashboardDAO | None = None
    ) -> None:
        super().__init__(model_ids)
        self._dao = dao

    @staticmethod
    def _file_name(model: Any) -> str:
        slug = secure_filename(model.dashboard_title or "") or "unnamed"
        return f"dashboards/{slug}_{model.id}.yaml"

    async def _export_single(self, model_id: int) -> list[tuple[str, str]]:  # noqa: C901
        from sqlalchemy import select as sa_select
        from sqlalchemy.orm import selectinload

        if self._dao is None:
            raise CommandInvalidError("DAO not provided for export")

        from superset.models.connectors import SqlaTable
        from superset.models.dashboard import Dashboard
        from superset.models.slice import Slice

        # Eager-load relationships before going async.
        # Each ``ds.export_to_dict(recursive=True)`` walks
        # ``SqlaTable.export_children`` = ["metrics", "columns"]; without the
        # selectinload below SQLAlchemy fires an implicit lazy-load on
        # ``ds.metrics`` / ``ds.columns`` that fails with ``MissingGreenlet``.
        # We also pre-load ``Dashboard.theme`` / ``.tags`` because the export
        # payload references them while still inside the async event loop.
        stmt = (
            sa_select(Dashboard)
            .where(Dashboard.id == model_id)
            .options(
                selectinload(Dashboard.slices)
                .selectinload(Slice.table)
                .selectinload(SqlaTable.database),
                selectinload(Dashboard.slices)
                .selectinload(Slice.table)
                .selectinload(SqlaTable.metrics),
                selectinload(Dashboard.slices)
                .selectinload(Slice.table)
                .selectinload(SqlaTable.columns),
                selectinload(Dashboard.theme),
                selectinload(Dashboard.tags),
            )
        )
        result = await self._dao.session.execute(stmt)
        dashboard = result.scalars().one_or_none()
        if not dashboard:
            raise ObjectNotFoundError("Dashboard", model_id)

        # ``export_to_dict(recursive=False, include_defaults=True, export_uuids=True)``
        payload: dict[str, Any] = dashboard.export_to_dict(
            recursive=False,
            include_parent_ref=False,
            include_defaults=True,
            export_uuids=True,
        )

        # Convert position_json + json_metadata strings to dicts.
        for key, new_name in _JSON_KEYS.items():
            value: str | None = payload.pop(key, None)
            if value:
                try:
                    payload[new_name] = _json.loads(value)
                except (_json.JSONDecodeError, TypeError):
                    logger.info("Unable to decode `%s` field: %s", key, value)
                    payload[new_name] = {}

        # Replace native filter dataset IDs with UUIDs.
        for native_filter in payload.get("metadata", {}).get(
            "native_filter_configuration", []
        ):
            for target in native_filter.get("targets", []):
                dataset_id = target.pop("datasetId", None)
                if dataset_id is not None:
                    ds_q = await self._dao.session.execute(
                        sa_select(SqlaTable).where(SqlaTable.id == dataset_id)
                    )
                    ds = ds_q.scalars().one_or_none()
                    if ds:
                        target["datasetUuid"] = str(ds.uuid)

        if not payload.get("position"):
            payload["position"] = _get_default_position(dashboard.dashboard_title or "")

        # Append orphan charts not referenced in the position.
        referenced_charts = find_chart_uuids(payload["position"])
        slices = dashboard.slices or []
        orphan_charts = {
            chart for chart in slices if str(chart.uuid) not in referenced_charts
        }
        if orphan_charts:
            payload["position"] = _append_charts(payload["position"], orphan_charts)

        # Theme UUID for cross-system imports.
        theme = getattr(dashboard, "theme", None)
        payload["theme_uuid"] = str(theme.uuid) if theme else None

        payload["version"] = EXPORT_VERSION

        # Fetch tags from the database if TAGGING_SYSTEM is enabled
        if feature_flag_manager.is_feature_enabled("TAGGING_SYSTEM"):
            tags = getattr(dashboard, "tags", []) or []
            payload["tags"] = [tag.name for tag in tags if tag.type == TagType.custom]

        files: list[tuple[str, str]] = [
            (
                self._file_name(dashboard),
                yaml.safe_dump(payload, sort_keys=False),
            )
        ]

        # Bundle related charts + datasets + databases.
        seen_datasets: set[int] = set()
        seen_databases: set[int] = set()

        for chart in slices:
            chart_payload = chart.export_to_dict(
                recursive=False,
                include_parent_ref=False,
                include_defaults=True,
                export_uuids=True,
            )
            chart_payload = {
                k: v
                for k, v in chart_payload.items()
                if k not in ("datasource_type", "datasource_name", "url_params")
            }
            if chart_payload.get("params"):
                try:
                    chart_payload["params"] = _json.loads(chart_payload["params"])
                except (_json.JSONDecodeError, TypeError):
                    pass
            if chart.table:
                chart_payload["dataset_uuid"] = str(chart.table.uuid)
            chart_payload["version"] = EXPORT_VERSION

            chart_slug = secure_filename(chart.slice_name or "") or "unnamed"
            files.append(
                (
                    f"charts/{chart_slug}_{chart.id}.yaml",
                    yaml.safe_dump(chart_payload, sort_keys=False),
                )
            )

            ds = chart.table
            if ds and ds.id not in seen_datasets:
                seen_datasets.add(ds.id)
                ds_payload = ds.export_to_dict(
                    recursive=True,
                    include_parent_ref=False,
                    include_defaults=True,
                    export_uuids=True,
                )
                for key in ("params", "template_params", "extra"):
                    if ds_payload.get(key):
                        try:
                            ds_payload[key] = _json.loads(ds_payload[key])
                        except (TypeError, _json.JSONDecodeError):
                            pass
                for nested in ("metrics", "columns"):
                    for attrs in ds_payload.get(nested, []) or []:
                        if isinstance(attrs.get("extra"), str):
                            try:
                                attrs["extra"] = _json.loads(attrs["extra"])
                            except (TypeError, _json.JSONDecodeError):
                                pass
                ds_payload["version"] = EXPORT_VERSION
                if ds.database:
                    ds_payload["database_uuid"] = str(ds.database.uuid)
                db_slug = (
                    secure_filename(ds.database.database_name)
                    if ds.database
                    else "unknown"
                ) or "unknown"
                ds_slug = secure_filename(ds.table_name or "") or "unnamed"
                files.append(
                    (
                        f"datasets/{db_slug}/{ds_slug}.yaml",
                        yaml.safe_dump(ds_payload, sort_keys=False),
                    )
                )

                if ds.database and ds.database.id not in seen_databases:
                    seen_databases.add(ds.database.id)
                    db = ds.database
                    db_payload = db.export_to_dict(
                        recursive=False,
                        include_parent_ref=False,
                        include_defaults=True,
                        export_uuids=True,
                    )
                    if db_payload.get("extra"):
                        try:
                            db_payload["extra"] = _json.loads(db_payload["extra"])
                        except (TypeError, _json.JSONDecodeError):
                            pass
                    db_payload["version"] = EXPORT_VERSION
                    db_slug = secure_filename(db.database_name or "") or "unnamed"
                    files.append(
                        (
                            f"databases/{db_slug}.yaml",
                            yaml.safe_dump(db_payload, sort_keys=False),
                        )
                    )

        # Native-filter referenced datasets that aren't chart datasources.
        for native_filter in payload.get("metadata", {}).get(
            "native_filter_configuration", []
        ):
            for target in native_filter.get("targets", []):
                ds_uuid = target.get("datasetUuid")
                if ds_uuid:
                    ds_q = await self._dao.session.execute(
                        sa_select(SqlaTable)
                        .where(SqlaTable.uuid == _UUID(ds_uuid))
                        .options(selectinload(SqlaTable.database))
                    )
                    ds = ds_q.scalars().one_or_none()
                    if ds and ds.id not in seen_datasets:
                        seen_datasets.add(ds.id)
                        ds_payload = ds.export_to_dict(
                            recursive=True,
                            include_parent_ref=False,
                            include_defaults=True,
                            export_uuids=True,
                        )
                        ds_payload["version"] = EXPORT_VERSION
                        if ds.database:
                            ds_payload["database_uuid"] = str(ds.database.uuid)
                        db_slug = (
                            secure_filename(ds.database.database_name)
                            if ds.database
                            else "unknown"
                        ) or "unknown"
                        ds_slug = secure_filename(ds.table_name or "") or "unnamed"
                        files.append(
                            (
                                f"datasets/{db_slug}/{ds_slug}.yaml",
                                yaml.safe_dump(ds_payload, sort_keys=False),
                            )
                        )

        return files
