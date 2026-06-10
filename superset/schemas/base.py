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

from typing import Any, Self

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


# ---------------------------------------------------------------------------
# Shared reference Structs — reusable across chart / dashboard / dataset etc.
# ---------------------------------------------------------------------------


class ModelStruct(msgspec.Struct):
    """Base class for API response Structs with automatic ORM→Struct mapping.

    Note: ``omit_defaults`` is intentionally **disabled** so that fields
    with ``None`` / ``False`` / ``[]`` values are always serialised.
    The original Superset API (Flask/Marshmallow) always includes every
    column in the response, even when the value is null.  The frontend
    relies on this: e.g. ``position_json``, ``json_metadata``, ``owners``
    must be present even when empty.

    Subclasses declare typed fields matching ORM model attribute names.
    :meth:`from_model` inspects ``msgspec.structs.fields(cls)`` and
    resolves each field from the ORM object via ``getattr``.

    Nested ``ModelStruct`` fields are auto-resolved:
    - If the ORM attribute is a list → maps each element with the
      nested struct's ``from_model``
    - If the ORM attribute is a single object → calls nested ``from_model``

    Override specific fields by declaring ``_resolve_<field_name>``
    classmethods that accept ``(cls, obj)`` and return the value.
    """

    @classmethod
    def from_model(cls, obj: Any, **overrides: Any) -> Self:  # noqa: C901
        kwargs: dict[str, Any] = {}
        for field in msgspec.structs.fields(cls):
            name = field.name

            # Explicit override takes priority
            if name in overrides:
                kwargs[name] = overrides[name]
                continue

            # Custom resolver: _resolve_<field_name>(cls, obj)
            resolver = getattr(cls, f"_resolve_{name}", None)
            if resolver is not None:
                kwargs[name] = resolver(obj)
                continue

            try:
                raw = getattr(obj, name, None)
            except Exception:
                # Attribute access may trigger lazy load on async sessions
                # (MissingGreenlet). Fall back to field default.
                raw = field.default if field.default is not msgspec.NODEFAULT else None
                kwargs[name] = raw
                continue

            # Nested ModelStruct (scalar relationship)
            nested_type = _extract_model_struct_type(field)
            if nested_type is not None:
                if isinstance(raw, list):
                    kwargs[name] = [nested_type.from_model(item) for item in raw]
                elif raw is not None:
                    kwargs[name] = nested_type.from_model(raw)
                else:
                    kwargs[name] = (
                        field.default
                        if field.default is not msgspec.NODEFAULT
                        else None
                    )
                continue

            # List of nested ModelStruct
            list_nested = _extract_list_model_struct_type(field)
            if list_nested is not None:
                items = raw or []
                kwargs[name] = [list_nested.from_model(item) for item in items]
                continue

            # uuid → str
            if raw is not None and hasattr(raw, "hex") and hasattr(raw, "int"):
                raw = str(raw)

            # datetime → isoformat str
            if (
                raw is not None
                and hasattr(raw, "isoformat")
                and isinstance(
                    field.default, (str, type(None), type(msgspec.NODEFAULT))
                )
            ):
                if (
                    isinstance(field.default, str)
                    or field.default is None
                    or field.default is msgspec.NODEFAULT
                ):
                    if not isinstance(raw, str):
                        raw = raw.isoformat()

            kwargs[name] = raw

        return cls(**kwargs)


def _extract_model_struct_type(
    field: msgspec.structs.FieldInfo,
) -> type[ModelStruct] | None:
    """If field type is ``SomeModelStruct | None``, return the struct class."""
    import types

    ft = field.type

    # Union (X | None) — Python 3.10+ ``X | Y`` is types.UnionType
    if isinstance(ft, types.UnionType):
        for arg in ft.__args__:
            if isinstance(arg, type) and issubclass(arg, ModelStruct):
                return arg
    # Direct ModelStruct subclass
    if isinstance(ft, type) and issubclass(ft, ModelStruct):
        return ft
    return None


def _extract_list_model_struct_type(
    field: msgspec.structs.FieldInfo,
) -> type[ModelStruct] | None:
    """If field type is ``list[SomeModelStruct]``, return the item struct class."""
    ft = field.type
    origin = getattr(ft, "__origin__", None)
    if origin is list:
        args = getattr(ft, "__args__", ())
        if args and isinstance(args[0], type) and issubclass(args[0], ModelStruct):
            return args[0]
    return None


class UserRef(ModelStruct):
    """Lightweight user reference embedded in API responses."""

    id: int
    first_name: str = ""
    last_name: str = ""


class TagRef(ModelStruct):
    """Lightweight tag reference embedded in API responses."""

    id: int
    name: str = ""
    type: str | None = None


class DashboardRef(ModelStruct):
    """Lightweight dashboard reference embedded in API responses."""

    id: int
    dashboard_title: str = ""
    json_metadata: str | None = None


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
