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
"""Base async import command — reads YAML+ZIP bundles."""

from __future__ import annotations

import asyncio
import io
import zipfile
from abc import abstractmethod
from pathlib import PurePosixPath
from typing import Any

import yaml

from superset.commands.base import AsyncBaseCommand
from superset.exceptions import CommandInvalidError

MAX_ZIP_ENTRIES = 500
MAX_ENTRY_SIZE = 50 * 1024 * 1024  # 50 MB


class AsyncImportModelsCommand(AsyncBaseCommand[None]):
    """Import models from a ZIP file containing YAML manifests."""

    _expected_type: str = ""  # Override in subclasses to validate metadata type

    def __init__(
        self,
        contents: io.BytesIO,
        passwords: dict[str, str] | None = None,
        ssh_tunnel_passwords: dict[str, str] | None = None,
        overwrite: bool = False,
    ) -> None:
        self._contents = contents
        self._passwords = passwords or {}
        self._ssh_tunnel_passwords = ssh_tunnel_passwords or {}
        self._overwrite = overwrite
        self._configs: dict[str, dict[str, Any]] | None = None

    def _parse_zip(self) -> dict[str, dict[str, Any]]:
        """Parse ZIP file into {filename: parsed_yaml_dict}."""
        configs: dict[str, dict[str, Any]] = {}
        with zipfile.ZipFile(self._contents) as zf:
            entries = [n for n in zf.namelist() if not n.endswith("/")]
            if len(entries) > MAX_ZIP_ENTRIES:
                raise ValueError(
                    f"ZIP contains too many entries "
                    f"({len(entries)} > {MAX_ZIP_ENTRIES})"
                )
            for name in entries:
                # Sanitize path — prevent directory traversal
                parts = PurePosixPath(name).parts
                safe_name = str(
                    PurePosixPath(*[p for p in parts if p not in ("..", "/")])
                )
                raw = zf.read(name)
                if len(raw) > MAX_ENTRY_SIZE:
                    raise ValueError(
                        f"ZIP entry '{name}' too large "
                        f"({len(raw)} > {MAX_ENTRY_SIZE} bytes)"
                    )
                configs[safe_name] = yaml.safe_load(raw)
        return configs

    async def validate(self) -> None:
        self._contents.seek(0)
        self._configs = await asyncio.to_thread(self._parse_zip)

        # Validate metadata.yaml presence and version
        metadata = self._configs.get("metadata.yaml")
        if metadata is None:
            raise CommandInvalidError("Missing metadata.yaml in import bundle")
        version = metadata.get("version") if isinstance(metadata, dict) else None
        if version != "1.0.0":
            raise CommandInvalidError(f"Unsupported import version: {version}")
        # Validate metadata type if subclass specifies expected type
        if self._expected_type and isinstance(metadata, dict):
            meta_type = metadata.get("type")
            if meta_type and meta_type != self._expected_type:
                raise CommandInvalidError(
                    f"Expected type '{self._expected_type}', got '{meta_type}'"
                )

        # Filter metadata.yaml before validation to match run() behavior
        validatable = {k: v for k, v in self._configs.items() if k != "metadata.yaml"}

        # Note: UUID-existence checking is NOT done here.  The orchestrated
        # import commands (ImportChartsCommand, ImportDashboardsCommand) handle
        # dedup themselves because dependencies (databases, datasets) should be
        # reused when they already exist, while only the primary resource type
        # respects the user's overwrite flag.  Subclasses that need a blanket
        # pre-check can implement it in their _validate() override.

        # Apply password substitutions before subclass validation
        for _file_name, content in validatable.items():
            if isinstance(content, dict):
                self._apply_password(content)

        await self._validate(validatable)

    async def run(self) -> None:
        # Ensure validate() was called first — it parses the ZIP and runs
        # security/overwrite checks.  Silently re-parsing would bypass those.
        if self._configs is None:
            raise CommandInvalidError("validate() must be called before run()")
        configs = self._configs
        for file_name, content in configs.items():
            if file_name == "metadata.yaml":
                continue
            if isinstance(content, dict):
                content = self._apply_password(content)
            await self._import_single(file_name, content)

    def _apply_password(self, content: dict[str, Any]) -> dict[str, Any]:
        """Substitute masked passwords in database URIs."""
        uuid = content.get("uuid", "")
        if uuid and self._passwords and uuid in self._passwords:
            uri = content.get("sqlalchemy_uri", "")
            if "XXXXXXXXXX" in uri:
                content["sqlalchemy_uri"] = uri.replace(
                    "XXXXXXXXXX", self._passwords[uuid]
                )
        if uuid and self._ssh_tunnel_passwords and uuid in self._ssh_tunnel_passwords:
            ssh = content.get("ssh_tunnel", {})
            if ssh and "XXXXXXXXXX" in ssh.get("password", ""):
                ssh["password"] = self._ssh_tunnel_passwords[uuid]
        return content

    @abstractmethod
    async def _import_single(self, file_name: str, content: dict[str, Any]) -> None: ...

    @abstractmethod
    async def _validate(self, configs: dict[str, dict[str, Any]]) -> None: ...

    async def _check_existing(self, uuid_val: str) -> bool:
        """Check if an object with this UUID already exists.

        Subclasses should override with UUID-based DB lookup.
        Default implementation returns False (no check).
        """
        return False
