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
"""msgspec Structs for the Annotation Layer and Annotation APIs."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

import msgspec
from msgspec import Meta

# ---------------------------------------------------------------------------
# Annotation Layer — request bodies
# ---------------------------------------------------------------------------


class AnnotationLayerPostSchema(msgspec.Struct):
    """POST /api/v1/annotation_layer/

    Upstream's AnnotationLayerPostSchema enforces ``Length(1, 250)`` on
    both ``name`` and ``descr``
    (superset_old/annotation_layers/schemas.py:47-58).
    """

    name: Annotated[str, Meta(min_length=1, max_length=250)]
    descr: Annotated[str, Meta(max_length=250)] = ""


class AnnotationLayerPutSchema(msgspec.Struct):
    """PUT /api/v1/annotation_layer/<pk>"""

    name: (
        Annotated[str, Meta(min_length=1, max_length=250)] | None | msgspec.UnsetType
    ) = msgspec.UNSET
    descr: (
        Annotated[str, Meta(max_length=250)] | None | msgspec.UnsetType
    ) = msgspec.UNSET


# ---------------------------------------------------------------------------
# Annotation Layer — response
# ---------------------------------------------------------------------------


class AnnotationLayerResponseSchema(msgspec.Struct):
    """Single annotation layer in responses."""

    id: int
    name: str
    descr: str
    created_on: str | None = None
    changed_on: str | None = None


# ---------------------------------------------------------------------------
# Annotation — request bodies
# ---------------------------------------------------------------------------


class AnnotationPostSchema(msgspec.Struct):
    """POST /api/v1/annotation_layer/<layer_pk>/annotation/"""

    short_descr: Annotated[str, Meta(min_length=1)]
    start_dttm: datetime
    end_dttm: datetime
    long_descr: str = ""
    json_metadata: str = ""


class AnnotationPutSchema(msgspec.Struct):
    """PUT /api/v1/annotation_layer/<layer_pk>/annotation/<pk>"""

    short_descr: str | None | msgspec.UnsetType = msgspec.UNSET
    long_descr: str | None | msgspec.UnsetType = msgspec.UNSET
    start_dttm: datetime | None | msgspec.UnsetType = msgspec.UNSET
    end_dttm: datetime | None | msgspec.UnsetType = msgspec.UNSET
    json_metadata: str | None | msgspec.UnsetType = msgspec.UNSET


# ---------------------------------------------------------------------------
# Annotation — response
# ---------------------------------------------------------------------------


class AnnotationResponseSchema(msgspec.Struct):
    """Single annotation in responses."""

    id: int
    short_descr: str
    long_descr: str
    start_dttm: str | None = None
    end_dttm: str | None = None
    layer_id: int | None = None
    json_metadata: str | None = None
    created_on: str | None = None
    changed_on: str | None = None
