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
"""Dashboard importer (v1 ZIP bundle format)."""

from __future__ import annotations

import io
import logging
from typing import Any, TYPE_CHECKING
from uuid import UUID as _UUID

from superset.commands.chart.importers.v1.utils import (
    _import_chart,
    _import_database,
    _import_dataset,
    update_chart_config_dataset,
)
from superset.commands.dashboard.importers.v1.utils import (
    _import_dashboard,
    find_chart_uuids,
    find_native_filter_datasets,
    update_id_refs,
)
from superset.commands.importers.v1.utils import import_tag
from superset.exceptions import CommandInvalidError
from superset.importexport.import_base import AsyncImportModelsCommand

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from superset.db.daos.dashboard import AsyncDashboardDAO
    from superset.models.dashboard import Dashboard


class ImportDashboardsCommand(AsyncImportModelsCommand):
    """
    Import dashboards from a ZIP bundle
    (databases -> datasets -> charts -> dashboards).
    """

    _expected_type = "Dashboard"

    def __init__(
        self,
        contents: io.BytesIO,
        dao: AsyncDashboardDAO | None = None,
        security_manager: Any | None = None,
        current_user: Any | None = None,
        ignore_permissions: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(contents, **kwargs)
        self._dao = dao
        self._security_manager = security_manager
        self._current_user = current_user
        # Explicit opt-out for operator-run contexts with no HTTP user (the
        # CLI). Defaulting to permissive is the defect this replaced.
        self._ignore_permissions = ignore_permissions

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
        if self._configs is None:
            raise CommandInvalidError("validate() must be called before run()")
        if self._dao is None:
            raise CommandInvalidError("DAO not provided for import")

        from sqlalchemy import select as sa_select

        from superset.models.dashboard import dashboard_slices

        configs = self._configs
        session = self._dao.session

        chart_uuids: set[str] = set()
        dataset_uuids: set[str] = set()
        theme_uuids: set[str] = set()
        for file_name, config in configs.items():
            if file_name.startswith("dashboards/") and isinstance(config, dict):
                chart_uuids.update(find_chart_uuids(config.get("position", {})))
                dataset_uuids.update(
                    find_native_filter_datasets(config.get("metadata", {}))
                )
                if config.get("theme_uuid"):
                    theme_uuids.add(config["theme_uuid"])

        for file_name, config in configs.items():
            if (
                file_name.startswith("charts/")
                and isinstance(config, dict)
                and config.get("uuid") in chart_uuids
            ):
                ds_uuid = config.get("dataset_uuid")
                if ds_uuid:
                    dataset_uuids.add(ds_uuid)

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

        from superset.commands.theme_import import import_theme

        theme_ids: dict[str, int] = {}
        for file_name, config in configs.items():
            if (
                file_name.startswith("themes/")
                and isinstance(config, dict)
                and config.get("uuid") in theme_uuids
            ):
                theme = await import_theme(session, config, overwrite=False)
                if theme is not None:
                    theme_ids[str(theme.uuid)] = theme.id

        database_ids: dict[str, int] = {}
        for file_name, config in configs.items():
            if (
                file_name.startswith("databases/")
                and isinstance(config, dict)
                and config.get("uuid") in database_uuids
            ):
                db = await _import_database(
                    session,
                    config,
                    security_manager=self._security_manager,
                    ignore_permissions=self._ignore_permissions,
                )
                database_ids[str(db.uuid)] = db.id

        dataset_info: dict[str, dict[str, Any]] = {}
        for file_name, config in configs.items():
            if (
                file_name.startswith("datasets/")
                and isinstance(config, dict)
                and config.get("database_uuid") in database_ids
            ):
                config["database_id"] = database_ids[config["database_uuid"]]
                dataset = await _import_dataset(
                    session,
                    config,
                    security_manager=self._security_manager,
                    current_user=self._current_user,
                    ignore_permissions=self._ignore_permissions,
                )
                dataset_info[str(dataset.uuid)] = {
                    "datasource_id": dataset.id,
                    "datasource_type": getattr(dataset, "datasource_type", "table"),
                    "datasource_name": dataset.table_name,
                }

        charts: list[Any] = []
        chart_ids: dict[str, int] = {}
        for file_name, config in configs.items():
            if (
                file_name.startswith("charts/")
                and isinstance(config, dict)
                and config.get("dataset_uuid") in dataset_info
            ):
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

                if "tags" in config and config["tags"]:
                    try:
                        await import_tag(
                            list(config["tags"]),
                            self._raw_contents() or {},
                            chart.id,
                            "chart",
                            session,
                        )
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "Failed to import tags for chart %s",
                            chart.id,
                        )

        existing_relationships_stmt = sa_select(
            dashboard_slices.c.dashboard_id,
            dashboard_slices.c.slice_id,
        )
        existing_result = await session.execute(existing_relationships_stmt)
        existing_relationships = set(existing_result.fetchall())

        dashboards: list[Dashboard] = []
        dashboard_chart_ids: list[tuple[int, int]] = []
        for file_name, config in configs.items():
            if file_name.startswith("dashboards/") and isinstance(config, dict):
                config = update_id_refs(config, chart_ids, dataset_info)

                if "theme_uuid" in config:
                    if config["theme_uuid"] in theme_ids:
                        config["theme_id"] = theme_ids[config["theme_uuid"]]
                    else:
                        config["theme_id"] = None
                    del config["theme_uuid"]

                dashboard = await _import_dashboard(
                    session,
                    config,
                    overwrite=self._overwrite,
                    security_manager=self._security_manager,
                    current_user=self._current_user,
                )
                dashboards.append(dashboard)

                for uuid_str in find_chart_uuids(config.get("position", {})):
                    if uuid_str not in chart_ids:
                        break
                    chart_id = chart_ids[uuid_str]
                    if (dashboard.id, chart_id) not in existing_relationships:
                        dashboard_chart_ids.append((dashboard.id, chart_id))

                if "tags" in config and config["tags"]:
                    try:
                        await import_tag(
                            list(config["tags"]),
                            self._raw_contents() or {},
                            dashboard.id,
                            "dashboard",
                            session,
                        )
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "Failed to import tags for dashboard %s",
                            dashboard.id,
                        )

        if dashboard_chart_ids:
            values = [
                {"dashboard_id": dashboard_id, "slice_id": chart_id}
                for (dashboard_id, chart_id) in dashboard_chart_ids
            ]
            await session.execute(dashboard_slices.insert(), values)

        from superset.migrations.shared.native_filters import migrate_dashboard

        for dashboard in dashboards:
            try:
                # Refresh slices before migrate_dashboard mutates them.
                await session.refresh(dashboard, ["slices"])
                migrate_dashboard(dashboard)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Skipping native-filter migration for dashboard %s",
                    getattr(dashboard, "id", None),
                )

        for chart in charts:
            if getattr(chart, "viz_type", None) == "filter_box":
                await session.delete(chart)

    async def _import_single(self, file_name: str, content: dict[str, Any]) -> None:
        pass  # run() handles the full orchestration

    def _raw_contents(self) -> dict[str, str] | None:
        """Return the raw bundle ``{filename: yaml_text}`` mapping
        for the tag importer."""
        try:
            import io
            import zipfile

            buf = self._contents
            if buf is None:
                return None
            buf.seek(0)
            data = buf.read()
            buf.seek(0)
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                return {
                    name: zf.read(name).decode("utf-8")
                    for name in zf.namelist()
                    if not name.endswith("/")
                }
        except Exception:  # noqa: BLE001
            return None
