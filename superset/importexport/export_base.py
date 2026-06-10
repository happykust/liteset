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
"""Base async export command — produces YAML+ZIP bundles."""

from __future__ import annotations

import io
import re
import zipfile
from abc import abstractmethod
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any

import yaml

from superset.commands.base import AsyncBaseCommand


class AsyncExportModelsCommand(AsyncBaseCommand[io.BytesIO]):
    """Export models to a ZIP file containing YAML manifests.
    Subclasses implement _export_single() for each model type.
    """

    _resource_type: str = ""  # Override in subclasses

    def __init__(
        self,
        model_ids: list[int],
        root: str | None = None,
        security_manager: Any = None,
        user: Any = None,
        export_related: bool = True,
    ) -> None:
        self._model_ids = model_ids
        # Optional root folder under which every ZIP entry is nested. The
        # original API handlers (e.g.
        # ``superset_old/datasets/api.py:553-579``) build
        # ``root = f"{type}_export_{timestamp}"`` and write each entry as
        # ``f"{root}/{file_name}"``; the importer strips it back off via
        # ``remove_root`` (``parts[1:]``).
        self._root = root
        self._security_manager = security_manager
        self._user = user
        # 1:1 with superset_old/commands/export/models.py:39 — stored so
        # subclasses can gate related-resource emission on this flag
        # (e.g. tags.yaml is skipped in full-bundle exports).
        self._export_related = export_related

    async def validate(self) -> None:
        """No-op at the base level; subclasses override with ``_validate_access``."""

    async def _validate_access(
        self,
        dao: Any,
        model_cls: Any,
        access_filters: Any,
        not_found_name: str,
    ) -> None:
        """Restrict the export to IDs the current user is allowed to see.

        1:1 with ``superset_old/commands/export/models.py:validate()`` which
        calls ``dao.find_by_ids(model_ids)`` — that applies the DAO
        ``base_filter`` (``ChartFilter`` / ``DashboardAccessFilter`` /
        ``DatabaseFilter`` / ``DatasourceFilter``), restricting results to
        models the user can access, and raises ``not_found`` when the number
        found differs from the number requested.  Without this an authenticated
        user holding only ``can_export`` could export objects they cannot read
        (information disclosure / IDOR).
        """
        from superset.exceptions import CommandInvalidError, ObjectNotFoundError

        if dao is None:
            raise CommandInvalidError("DAO not provided for export")
        # CLI callers (e.g. superset/cli/importexport.py) do not inject a
        # security_manager — they run as the OS process with admin-level DB
        # access.  The original CLI (superset_old/cli/importexport.py:76) set
        # g.user=admin which made every access-filter return [] immediately.
        # Guard here to reproduce that behaviour: no security_manager → skip
        # the filter check and allow all requested IDs.
        if self._security_manager is None:
            return
        base_filters = await access_filters(self._security_manager, self._user)
        filters = [model_cls.id.in_(self._model_ids)]
        if base_filters:
            filters.extend(base_filters)
        accessible_count = await dao.count(filters=filters)
        if accessible_count != len(self._model_ids):
            raise ObjectNotFoundError(not_found_name, None)

    async def run(self) -> io.BytesIO:
        buf = io.BytesIO()
        seen: set[str] = set()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            # Write metadata.yaml first
            metadata = {
                "version": "1.0.0",
                "type": self._resource_type,
                "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            }
            zf.writestr(
                self._with_root("metadata.yaml"),
                yaml.safe_dump(metadata, sort_keys=False),
            )
            seen.add("metadata.yaml")

            for model_id in self._model_ids:
                files = await self._export_single(model_id)
                for file_name, content in files:
                    # Sanitize path — prevent directory traversal
                    parts = PurePosixPath(file_name).parts
                    safe_name = str(
                        PurePosixPath(*[p for p in parts if p not in ("..", "/")])
                    )
                    # Sanitize remaining unsafe characters
                    safe_name = re.sub(r'[\x00\\:*?"<>|]', "_", safe_name)
                    # Avoid duplicate entries (e.g. metadata.yaml from each model)
                    if safe_name not in seen:
                        zf.writestr(self._with_root(safe_name), content)
                        seen.add(safe_name)
        buf.seek(0)
        return buf

    def _with_root(self, file_name: str) -> str:
        """Prefix ``file_name`` with the export root folder, when set."""
        if self._root:
            return f"{self._root}/{file_name}"
        return file_name

    @abstractmethod
    async def _export_single(self, model_id: int) -> list[tuple[str, str]]:
        """Return list of (file_path, yaml_content) for one model."""
        ...
