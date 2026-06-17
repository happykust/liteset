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
"""Command for creating annotation layers."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from superset.commands.base import AsyncBaseCommand
from superset.exceptions import CommandInvalidError

if TYPE_CHECKING:
    from superset.models.annotations import AnnotationLayer


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
            from superset.commands.annotation_layer.exceptions import (
                AnnotationLayerInvalidError,
                AnnotationLayerNameUniquenessValidationError,
            )

            raise AnnotationLayerInvalidError(
                exceptions=[AnnotationLayerNameUniquenessValidationError()]
            )

    async def run(self) -> "AnnotationLayer":
        item = await self._dao.create(self._data)
        await self._dao.session.flush()
        return item
