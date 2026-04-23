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
"""Async port of ``superset_old/commands/dataset/export.py``."""

from __future__ import annotations

import json
import logging
from typing import Any, TYPE_CHECKING

import yaml  # type: ignore[import-untyped]
from werkzeug.utils import secure_filename

from superset.exceptions import CommandInvalidError, ObjectNotFoundError
from superset.importexport.export_base import AsyncExportModelsCommand
from superset.utils.ssh_tunnel import mask_password_info

if TYPE_CHECKING:
    from superset.db.daos.dataset import AsyncDatasetDAO

logger = logging.getLogger(__name__)

EXPORT_VERSION = "1.0.0"
JSON_KEYS = ("params", "template_params", "extra")


class ExportDatasetsCommand(AsyncExportModelsCommand):
    """Export datasets to a ZIP bundle (1:1 with the original).

    Uses ``SqlaTable.export_to_dict(recursive=True, include_defaults=True,
    export_uuids=True)`` to grab columns + metrics in the same call,
    decodes JSON string fields for human-readable YAML, and bundles the
    parent database alongside.
    """

    _resource_type = "SqlaTable"

    def __init__(
        self, model_ids: list[int], dao: AsyncDatasetDAO | None = None
    ) -> None:
        super().__init__(model_ids)
        self._dao = dao

    @staticmethod
    def _file_name(model: Any) -> str:
        db_slug = (
            secure_filename(model.database.database_name)
            if model.database
            else "unknown"
        ) or "unknown"
        ds_slug = secure_filename(model.table_name or "") or "unnamed"
        return f"datasets/{db_slug}/{ds_slug}_{model.id}.yaml"

    @staticmethod
    def _file_content(model: Any) -> str:
        payload = model.export_to_dict(
            recursive=True,
            include_parent_ref=False,
            include_defaults=True,
            export_uuids=True,
        )
        for key in JSON_KEYS:
            if payload.get(key):
                try:
                    payload[key] = json.loads(payload[key])
                except (TypeError, json.JSONDecodeError):
                    logger.info("Unable to decode `%s` field: %s", key, payload[key])
        for nested in ("metrics", "columns"):
            for attrs in payload.get(nested, []) or []:
                if isinstance(attrs.get("extra"), str):
                    try:
                        attrs["extra"] = json.loads(attrs["extra"])
                    except (TypeError, json.JSONDecodeError):
                        logger.info(
                            "Unable to decode `extra` field: %s",
                            attrs["extra"],
                        )

        payload["version"] = EXPORT_VERSION
        payload["database_uuid"] = str(model.database.uuid) if model.database else None
        return yaml.safe_dump(payload, sort_keys=False)

    async def _export_single(self, model_id: int) -> list[tuple[str, str]]:
        if self._dao is None:
            raise CommandInvalidError("DAO not provided for export")

        from sqlalchemy.orm import selectinload

        from superset.models.connectors import SqlaTable

        dataset = await self._dao.find_by_id_with_options(
            model_id,
            options=[
                selectinload(SqlaTable.database),
                selectinload(SqlaTable.columns),
                selectinload(SqlaTable.metrics),
                selectinload(SqlaTable.owners),
            ],
        )
        if not dataset:
            raise ObjectNotFoundError("Dataset", model_id)

        files: list[tuple[str, str]] = [
            (self._file_name(dataset), self._file_content(dataset))
        ]

        # Related database YAML (recursive=False) — matches the original.
        db = getattr(dataset, "database", None)
        if db:
            db_payload = db.export_to_dict(
                recursive=False,
                include_parent_ref=False,
                include_defaults=True,
                export_uuids=True,
            )
            if db_payload.get("extra"):
                try:
                    db_payload["extra"] = json.loads(db_payload["extra"])
                except (TypeError, json.JSONDecodeError):
                    logger.info(
                        "Unable to decode `extra` field: %s", db_payload["extra"]
                    )

            # SSH tunnel
            try:
                from superset.db.daos.database import AsyncSSHTunnelDAO

                ssh_dao = AsyncSSHTunnelDAO(self._dao.session)
                ssh_tunnel = await ssh_dao.get_by_database_id(db.id)
                if ssh_tunnel:
                    ssh_payload = ssh_tunnel.export_to_dict(
                        recursive=False,
                        include_parent_ref=False,
                        include_defaults=True,
                        export_uuids=False,
                    )
                    db_payload["ssh_tunnel"] = mask_password_info(ssh_payload)
            except ImportError:
                pass

            db_payload["version"] = EXPORT_VERSION

            db_slug = secure_filename(db.database_name or "") or "unnamed"
            files.append(
                (
                    f"databases/{db_slug}.yaml",
                    yaml.safe_dump(db_payload, sort_keys=False),
                )
            )

        return files
