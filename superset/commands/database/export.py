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
"""Command for exporting database connections to a ZIP bundle."""

from __future__ import annotations

import json
import logging
from typing import Any, TYPE_CHECKING

import yaml  # type: ignore[import-untyped]

from superset.exceptions import CommandInvalidError, ObjectNotFoundError
from superset.importexport.export_base import AsyncExportModelsCommand
from superset.utils.file import get_filename
from superset.utils.ssh_tunnel import mask_password_info

if TYPE_CHECKING:
    from superset.db.daos.database import AsyncDatabaseDAO

logger = logging.getLogger(__name__)

EXPORT_VERSION = "1.0.0"


def _parse_extra(extra_payload: str) -> dict[str, Any]:
    """Parse the ``extra`` JSON field with legacy fixups."""
    try:
        extra = json.loads(extra_payload)
    except (json.JSONDecodeError, TypeError):
        logger.info("Unable to decode `extra` field: %s", extra_payload)
        return {}

    # Fix for DBs saved with an invalid ``schemas_allowed_for_csv_upload``.
    # Note: if the stored value is a string that is not valid JSON, json.loads
    # raises JSONDecodeError — intentionally NOT caught here.
    schemas_allowed = extra.get("schemas_allowed_for_csv_upload")
    if isinstance(schemas_allowed, str):
        extra["schemas_allowed_for_csv_upload"] = json.loads(schemas_allowed)
    return extra


class ExportDatabasesCommand(AsyncExportModelsCommand):
    """Export databases to a ZIP bundle, with related datasets when requested."""

    _resource_type = "Database"

    def __init__(
        self,
        model_ids: list[int],
        dao: AsyncDatabaseDAO | None = None,
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
        from superset.db.filters import database_access_filters
        from superset.models.core import Database

        await self._validate_access(
            self._dao, Database, database_access_filters, "Database"
        )

    @staticmethod
    def _file_name(model: Any) -> str:
        slug = get_filename(model.database_name, model.id, skip_id=True)
        return f"databases/{slug}.yaml"

    @staticmethod
    def _file_content(model: Any, ssh_tunnel: Any | None = None) -> str:
        payload = model.export_to_dict(
            recursive=False,
            include_parent_ref=False,
            include_defaults=True,
            export_uuids=True,
        )
        # V1 schema backward compat renames.
        replacements = {"allow_file_upload": "allow_csv_upload"}
        payload = {replacements.get(k, k): v for k, v in payload.items()}

        if payload.get("extra"):
            extra = payload["extra"] = _parse_extra(payload["extra"])
            if "schemas_allowed_for_file_upload" in extra:
                extra["schemas_allowed_for_csv_upload"] = extra.pop(
                    "schemas_allowed_for_file_upload"
                )

        if ssh_tunnel is not None:
            ssh_payload = ssh_tunnel.export_to_dict(
                recursive=False,
                include_parent_ref=False,
                include_defaults=True,
                export_uuids=False,
            )
            payload["ssh_tunnel"] = mask_password_info(ssh_payload)

        payload["version"] = EXPORT_VERSION
        return yaml.safe_dump(payload, sort_keys=False)

    async def _export_single(self, model_id: int) -> list[tuple[str, str]]:  # noqa: C901  # complex business logic
        if self._dao is None:
            raise CommandInvalidError("DAO not provided for export")
        database = await self._dao.find_by_id(model_id)
        if not database:
            raise ObjectNotFoundError("Database", model_id)

        ssh_tunnel = await self._dao.get_ssh_tunnel(model_id)

        files: list[tuple[str, str]] = [
            (self._file_name(database), self._file_content(database, ssh_tunnel))
        ]

        # export_related=False when the caller (e.g. full-bundle export manager)
        # already produces datasets via ExportDatasetsCommand.
        if self._export_related:
            datasets = await self._dao.get_datasets(model_id)
            db_slug = get_filename(database.database_name, database.id, skip_id=True)
            for dataset in datasets:
                ds_payload = dataset.export_to_dict(
                    recursive=True,
                    include_parent_ref=False,
                    include_defaults=True,
                    export_uuids=True,
                )
                ds_payload["version"] = EXPORT_VERSION
                # A NULL uuid serialises as the STRING "None", not YAML null,
                # so the importer's uuid-string lookup would miss it.
                ds_payload["database_uuid"] = str(getattr(database, "uuid", None))
                ds_slug = get_filename(
                    getattr(dataset, "table_name", ""), dataset.id, skip_id=True
                )
                files.append(
                    (
                        f"datasets/{db_slug}/{ds_slug}.yaml",
                        yaml.safe_dump(ds_payload, sort_keys=False),
                    )
                )

        return files
