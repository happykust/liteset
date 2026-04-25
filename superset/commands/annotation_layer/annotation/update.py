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
"""Async port of ``superset_old/commands/annotation_layer/annotation/update.py``."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from superset.commands.base import AsyncBaseCommand
from superset.exceptions import ObjectNotFoundError

if TYPE_CHECKING:
    from superset.models.annotations import Annotation


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
