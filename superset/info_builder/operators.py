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
"""SQLAlchemy column type → upstream filter operator mapping.

This module reproduces (without the upstream app-builder dependency) the
``SQLAFilterConverter.conversion_table`` resolution that the original
Superset uses to expose ``GET /<resource>/_info`` filter catalogs.

The mapping is **first-match-wins** by predicate; both the order of the
predicates and the order of the operator entries returned for a match
are load-bearing — they are byte-compared against the live ``_info``
contract snapshots.

Key edge cases:

* ``MediumText``/``LongText`` (``superset.utils.core``) are
  ``sa.Text().with_variant(...)`` instances and therefore satisfy
  ``isinstance(t, sa.Text)`` — handled by the generic ``Text`` branch
  *before* the ``String`` branch, since ``Text`` itself extends
  ``String`` in SQLAlchemy and we need the text-specific operator list.
* ``sqlalchemy_utils.UUIDType`` is a ``TypeDecorator`` whose impl is
  ``CHAR(32)`` or ``UUID``; treated as a string for filter purposes.
* ``LargeBinary`` is excluded from the ``In/Not In`` operator pair —
  matches the upstream ``is_binary`` short-circuit.
* Relationships are detected via the model's ``__mapper__.relationships``
  registry; their direction (``MANYTOONE``/``ONETOMANY``/etc.) selects
  between the singular (``rel_o_m``) and plural (``rel_m_m``) operator
  shapes.  ``MANYTOONE`` with ``uselist=False`` (one-to-one) collapses
  into the singular branch, matching the upstream
  ``is_relation_one_to_one`` predicate.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm.interfaces import (
    MANYTOMANY,
    MANYTOONE,
    ONETOMANY,
)

_OP_NAMES: dict[str, str] = {
    "sw": "Starts with",
    "nsw": "Not Starts with",
    "ew": "Ends with",
    "new": "Not Ends with",
    "ct": "Contains",
    "nct": "Not Contains",
    "eq": "Equal to",
    "neq": "Not Equal to",
    "gt": "Greater than",
    "lt": "Smaller than",
    "in": "In",
    "not_in": "Not In",
    "rel_o_m": "Relation",
    "nrel_o_m": "No Relation",
    "rel_m_m": "Relation as Many",
}


def _ops(*arg_names: str) -> list[dict[str, str]]:
    return [{"name": _OP_NAMES[arg], "operator": arg} for arg in arg_names]


_OPS_REL_SINGULAR: list[dict[str, str]] = _ops("rel_o_m", "nrel_o_m")
_OPS_REL_PLURAL: list[dict[str, str]] = _ops("rel_m_m")
_OPS_ENUM: list[dict[str, str]] = _ops("eq", "neq")
_OPS_TEXT: list[dict[str, str]] = _ops(
    "sw", "ew", "ct", "eq", "nsw", "new", "nct", "neq", "in", "not_in"
)
_OPS_BINARY: list[dict[str, str]] = _ops(
    "sw", "ew", "ct", "eq", "nsw", "new", "nct", "neq"
)
_OPS_STRING: list[dict[str, str]] = _ops(
    "sw", "ew", "ct", "eq", "nsw", "new", "nct", "neq", "in", "not_in"
)
_OPS_NUMERIC: list[dict[str, str]] = _ops("eq", "gt", "lt", "neq", "in", "not_in")
_OPS_DATE: list[dict[str, str]] = _ops("eq", "gt", "lt", "neq")
_OPS_BOOLEAN: list[dict[str, str]] = _ops("eq", "neq")
_OPS_DATETIME: list[dict[str, str]] = _ops("eq", "gt", "lt", "neq")
_OPS_JSON: list[dict[str, str]] = _ops(
    "sw", "ew", "ct", "eq", "nsw", "new", "nct", "neq"
)


# UUIDType is optional — sqlalchemy_utils may not be installed in thin test envs.
def _uuid_type() -> type | None:
    try:
        from sqlalchemy_utils import UUIDType
    except ImportError:  # pragma: no cover - sqlalchemy_utils is a hard dep
        return None
    return UUIDType


def operators_for_column(model_cls: type, column_name: str) -> list[dict[str, str]]:
    """Return the upstream-style filter operator list for ``column_name``.

    Resolves ``column_name`` against ``model_cls.__mapper__``:

    * If the name maps to a relationship, the operator list is derived
      from the relationship's ``direction`` — ``MANYTOONE`` (and the
      ``uselist=False`` "one-to-one" variant) returns the singular
      (``rel_o_m``/``nrel_o_m``) operators; ``MANYTOMANY`` and
      ``ONETOMANY`` return the plural (``rel_m_m``) operators.
    * Otherwise the name is looked up in ``mapper.columns`` and the
      column's ``type`` is matched against the predicate ladder
      mirrored from the upstream
      ``SQLAFilterConverter.conversion_table`` (the upstream
      app-builder sqla filters module).

    Raises:
        KeyError: when ``column_name`` is neither a known relationship
            nor a mapped column on ``model_cls``.

    Notes:
        Any unmappable column type (e.g. a custom ``TypeDecorator`` that
        doesn't subclass any of the recognised SA primitives) falls
        back to an empty list — matching the upstream behaviour, which
        simply skips the column when no predicate matches.
    """
    mapper: Any = sa.inspect(model_cls)

    if column_name in mapper.relationships:
        rel = mapper.relationships[column_name]
        direction = rel.direction
        if direction is MANYTOONE:
            return list(_OPS_REL_SINGULAR)
        if direction is MANYTOMANY:
            return list(_OPS_REL_PLURAL)
        if direction is ONETOMANY:
            return list(_OPS_REL_PLURAL)
        return []

    column = mapper.columns[column_name]
    return _operators_for_type(column.type)


def _operators_for_type(col_type: Any) -> list[dict[str, str]]:  # noqa: C901  # complex business logic
    """First-match-wins resolution of a column ``type`` to its op list.

    The check order follows
    ``SQLAFilterConverter.conversion_table`` — in particular ``Text`` is
    checked **before** ``String`` so the
    Superset ``MediumText``/``LongText`` variants (which are
    ``sa.Text().with_variant(...)``) get the text-specific ladder rather
    than the generic-string one.  In practice the operator lists are
    identical, but this preserves the canonical lookup that the snapshot
    fixtures encode.
    """
    if isinstance(col_type, sa.Enum):
        return list(_OPS_ENUM)

    # Text checked before String because sa.Text subclasses sa.String;
    # MediumText/LongText are Text variants that must not fall through to _OPS_STRING.
    if isinstance(col_type, sa.Text):
        return list(_OPS_TEXT)

    if isinstance(col_type, sa.LargeBinary):
        return list(_OPS_BINARY)

    uuid_type = _uuid_type()
    if isinstance(col_type, sa.String) or (
        uuid_type is not None and isinstance(col_type, uuid_type)
    ):
        return list(_OPS_STRING)

    # Boolean checked before Integer: sa.Boolean presents as integer-shaped on some
    # backends, and we need the boolean-specific eq/neq pair.
    if isinstance(col_type, sa.Boolean):
        return list(_OPS_BOOLEAN)

    if isinstance(col_type, sa.Integer):
        return list(_OPS_NUMERIC)

    if isinstance(col_type, sa.Float):
        return list(_OPS_NUMERIC)
    if isinstance(col_type, sa.Numeric):
        return list(_OPS_NUMERIC)

    # DateTime checked before Date: sa.DateTime does not subclass sa.Date.
    if isinstance(col_type, sa.DateTime):
        return list(_OPS_DATETIME)
    if isinstance(col_type, sa.Date):
        return list(_OPS_DATE)

    if isinstance(col_type, sa.JSON):
        return list(_OPS_JSON)

    return []
