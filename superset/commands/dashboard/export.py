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
"""Dashboard export command."""

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

_JSON_KEYS = {"position_json": "position", "json_metadata": "metadata"}


def _suffix(length: int = 8) -> str:
    return "".join(
        random.SystemRandom().choice(string.ascii_uppercase + string.digits)
        for _ in range(length)
    )


class ExportDashboardsCommand(AsyncExportModelsCommand):
    """Export dashboards to a ZIP bundle."""

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
        """Attach the masked SSH-tunnel payload to a database YAML payload."""
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

        # Eager-load all relationships export_to_dict will walk. Without
        # selectinload SA fires implicit lazy-loads that fail with MissingGreenlet.
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

        payload: dict[str, Any] = dashboard.export_to_dict(
            recursive=False,
            include_parent_ref=False,
            include_defaults=True,
            export_uuids=True,
        )

        # Decode JSON strings to dicts; coerce non-object values (``[1,2]``,
        # ``"s"``) to ``{}`` — metadata.get() and find_chart_uuids() would
        # raise on a non-dict, producing HTTP 500 on export.
        for key, new_name in _JSON_KEYS.items():
            value: str | None = payload.pop(key, None)
            if value:
                try:
                    parsed = _json.loads(value)
                except (_json.JSONDecodeError, TypeError):
                    logger.info("Unable to decode `%s` field: %s", key, value)
                    parsed = {}
                payload[new_name] = parsed if isinstance(parsed, dict) else {}

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

        referenced_charts = find_chart_uuids(payload["position"])
        slices = dashboard.slices or []
        orphan_charts = {
            chart for chart in slices if str(chart.uuid) not in referenced_charts
        }
        if orphan_charts:
            payload["position"] = _append_charts(payload["position"], orphan_charts)

        theme = getattr(dashboard, "theme", None)
        payload["theme_uuid"] = str(theme.uuid) if theme else None

        payload["version"] = EXPORT_VERSION

        # When export_related=False, skip theme YAML — the full-assets bundle
        # exports themes separately; emitting here would inject spurious entries.
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

        if feature_flag_manager.is_feature_enabled("TAGGING_SYSTEM"):
            tags = getattr(dashboard, "tags", []) or []
            payload["tags"] = [tag.name for tag in tags if tag.type == TagType.custom]

        files: list[tuple[str, str]] = [
            (
                self._file_name(dashboard),
                yaml.safe_dump(payload, sort_keys=False),
            )
        ]

        # When export_related=False, skip tags.yaml — no prior phase emits one in
        # the full-assets bundle; emitting here would produce partial tag data.
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

        files.extend(_theme_files)

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
