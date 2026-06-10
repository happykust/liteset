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

        # Short-descr uniqueness within the layer — 1:1 with upstream
        # create.py:63-65. The model has only a NON-unique index (matching
        # upstream), so there is no IntegrityError to lean on; the check must be
        # explicit or duplicates are silently accepted.
        from superset.commands.annotation_layer.annotation.exceptions import (
            AnnotationUniquenessValidationError,
        )

        if not await self._dao.validate_update_uniqueness(self._layer_pk, short_descr):
            raise AnnotationUniquenessValidationError()

        # Mirror upstream validations (superset_old/commands/annotation_layer/
        # annotation/create.py:67-72) — `end_dttm < start_dttm` → 422
        # AnnotationDatesValidationError.
        start_dttm = self._data.get("start_dttm")
        end_dttm = self._data.get("end_dttm")
        if start_dttm and end_dttm and end_dttm < start_dttm:
            raise CommandInvalidError("end_dttm must be greater or equal to start_dttm")

        # Validate json_metadata is valid JSON — 1:1 with upstream
        # ``AnnotationPostSchema.json_metadata`` which carries
        # ``validate=validate_json`` (superset_old/annotation_layers/
        # annotations/schemas.py:76-80). The port's msgspec struct has no
        # inline validator, so we check here instead.
        json_metadata = self._data.get("json_metadata")
        if json_metadata not in (None, ""):
            import json as _json

            try:
                _json.loads(json_metadata)  # type: ignore[arg-type]
            except (TypeError, ValueError) as ex:
                raise CommandInvalidError("JSON not valid") from ex

    async def run(self) -> "Annotation":
        self._data["layer_id"] = self._layer_pk
        item = await self._dao.create(self._data)
        await self._dao.session.flush()
        return item
