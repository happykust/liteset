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
"""msgspec Structs for the Row Level Security API.

* ``RLSPostSchema`` — required: ``name`` (1-255 chars), ``filter_type``
  (``Regular``/``Base``), ``tables`` (≥ 1 id), ``roles`` (id list,
  may be empty), ``clause`` (any string, no length limit); optional
  with ``allow_none=True``: ``description``, ``group_key``.
* ``RLSPutSchema`` — every field optional; same per-field validators apply.
"""

from __future__ import annotations

from typing import Annotated, Literal

import msgspec
from msgspec import Meta

# Literal aliases — msgspec validates a string against the literal set
# automatically, mirroring marshmallow's ``OneOf([...])``.
FilterTypeLiteral = Literal["Regular", "Base"]


class RLSPostSchema(msgspec.Struct):
    """Body for POST /api/v1/rowlevelsecurity/.

    ``clause`` has no length validator; an empty string is valid.
    ``description`` and ``group_key`` accept ``null``.

    Optional fields default to ``msgspec.UNSET`` so absent keys are
    distinguishable from explicitly-null ones.
    """

    name: Annotated[str, Meta(min_length=1, max_length=255)]
    filter_type: FilterTypeLiteral
    clause: str
    tables: Annotated[list[int], Meta(min_length=1)]
    roles: list[int]
    description: str | None | msgspec.UnsetType = msgspec.UNSET
    group_key: str | None | msgspec.UnsetType = msgspec.UNSET


class RLSPutSchema(msgspec.Struct):
    """Body for PUT /api/v1/rowlevelsecurity/{pk}.

    Every field is optional. ``msgspec.UNSET`` distinguishes "absent" from
    ``None`` so the controller can patch only the fields that were sent.

    ``name``, ``filter_type``, ``clause``, ``tables``, ``roles`` are
    non-nullable when present. ``description`` and ``group_key`` may be
    null to clear the field.
    """

    name: Annotated[str, Meta(min_length=1, max_length=255)] | msgspec.UnsetType = (
        msgspec.UNSET
    )
    filter_type: FilterTypeLiteral | msgspec.UnsetType = msgspec.UNSET
    clause: str | msgspec.UnsetType = msgspec.UNSET
    tables: list[int] | msgspec.UnsetType = msgspec.UNSET
    roles: list[int] | msgspec.UnsetType = msgspec.UNSET
    description: str | None | msgspec.UnsetType = msgspec.UNSET
    group_key: str | None | msgspec.UnsetType = msgspec.UNSET
