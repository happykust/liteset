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

    Upstream's AnnotationLayerPostSchema enforces ``Length(1, 250)`` only
    on ``name``; ``descr`` has no length constraint and allows None/null
    (superset_old/annotation_layers/schemas.py:44-51). With no ``missing``
    argument an absent ``descr`` is omitted from the loaded dict, so the
    column keeps its SQL default (NULL) — hence UNSET, not ``""``.
    """

    name: Annotated[str, Meta(min_length=1, max_length=250)]
    descr: str | None | msgspec.UnsetType = msgspec.UNSET


class AnnotationLayerPutSchema(msgspec.Struct):
    """PUT /api/v1/annotation_layer/<pk>

    Upstream's AnnotationLayerPutSchema has no ``allow_none=True`` on
    ``descr`` (superset_old/annotation_layers/schemas.py:60-62), so passing
    ``descr: null`` in a PUT body must be rejected with 422.  Only UNSET
    (field absent) is permitted.
    """

    name: Annotated[str, Meta(min_length=1, max_length=250)] | msgspec.UnsetType = (
        msgspec.UNSET
    )
    descr: str | msgspec.UnsetType = msgspec.UNSET


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
    """POST /api/v1/annotation_layer/<layer_pk>/annotation/

    Optional fields default to ``UNSET`` (not ``None``) so the 201 response
    can echo exactly the submitted keys — 1:1 with the original
    ``result=item`` where ``item`` is the Marshmallow-loaded request dict
    (superset_old/annotation_layers/annotations/api.py:289-297).
    """

    short_descr: Annotated[str, Meta(min_length=1, max_length=500)]
    start_dttm: datetime
    end_dttm: datetime
    long_descr: str | None | msgspec.UnsetType = msgspec.UNSET
    json_metadata: str | None | msgspec.UnsetType = msgspec.UNSET


class AnnotationPutSchema(msgspec.Struct):
    """PUT /api/v1/annotation_layer/<layer_pk>/annotation/<pk>"""

    short_descr: (
        Annotated[str, Meta(min_length=1, max_length=500)] | msgspec.UnsetType
    ) = msgspec.UNSET
    long_descr: str | None | msgspec.UnsetType = msgspec.UNSET
    # No ``allow_none=True`` in the original PUT schema
    # (superset_old/annotation_layers/annotations/schemas.py:92-97) —
    # explicit null → 422, never NULL in the DateTime columns.
    start_dttm: datetime | msgspec.UnsetType = msgspec.UNSET
    end_dttm: datetime | msgspec.UnsetType = msgspec.UNSET
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
