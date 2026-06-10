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

Mirrors ``superset_old/row_level_security/schemas.py`` exactly:

* ``RLSPostSchema`` — required: ``name`` (1-255 chars), ``filter_type``
  (``Regular``/``Base``), ``tables`` (≥ 1 id), ``roles`` (id list,
  may be empty), ``clause`` (any string, including empty — original
  has no length validator); optional with ``allow_none=True``:
  ``description``, ``group_key``.
* ``RLSPutSchema`` — every field optional; same per-field validators
  apply when present.
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

    Mirrors ``superset_old.row_level_security.schemas.RLSPostSchema``:
    every field marked ``required=True`` there is required here, and
    the marshmallow ``Length`` / ``OneOf`` validators map onto
    ``msgspec.Meta`` constraints.

    Note: ``clause`` has *no* length validator (mirrors the original
    which only sets ``required=True, allow_none=False``). An empty
    string is a valid clause. ``description`` and ``group_key`` accept
    ``null`` (``allow_none=True`` in the original).

    Optional fields (``description``, ``group_key``) default to
    ``msgspec.UNSET`` so that absent keys are distinguishable from
    explicitly-null ones.  This mirrors Marshmallow 3 ``Schema.load()``
    which omits absent optional fields from the returned dict — the
    original Superset ``POST /api/v1/rowlevelsecurity/`` returns only the
    keys that were actually present in the request body under ``result``.
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

    Mirrors ``superset_old.row_level_security.schemas.RLSPutSchema`` —
    every field is optional (``required=False``), and the same per-field
    validators apply when the caller provides a value.

    ``msgspec.UNSET`` distinguishes "absent" from ``None`` so the
    controller can patch only the fields that were sent.

    Per the original schema:
    - ``name``, ``filter_type``, ``clause``, ``tables``, ``roles`` are
      ``allow_none=False`` (when present, must not be null).
    - ``description`` and ``group_key`` are ``allow_none=True`` (may be
      null to clear the field).
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
