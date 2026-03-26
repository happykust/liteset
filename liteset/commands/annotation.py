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
"""Annotation layer and annotation command classes — business logic for CRUD."""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from liteset.commands.base import AsyncBaseCommand
from liteset.exceptions import CommandInvalidError, ObjectNotFoundError

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from liteset.models.annotations import Annotation, AnnotationLayer


# ---------------------------------------------------------------------------
# Annotation Layer commands
# ---------------------------------------------------------------------------


class CreateAnnotationLayerCommand(AsyncBaseCommand["AnnotationLayer"]):
    """Create a new annotation layer."""

    def __init__(self, dao: Any, data: dict[str, Any]) -> None:
        self._dao = dao
        self._data = data

    async def validate(self) -> None:
        name = self._data.get("name")
        if not name or not name.strip():
            raise CommandInvalidError("name is required")
        is_unique = await self._dao.validate_update_uniqueness(name)
        if not is_unique:
            raise CommandInvalidError(
                f"Annotation layer with name '{name}' already exists"
            )

    async def run(self) -> "AnnotationLayer":
        item = await self._dao.create(self._data)
        await self._dao.session.flush()
        return item


class UpdateAnnotationLayerCommand(AsyncBaseCommand["AnnotationLayer"]):
    """Update an existing annotation layer."""

    def __init__(self, dao: Any, pk: int, data: dict[str, Any]) -> None:
        self._dao = dao
        self._pk = pk
        self._data = data
        self._layer: Any | None = None

    async def validate(self) -> None:
        self._layer = await self._dao.find_by_id(self._pk)
        if not self._layer:
            raise ObjectNotFoundError("AnnotationLayer", self._pk)
        name = self._data.get("name")
        if name is not None:
            is_unique = await self._dao.validate_update_uniqueness(
                name, layer_id=self._pk
            )
            if not is_unique:
                raise CommandInvalidError(
                    f"Annotation layer with name '{name}' already exists"
                )

    async def run(self) -> "AnnotationLayer":
        assert self._layer is not None
        item = await self._dao.update(self._layer, self._data)
        await self._dao.session.flush()
        return item


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


# ---------------------------------------------------------------------------
# Annotation commands
# ---------------------------------------------------------------------------


class CreateAnnotationCommand(AsyncBaseCommand["Annotation"]):
    """Create a new annotation within a layer."""

    def __init__(
        self,
        dao: Any,
        layer_dao: Any,
        layer_pk: int,
        data: dict[str, Any],
    ) -> None:
        self._dao = dao
        self._layer_dao = layer_dao
        self._layer_pk = layer_pk
        self._data = data

    async def validate(self) -> None:
        layer = await self._layer_dao.find_by_id(self._layer_pk)
        if not layer:
            raise ObjectNotFoundError("AnnotationLayer", self._layer_pk)
        short_descr = self._data.get("short_descr")
        if not short_descr or not short_descr.strip():
            raise CommandInvalidError("short_descr is required")

    async def run(self) -> "Annotation":
        self._data["layer_id"] = self._layer_pk
        item = await self._dao.create(self._data)
        await self._dao.session.flush()
        return item


class UpdateAnnotationCommand(AsyncBaseCommand["Annotation"]):
    """Update an existing annotation."""

    def __init__(self, dao: Any, pk: int, data: dict[str, Any]) -> None:
        self._dao = dao
        self._pk = pk
        self._data = data
        self._annotation: Any | None = None

    async def validate(self) -> None:
        self._annotation = await self._dao.find_by_id(self._pk)
        if not self._annotation:
            raise ObjectNotFoundError("Annotation", self._pk)

    async def run(self) -> "Annotation":
        assert self._annotation is not None
        item = await self._dao.update(self._annotation, self._data)
        await self._dao.session.flush()
        return item


class DeleteAnnotationCommand(AsyncBaseCommand[None]):
    """Delete a single annotation."""

    def __init__(self, dao: Any, pk: int) -> None:
        self._dao = dao
        self._pk = pk
        self._annotation: Any | None = None

    async def validate(self) -> None:
        self._annotation = await self._dao.find_by_id(self._pk)
        if not self._annotation:
            raise ObjectNotFoundError("Annotation", self._pk)

    async def run(self) -> None:
        assert self._annotation is not None
        await self._dao.delete([self._annotation])
        await self._dao.session.flush()


class BulkDeleteAnnotationCommand(AsyncBaseCommand[None]):
    """Bulk-delete annotations by IDs."""

    def __init__(self, dao: Any, ids: list[int]) -> None:
        self._dao = dao
        self._ids = ids
        self._annotations: list[Any] = []

    async def validate(self) -> None:
        if not self._ids:
            raise CommandInvalidError("No annotation IDs provided")
        self._annotations = await self._dao.find_by_ids(self._ids)
        found_ids = {a.id for a in self._annotations}
        missing = set(self._ids) - found_ids
        if missing:
            raise ObjectNotFoundError("Annotation", str(missing))

    async def run(self) -> None:
        await self._dao.delete(self._annotations)
        await self._dao.session.flush()
