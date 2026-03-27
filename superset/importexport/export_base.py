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

import yaml  # type: ignore[import-untyped]

from superset.commands.base import AsyncBaseCommand


class AsyncExportModelsCommand(AsyncBaseCommand[io.BytesIO]):
    """Export models to a ZIP file containing YAML manifests.
    Subclasses implement _export_single() for each model type.
    """

    _resource_type: str = ""  # Override in subclasses

    def __init__(self, model_ids: list[int]) -> None:
        self._model_ids = model_ids

    async def validate(self) -> None:
        pass

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
            zf.writestr("metadata.yaml", yaml.safe_dump(metadata, sort_keys=False))
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
                        zf.writestr(safe_name, content)
                        seen.add(safe_name)
        buf.seek(0)
        return buf

    @abstractmethod
    async def _export_single(self, model_id: int) -> list[tuple[str, str]]:
        """Return list of (file_path, yaml_content) for one model."""
        ...
