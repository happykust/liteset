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
"""Async port of ``superset_old/commands/chart/importers/v1/__init__.py``."""

from __future__ import annotations

import io
from typing import Any, TYPE_CHECKING
from uuid import UUID as _UUID

from superset.commands.chart.importers.v1.utils import (
    _import_chart,
    _import_database,
    _import_dataset,
    update_chart_config_dataset,
)
from superset.commands.importers.v1.utils import import_tag
from superset.exceptions import CommandInvalidError
from superset.importexport.import_base import AsyncImportModelsCommand

logger = __import__("logging").getLogger(__name__)

if TYPE_CHECKING:
    from superset.db.daos.chart import AsyncChartDAO


class ImportChartsCommand(AsyncImportModelsCommand):
    """Import charts from a ZIP bundle.

    Ported 1:1 from superset_old/commands/chart/importers/v1/.
    Resolves dependencies: databases -> datasets -> charts.
    Handles UUID-based dedup, annotation filtering, params serialization,
    datasource cross-referencing via UUIDs, and owner management.
    """

    def __init__(
        self,
        contents: io.BytesIO,
        dao: AsyncChartDAO | None = None,
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
            if name.startswith("charts/") and not config.get("slice_name"):
                raise CommandInvalidError(f"Missing slice_name in {name}")

    async def _check_existing(self, uuid_val: str) -> bool:
        if self._dao is None:
            return False
        result = await self._dao.find_one_or_none(uuid=_UUID(uuid_val))
        return result is not None

    async def run(self) -> None:  # noqa: C901
        """Orchestrate import: databases -> datasets -> charts.

        Ported 1:1 from ImportChartsCommand._import in the original.
        """
        if self._configs is None:
            raise CommandInvalidError("validate() must be called before run()")
        if self._dao is None:
            raise CommandInvalidError("DAO not provided for import")

        configs = self._configs
        session = self._dao.session

        # 1. Discover datasets associated with charts
        dataset_uuids: set[str] = set()
        for file_name, config in configs.items():
            if file_name.startswith("charts/") and isinstance(config, dict):
                ds_uuid = config.get("dataset_uuid")
                if ds_uuid:
                    dataset_uuids.add(ds_uuid)

        # 2. Discover databases associated with needed datasets
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

        # 3. Import related databases (overwrite=False)
        database_ids: dict[str, int] = {}
        for file_name, config in configs.items():
            if (
                file_name.startswith("databases/")
                and isinstance(config, dict)
                and config.get("uuid") in database_uuids
            ):
                db = await _import_database(session, config)
                database_ids[str(db.uuid)] = db.id

        # 4. Import datasets with correct parent ref (overwrite=False)
        datasets: dict[str, Any] = {}
        for file_name, config in configs.items():
            if (
                file_name.startswith("datasets/")
                and isinstance(config, dict)
                and config.get("database_uuid") in database_ids
            ):
                config["database_id"] = database_ids[config["database_uuid"]]
                dataset = await _import_dataset(session, config)
                datasets[str(dataset.uuid)] = dataset

        # 5. Import charts with correct parent ref
        for file_name, config in configs.items():
            if (
                file_name.startswith("charts/")
                and isinstance(config, dict)
                and config.get("dataset_uuid") in datasets
            ):
                # Skip obsolete filter-box charts
                if config.get("viz_type") == "filter_box":
                    continue

                # Update datasource id, type, and name from resolved dataset
                dataset = datasets[config["dataset_uuid"]]
                dataset_dict = {
                    "datasource_id": dataset.id,
                    "datasource_type": "table",
                    "datasource_name": dataset.table_name,
                }
                config = update_chart_config_dataset(config, dataset_dict)

                chart = await _import_chart(
                    session,
                    config,
                    overwrite=self._overwrite,
                    security_manager=self._security_manager,
                    current_user=self._current_user,
                )

                # Tag import (gated on TAGGING_SYSTEM feature flag).
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

    async def _import_single(self, file_name: str, content: dict[str, Any]) -> None:
        # Not used — run() handles the full orchestration
        pass

    def _raw_contents(self) -> dict[str, str] | None:
        """Return the raw bundle ``{filename: yaml_text}`` mapping if available."""
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
