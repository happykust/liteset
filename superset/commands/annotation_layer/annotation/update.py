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

import msgspec

from superset.commands.base import AsyncBaseCommand
from superset.exceptions import CommandInvalidError, ObjectNotFoundError

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

        # Short-descr uniqueness within the layer (excluding self) — 1:1 with
        # upstream update.py. The model has only a non-unique index, so the
        # check must be explicit.
        # Original: short_descr defaults to "" when absent from the payload
        # (superset_old/commands/annotation_layer/annotation/update.py:56:
        # ``self._properties.get('short_descr', '')``).
        # The upstream controller always sets item["layer"] = pk before
        # calling the command, so layer_id is always present and the
        # uniqueness check always fires — gated only on layer_id, NOT on
        # short_descr being truthy.
        short_descr: str = self._data.get("short_descr", "")
        layer_id = self._data.get("layer_id") or getattr(
            self._annotation, "layer_id", None
        )
        if layer_id is not None:
            if not await self._dao.validate_update_uniqueness(
                layer_id, short_descr, annotation_id=self._pk
            ):
                from superset.commands.annotation_layer.annotation.exceptions import (
                    AnnotationUniquenessValidationError,
                )

                raise AnnotationUniquenessValidationError()

        # Mirror upstream date-sanity check (superset_old/commands/annotation_
        # layer/annotation/update.py:80-83). Both default to None when the
        # incoming payload omits one side of the range, making the check a
        # no-op unless BOTH fields are present in the request — 1:1 with
        # original ``self._properties.get("start_dttm")`` / ``get("end_dttm")``.
        start_dttm = self._data.get("start_dttm")
        end_dttm = self._data.get("end_dttm")
        if start_dttm and end_dttm and end_dttm < start_dttm:
            raise CommandInvalidError("end_dttm must be greater or equal to start_dttm")

        # Validate json_metadata is valid JSON — 1:1 with upstream
        # ``AnnotationPutSchema.json_metadata`` which carries
        # ``validate=validate_json`` (superset_old/annotation_layers/
        # annotations/schemas.py:98-103). The port's msgspec struct has no
        # inline validator, so we check here instead.
        json_metadata = self._data.get("json_metadata")
        if json_metadata not in (None, "", msgspec.UNSET):
            import json as _json

            try:
                _json.loads(json_metadata)  # type: ignore[arg-type]
            except (TypeError, ValueError) as ex:
                raise CommandInvalidError("JSON not valid") from ex

    async def run(self) -> "Annotation":
        assert self._annotation is not None
        item = await self._dao.update(self._annotation, self._data)
        await self._dao.session.flush()
        return item
