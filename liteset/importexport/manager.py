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
"""Async import/export manager — coordinates bulk operations."""

from __future__ import annotations

import io
import logging
import zipfile
from datetime import datetime, timezone
from typing import Any

import yaml  # type: ignore[import-untyped]

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


class ImportResult:
    """Result of an import operation."""

    def __init__(self) -> None:
        self.imported: dict[str, int] = {}
        self.errors: list[str] = []

    @property
    def success(self) -> bool:
        return len(self.errors) == 0


class AsyncFullAssetManager:
    """Full-asset import/export manager."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def export_assets(
        self,
        asset_types: list[str] | None = None,
    ) -> bytes:
        """Export all assets as a ZIP file.

        Returns bytes of the ZIP archive.
        """
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            metadata = {
                "version": "1.0.0",
                "type": "assets",
                "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            }
            zf.writestr("metadata.yaml", yaml.safe_dump(metadata, sort_keys=False))

            types_to_export = asset_types or [
                "databases",
                "datasets",
                "charts",
                "dashboards",
                "queries",
            ]

            for asset_type in types_to_export:
                try:
                    items = await self._export_type(asset_type)
                    for name, content in items:
                        zf.writestr(f"{asset_type}/{name}", content)
                except Exception:
                    logger.warning("Failed to export %s", asset_type, exc_info=True)

        return buf.getvalue()

    async def _export_type(self, asset_type: str) -> list[tuple[str, str]]:
        """Export all items of a given type. Returns list of (filename, yaml_content)."""
        # Placeholder — actual implementation would query DAOs
        return []

    async def import_assets(
        self,
        file_content: bytes,
        overwrite: bool = False,
        passwords: dict[str, str] | None = None,
    ) -> ImportResult:
        """Import assets from a ZIP file."""
        result = ImportResult()

        try:
            with zipfile.ZipFile(io.BytesIO(file_content)) as zf:
                entries = [n for n in zf.namelist() if not n.endswith("/")]

                # Parse metadata
                if "metadata.yaml" in entries:
                    metadata = yaml.safe_load(zf.read("metadata.yaml"))
                    logger.info(
                        "Importing assets version %s", metadata.get("version")
                    )

                # Group by type
                by_type: dict[str, list[str]] = {}
                for entry in entries:
                    if entry == "metadata.yaml":
                        continue
                    parts = entry.split("/", 1)
                    if len(parts) == 2:
                        asset_type, _name = parts
                        by_type.setdefault(asset_type, []).append(entry)

                # Import each type
                for asset_type, filenames in by_type.items():
                    try:
                        count = await self._import_type(
                            zf, asset_type, filenames, overwrite, passwords
                        )
                        result.imported[asset_type] = count
                    except Exception as e:
                        result.errors.append(f"Failed to import {asset_type}: {e}")

        except zipfile.BadZipFile:
            result.errors.append("Invalid ZIP file")

        return result

    async def _import_type(
        self,
        zf: zipfile.ZipFile,
        asset_type: str,
        filenames: list[str],
        overwrite: bool,
        passwords: dict[str, str] | None,
    ) -> int:
        """Import items of a given type. Returns count imported."""
        # Placeholder — actual implementation would use type-specific importers
        return 0


class AsyncImportExportManager:
    """Facade for import/export operations."""

    _EXPORT_COMMANDS: dict[str, type] = {}
    _IMPORT_COMMANDS: dict[str, type] = {}

    @classmethod
    def register_export(cls, resource_type: str, command_cls: type) -> None:
        cls._EXPORT_COMMANDS[resource_type] = command_cls

    @classmethod
    def register_import(cls, resource_type: str, command_cls: type) -> None:
        cls._IMPORT_COMMANDS[resource_type] = command_cls

    @classmethod
    async def export(cls, resource_type: str, model_ids: list[int]) -> io.BytesIO:
        if resource_type not in cls._EXPORT_COMMANDS:
            raise ValueError(f"No export command registered for: {resource_type}")
        cmd_cls = cls._EXPORT_COMMANDS[resource_type]
        cmd = cmd_cls(model_ids=model_ids)
        return await cmd.execute()

    @classmethod
    async def import_models(
        cls,
        resource_type: str,
        contents: io.BytesIO,
        **kwargs: Any,
    ) -> None:
        if resource_type not in cls._IMPORT_COMMANDS:
            raise ValueError(f"No import command registered for: {resource_type}")
        cmd_cls = cls._IMPORT_COMMANDS[resource_type]
        cmd = cmd_cls(contents=contents, **kwargs)
        await cmd.execute()
