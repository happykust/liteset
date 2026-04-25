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
"""Async port of ``superset_old/commands/annotation_layer/delete.py``."""

from __future__ import annotations

from typing import Any

from superset.commands.base import AsyncBaseCommand
from superset.exceptions import CommandInvalidError, ObjectNotFoundError


class DeleteAnnotationLayerCommand(AsyncBaseCommand[None]):
    """Delete a single annotation layer."""

    def __init__(self, dao: Any, pk: int) -> None:
        self._dao = dao
        self._pk = pk
        self._layer: Any | None = None

    async def validate(self) -> None:
        self._layer = await self._dao.find_by_id(self._pk)
        if not self._layer:
            raise ObjectNotFoundError("AnnotationLayer", self._pk)
        has_children = await self._dao.has_annotations(self._pk)
        if has_children:
            raise CommandInvalidError(
                "Cannot delete annotation layer that contains annotations"
            )

    async def run(self) -> None:
        assert self._layer is not None
        await self._dao.delete([self._layer])
        await self._dao.session.flush()


class BulkDeleteAnnotationLayerCommand(AsyncBaseCommand[None]):
    """Bulk-delete annotation layers by IDs."""

    def __init__(self, dao: Any, ids: list[int]) -> None:
        self._dao = dao
        self._ids = ids
        self._layers: list[Any] = []

    async def validate(self) -> None:
        if not self._ids:
            raise CommandInvalidError("No annotation layer IDs provided")
        self._layers = await self._dao.find_by_ids(self._ids)
        found_ids = {layer.id for layer in self._layers}
        missing = set(self._ids) - found_ids
        if missing:
            raise ObjectNotFoundError("AnnotationLayer", str(missing))
        has_children = await self._dao.has_annotations(self._ids)
        if has_children:
            raise CommandInvalidError(
                "Cannot delete annotation layers that contain annotations"
            )

    async def run(self) -> None:
        await self._dao.delete(self._layers)
        await self._dao.session.flush()
