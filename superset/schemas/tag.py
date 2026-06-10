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
"""msgspec Structs for the Tag API."""

from __future__ import annotations

from typing import Annotated

import msgspec
from msgspec import Meta


class TagPostSchema(msgspec.Struct):
    # Upstream's TagObjectSchema enforces ``Length(min=1)`` on ``name``
    # (superset_old/tags/schemas.py:75). The ``tag.name`` DB column is
    # ``VARCHAR(250)``; cap at the DB limit so a long string is rejected
    # cleanly instead of failing in the asyncpg StringDataRightTruncation
    # path (now caught by the global DBAPI handler as 400, but it's nicer
    # to reject up-front with the field name).
    name: Annotated[str, Meta(min_length=1, max_length=250)]
    description: str | None = None
    objects_to_tag: list[list[str | int]] = []


class TagPutSchema(msgspec.Struct):
    # PUT keeps the field optional via msgspec.UNSET, but if supplied it
    # still has to satisfy the same length constraint as POST.
    name: (
        Annotated[str, Meta(min_length=1, max_length=250)] | None | msgspec.UnsetType
    ) = msgspec.UNSET
    description: str | None | msgspec.UnsetType = msgspec.UNSET
    objects_to_tag: list[list[str | int]] | msgspec.UnsetType = msgspec.UNSET


class BulkTagCreateSchema(msgspec.Struct):
    tags: list[TagPostSchema]


class AddTagsToObjectProperties(msgspec.Struct, frozen=True):
    """Inner ``properties`` object for the add-tags-to-object request."""

    tags: list[str] | None = None


class AddTagsToObjectSchema(msgspec.Struct):
    """Schema for POST /{object_type}/{object_id}/ -- add tags to object.

    The original Flask endpoint expects ``{"properties": {"tags": [...]}}``.
    """

    properties: AddTagsToObjectProperties = msgspec.field(
        default_factory=AddTagsToObjectProperties,
    )
