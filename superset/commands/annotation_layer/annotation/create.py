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
"""Async port of ``superset_old/commands/annotation_layer/annotation/create.py``."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from superset.commands.base import AsyncBaseCommand
from superset.exceptions import CommandInvalidError, ObjectNotFoundError

if TYPE_CHECKING:
    from superset.models.annotations import Annotation


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
