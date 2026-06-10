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

from superset.commands.dashboard.importers.v1.utils import (
    _append_charts,
    _get_default_position,
    find_chart_uuids,
)
from superset.exceptions import CommandInvalidError, ObjectNotFoundError
from superset.importexport.export_base import AsyncExportModelsCommand
from superset.models.tags import TagType
from superset.utils.feature_flags import feature_flag_manager
from superset.utils.file import get_filename

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
        self,
        model_ids: list[int],
        dao: AsyncDashboardDAO | None = None,
        security_manager: Any = None,
        user: Any = None,
        export_related: bool = True,
    ) -> None:
        super().__init__(
            model_ids,
            security_manager=security_manager,
            user=user,
            export_related=export_related,
        )
        self._dao = dao

    async def validate(self) -> None:
        from superset.db.filters import dashboard_access_filters
        from superset.models.dashboard import Dashboard

        await self._validate_access(
            self._dao, Dashboard, dashboard_access_filters, "Dashboard"
        )

    @staticmethod
    def _file_name(model: Any) -> str:
        file_name = get_filename(model.dashboard_title or "", model.id, skip_id=False)
        return f"dashboards/{file_name}.yaml"

    async def _attach_ssh_tunnel(
        self, db_payload: dict[str, Any], database_id: int
    ) -> None:
        """Attach the masked SSH-tunnel payload to a database YAML payload.

        1:1 with ``superset_old/commands/dataset/export.py:114-121`` — the
        path the original dashboard export delegates every database YAML to
        (via ``ExportChartsCommand`` → ``ExportDatasetsCommand._export``).
        """
        from superset.db.daos.database import AsyncSSHTunnelDAO
        from superset.utils.ssh_tunnel import mask_password_info

        ssh_tunnel = await AsyncSSHTunnelDAO(self._dao.session).get_by_database_id(
            database_id
        )
        if ssh_tunnel:
            ssh_tunnel_payload = ssh_tunnel.export_to_dict(
                recursive=False,
                include_parent_ref=False,
                include_defaults=True,
                export_uuids=False,
            )
            db_payload["ssh_tunnel"] = mask_password_info(ssh_tunnel_payload)

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
                selectinload(Dashboard.slices).selectinload(Slice.tags),
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

        # Convert position_json + json_metadata strings to dicts. A
        # valid-but-non-object value (``[1,2]`` / ``"s"``) must be coerced to
        # ``{}`` — otherwise the ``metadata.get(...)`` and
        # ``find_chart_uuids(position)`` calls below raise → HTTP 500 on export.
        for key, new_name in _JSON_KEYS.items():
            value: str | None = payload.pop(key, None)
            if value:
                try:
                    parsed = _json.loads(value)
                except (_json.JSONDecodeError, TypeError):
                    logger.info("Unable to decode `%s` field: %s", key, value)
                    parsed = {}
                payload[new_name] = parsed if isinstance(parsed, dict) else {}

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

        # Export related theme — gated by export_related, 1:1 with
        # superset_old/commands/dashboard/export.py:199-203 which emits theme
        # files only inside the ``if export_related:`` block.  When called from
        # AsyncFullAssetManager._export_type (export_related=False) the theme
        # YAML must NOT be emitted here — each theme is already exported by
        # its own top-level exporter in the full-assets bundle, and emitting
        # it unconditionally would inject spurious themes/*.yaml entries into
        # the bundle for every dashboard that has a theme (superset/importexport
        # /manager.py:266 passes export_related=False for exactly this reason).
        _theme_files: list[tuple[str, str]] = []
        if self._export_related and theme:
            from superset.commands.theme import ExportThemesCommand
            from superset.db.daos.theme import AsyncThemeDAO

            _theme_dao = AsyncThemeDAO(self._dao.session)
            _export_themes_cmd = ExportThemesCommand(
                dao=_theme_dao, model_ids=[theme.id]
            )
            await _export_themes_cmd.validate()
            _theme_files = await _export_themes_cmd.run()

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

        # Emit a separate ``tags.yaml`` entry only when export_related=True and
        # TAGGING_SYSTEM is enabled.  The original gates this inside the
        # ``if export_related:`` block (superset_old/commands/dashboard/export.py
        # :187-197).  When export_related=False (full-assets bundle from
        # AsyncFullAssetManager) the tags.yaml must NOT be emitted here: no
        # prior phase emits a tags.yaml so the first dashboard's tags.yaml would
        # land in the bundle unchallenged, producing partial tag data that the
        # original bundle never contained (clean absence is correct).
        if self._export_related and feature_flag_manager.is_feature_enabled(
            "TAGGING_SYSTEM"
        ):
            dashboard_tags = [
                {"tag_name": tag.name, "description": tag.description}
                for tag in (getattr(dashboard, "tags", []) or [])
                if tag.type == TagType.custom
            ]
            chart_tags: list[dict[str, Any]] = []
            for chart in dashboard.slices or []:
                chart_tags.extend(
                    {"tag_name": tag.name, "description": tag.description}
                    for tag in (getattr(chart, "tags", []) or [])
                    if "type:" not in tag.name and "owner:" not in tag.name
                )
            # Merge, preventing duplicates by tag name (dashboard tags win).
            tags_dict = {tag["tag_name"]: tag for tag in dashboard_tags}
            for tag in chart_tags:
                if tag["tag_name"] not in tags_dict:
                    tags_dict[tag["tag_name"]] = tag
            tags_payload = {"tags": list(tags_dict.values())}
            files.append(
                (
                    "tags.yaml",
                    yaml.safe_dump(tags_payload, sort_keys=False),
                )
            )

        # Append theme YAML files to the bundle (populated above when
        # export_related=True and a theme is present).
        files.extend(_theme_files)

        # Bundle related charts + datasets + databases only when export_related is
        # True — 1:1 with superset_old/commands/dashboard/export.py:187 where the
        # entire chart/tags/theme/dataset block is guarded by ``if export_related:``.
        # When export_related=False (e.g. AsyncFullAssetManager passes False to avoid
        # cascading exports), only the dashboard YAML is emitted.
        if self._export_related:
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
                if feature_flag_manager.is_feature_enabled("TAGGING_SYSTEM"):
                    tags = getattr(chart, "tags", []) or []
                    chart_payload["tags"] = [
                        tag.name for tag in tags if tag.type == TagType.custom
                    ]
                chart_file_name = get_filename(chart.slice_name or "", chart.id)
                files.append(
                    (
                        f"charts/{chart_file_name}.yaml",
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
                    db_file_name = (
                        get_filename(
                            ds.database.database_name or "",
                            ds.database.id,
                            skip_id=True,
                        )
                        if ds.database
                        else "unknown"
                    )
                    ds_file_name = get_filename(ds.table_name or "", ds.id)
                    files.append(
                        (
                            f"datasets/{db_file_name}/{ds_file_name}.yaml",
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
                        await self._attach_ssh_tunnel(db_payload, db.id)
                        db_payload["version"] = EXPORT_VERSION
                        db_file_name = get_filename(
                            db.database_name or "", db.id, skip_id=True
                        )
                        files.append(
                            (
                                f"databases/{db_file_name}.yaml",
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
                            .options(
                                selectinload(SqlaTable.database),
                                selectinload(SqlaTable.metrics),
                                selectinload(SqlaTable.columns),
                            )
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
                            # Apply the same JSON normalization as the chart-datasource
                            # path and as ExportDatasetsCommand._file_content
                            # (superset_old/commands/dataset/export.py:62-76).
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
                            db_file_name = (
                                get_filename(
                                    ds.database.database_name or "",
                                    ds.database.id,
                                    skip_id=True,
                                )
                                if ds.database
                                else "unknown"
                            )
                            ds_file_name = get_filename(ds.table_name or "", ds.id)
                            files.append(
                                (
                                    f"datasets/{db_file_name}/{ds_file_name}.yaml",
                                    yaml.safe_dump(ds_payload, sort_keys=False),
                                )
                            )

                            # Emit the database YAML — mirrors ExportDatasetsCommand
                            # _export() (superset_old/commands/dataset/export.py:94-125)
                            # and the chart-datasource path above.
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
                                        db_payload["extra"] = _json.loads(
                                            db_payload["extra"]
                                        )
                                    except (TypeError, _json.JSONDecodeError):
                                        pass
                                await self._attach_ssh_tunnel(db_payload, db.id)
                                db_payload["version"] = EXPORT_VERSION
                                db_file_name_db = get_filename(
                                    db.database_name or "", db.id, skip_id=True
                                )
                                files.append(
                                    (
                                        f"databases/{db_file_name_db}.yaml",
                                        yaml.safe_dump(db_payload, sort_keys=False),
                                    )
                                )

        return files
