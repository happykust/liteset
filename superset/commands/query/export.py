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
"""Export command for saved queries."""

from __future__ import annotations

import json as _json
import logging
from typing import Any, TYPE_CHECKING

import yaml

from superset.exceptions import CommandInvalidError, ObjectNotFoundError
from superset.importexport.export_base import AsyncExportModelsCommand
from superset.utils.file import secure_filename

if TYPE_CHECKING:
    from superset.typing import CRUDDAOProtocol

logger = logging.getLogger(__name__)

EXPORT_VERSION = "1.0.0"


class ExportSavedQueriesCommand(AsyncExportModelsCommand):
    """Export saved queries to a ZIP bundle.

    Uses ``SavedQuery.export_to_dict`` for the YAML payload.
    """

    _resource_type = "SavedQuery"

    def __init__(
        self,
        model_ids: list[int],
        dao: "CRUDDAOProtocol | None" = None,
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
        """Validate that requested IDs are accessible to the current user.

        Restricts results to ``SavedQuery.created_by == current_user``.
        If any requested ID is inaccessible, raises
        :class:`~superset.exceptions.ObjectNotFoundError` (404).
        """
        from superset.db.filters import saved_query_access_filters
        from superset.models.sql_lab import SavedQuery

        await self._validate_access(
            self._dao, SavedQuery, saved_query_access_filters, "SavedQuery"
        )

    @staticmethod
    def _file_name(model: Any) -> str:
        database_slug = (
            secure_filename(
                model.database.database_name if getattr(model, "database", None) else ""
            )
            or "unknown"
        )
        query_slug = secure_filename(getattr(model, "label", "") or "") or str(
            model.uuid
        )
        if getattr(model, "schema", None) is None:
            return f"queries/{database_slug}/{query_slug}.yaml"
        schema_slug = secure_filename(model.schema)
        return f"queries/{database_slug}/{schema_slug}/{query_slug}.yaml"

    async def _export_single(self, model_id: int) -> list[tuple[str, str]]:
        if self._dao is None:
            raise CommandInvalidError("DAO not provided")
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        from superset.models.sql_lab import SavedQuery

        stmt = (
            select(SavedQuery)
            .where(SavedQuery.id == model_id)
            .options(selectinload(SavedQuery.database))
        )
        result = await self._dao.session.execute(stmt)
        query = result.scalars().one_or_none()
        if not query:
            raise ObjectNotFoundError("SavedQuery", model_id)

        payload = query.export_to_dict(
            recursive=False,
            include_parent_ref=False,
            include_defaults=True,
            export_uuids=True,
        )
        payload["version"] = EXPORT_VERSION
        # Unconditional: if database is None this raises AttributeError at export
        # time (loud failure) rather than producing a non-importable ZIP.
        payload["database_uuid"] = str(query.database.uuid)

        files: list[tuple[str, str]] = [
            (self._file_name(query), yaml.safe_dump(payload, sort_keys=False)),
        ]

        db = getattr(query, "database", None)
        if db and self._export_related:
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
                    logger.info("Unable to decode `extra` field")
            db_payload["version"] = EXPORT_VERSION
            db_slug = secure_filename(db.database_name or "") or "unnamed"
            files.append(
                (
                    f"databases/{db_slug}.yaml",
                    yaml.safe_dump(db_payload, sort_keys=False),
                )
            )

        return files
