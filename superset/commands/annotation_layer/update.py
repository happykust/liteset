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
"""Command for updating an existing annotation layer."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from superset.commands.base import AsyncBaseCommand
from superset.exceptions import ObjectNotFoundError

if TYPE_CHECKING:
    from superset.models.annotations import AnnotationLayer


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
                from superset.commands.annotation_layer.exceptions import (
                    AnnotationLayerInvalidError,
                    AnnotationLayerNameUniquenessValidationError,
                )

                raise AnnotationLayerInvalidError(
                    exceptions=[AnnotationLayerNameUniquenessValidationError()]
                )

    async def run(self) -> "AnnotationLayer":
        assert self._layer is not None
        item = await self._dao.update(self._layer, self._data)
        await self._dao.session.flush()
        return item
