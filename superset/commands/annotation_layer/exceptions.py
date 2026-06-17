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
"""Annotation-layer-specific exceptions."""

from __future__ import annotations

from superset.exceptions import (
    CommandInvalidError,
    ObjectNotFoundError,
)


class AnnotationLayerNameUniquenessValidationError(CommandInvalidError):
    """Layer-name uniqueness violation — field-keyed leaf error."""

    status_code = 422
    message = "Name must be unique"

    def normalized_messages(self) -> dict[str, list[str]]:
        return {"name": [str(self.message)]}


class AnnotationLayerInvalidError(CommandInvalidError):
    """Accumulating annotation-layer validation error.

    The registered handler emits ``{"message": normalized_messages()}``
    (per-field 422).
    """

    status_code = 422
    message = "Annotation layer parameters are invalid."


__all__ = (
    "AnnotationLayerInvalidError",
    "AnnotationLayerNameUniquenessValidationError",
    "CommandInvalidError",
    "ObjectNotFoundError",
)
