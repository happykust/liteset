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
from __future__ import annotations

from typing import Any

import msgspec

# NOTE on mutable defaults in msgspec.Struct:
# Unlike regular Python classes, msgspec.Struct safely handles mutable
# default values (= {}, = []). Each instance receives its own copy.
# This is an intentional design decision by msgspec — using dict/list
# literals as defaults is the idiomatic pattern and does NOT suffer from
# the classic Python mutable-default gotcha. See:
# https://jcristharif.com/msgspec/structs.html#default-values


class ApiResponse(msgspec.Struct, omit_defaults=True):
    result: Any = None
    id: int | str | None = None
    message: str | None = None
    last_modified_time: float | None = None


class ApiListResponse(msgspec.Struct):
    result: list[Any] = []
    count: int = 0
    ids: list[int | str] = []
    label_columns: dict[str, str] = {}
    list_columns: list[str] = []
    order_columns: list[str] = []
    description_columns: dict[str, str] = {}


class InfoColumnMeta(msgspec.Struct, omit_defaults=True):
    """Column metadata returned by /_info."""

    column_name: str
    type: str = "unknown"
    nullable: bool = True


class InfoResponse(msgspec.Struct, omit_defaults=True):
    """GET /_info — API metadata for frontend."""

    permissions: list[str] = []
    add_columns: list[InfoColumnMeta] = []
    edit_columns: list[InfoColumnMeta] = []
    filters: dict[str, list[dict[str, str]]] = {}


class RelatedResultItem(msgspec.Struct):
    value: int | str
    text: str


class RelatedResponse(msgspec.Struct):
    """GET /related/{column_name} — dropdown values."""

    count: int = 0
    result: list[RelatedResultItem] = []


class DistinctResultItem(msgspec.Struct):
    text: str
    value: Any = None


class DistinctResponse(msgspec.Struct):
    """GET /distinct/{column_name} — filter dropdown values."""

    count: int = 0
    result: list[DistinctResultItem] = []


class FavoriteStatusItem(msgspec.Struct):
    id: int
    value: bool


class FavoriteStatusResponse(msgspec.Struct):
    """GET /favorite_status/ — batch favorite check."""

    result: list[FavoriteStatusItem] = []


class SupersetErrorDetail(msgspec.Struct):
    """Single error entry in SIP-40 format."""

    message: str = ""
    error_type: str = "UNKNOWN_ERROR"
    level: str = "error"
    extra: dict[str, Any] = {}


class ErrorResponse(msgspec.Struct):
    """SIP-40 compatible error response."""

    errors: list[SupersetErrorDetail] = []
    message: str = ""  # legacy compat field
