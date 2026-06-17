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
"""Chart import command — v1 format."""

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

    Resolves dependencies: databases -> datasets -> charts.
    Handles UUID-based dedup, annotation filtering, params serialization,
    datasource cross-referencing via UUIDs, and owner management.
    """

    # The manifest's ``type`` must match the exported resource type (``Slice``).
    _expected_type = "Slice"

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
            # A bundled YAML file may parse to a non-dict (list/scalar); guard
            # before ``.get`` so a malformed file is skipped (matching ``run()``'s
            # ``isinstance`` checks) rather than raising AttributeError → HTTP 500.
            if (
                name.startswith("charts/")
                and isinstance(config, dict)
                and not config.get("slice_name")
            ):
                raise CommandInvalidError(f"Missing slice_name in {name}")

    async def _check_existing(self, uuid_val: str) -> bool:
        if self._dao is None:
            return False
        result = await self._dao.find_one_or_none(uuid=_UUID(uuid_val))
        return result is not None

    async def run(self) -> None:  # noqa: C901
        """Orchestrate import: databases -> datasets -> charts."""
        if self._configs is None:
            raise CommandInvalidError("validate() must be called before run()")
        if self._dao is None:
            raise CommandInvalidError("DAO not provided for import")

        configs = self._configs
        session = self._dao.session

        dataset_uuids: set[str] = set()
        for file_name, config in configs.items():
            if file_name.startswith("charts/") and isinstance(config, dict):
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

        database_ids: dict[str, int] = {}
        for file_name, config in configs.items():
            if (
                file_name.startswith("databases/")
                and isinstance(config, dict)
                and config.get("uuid") in database_uuids
            ):
                db = await _import_database(session, config)
                database_ids[str(db.uuid)] = db.id

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

        for file_name, config in configs.items():
            if (
                file_name.startswith("charts/")
                and isinstance(config, dict)
                and config.get("dataset_uuid") in datasets
            ):
                # Skip obsolete filter-box charts
                if config.get("viz_type") == "filter_box":
                    continue

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

                # Tag import (gated on TAGGING_SYSTEM inside ``import_tag``).
                # No try/except — upstream lets a tag-import failure roll the
                # whole import back (R11-05).
                if "tags" in config and config["tags"]:
                    await import_tag(
                        list(config["tags"]),
                        self._raw_contents() or {},
                        chart.id,
                        "chart",
                        session,
                    )

    async def _import_single(self, file_name: str, content: dict[str, Any]) -> None:
        pass  # run() handles full orchestration

    def _raw_contents(self) -> dict[str, str] | None:
        """Return the raw bundle ``{filename: yaml_text}`` mapping if available.

        Entry names are stripped of the export-root folder (``remove_root``,
        same as ``_parse_zip``) — consumers like ``import_tag`` look up
        ``"tags.yaml"``, not ``"chart_export_<ts>/tags.yaml"``.
        """
        try:
            import io
            import zipfile
            from pathlib import PurePosixPath

            buf = self._contents
            if buf is None:
                return None
            buf.seek(0)
            data = buf.read()
            buf.seek(0)
            out: dict[str, str] = {}
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                for name in zf.namelist():
                    if name.endswith("/"):
                        continue
                    parts = PurePosixPath(name).parts
                    safe_parts = tuple(p for p in parts if p not in ("..", "/"))
                    if not safe_parts:
                        continue
                    if len(safe_parts) > 1:
                        rel = str(PurePosixPath(*safe_parts[1:]))
                    else:
                        rel = safe_parts[0]
                    out[rel] = zf.read(name).decode("utf-8")
            return out
        except Exception:  # noqa: BLE001
            return None
