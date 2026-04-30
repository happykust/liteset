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
"""Marshmallow type-name mapping helpers.

The original Apache Superset reports field types in ``_info`` payloads
using Marshmallow class names (``Integer``, ``String``, ``Boolean``,
``DateTime``, ``Date``, ``List``, ``Dict``, ``Nested``, ``Enum``,
``Raw``, ``Inferred``, ``Decimal``, ``Float``).

These do not always map 1:1 to either SQLAlchemy column types or
``msgspec`` Python types — Marshmallow uses ``Inferred`` for FK ids and
``Nested`` for sub-objects, which have no direct equivalent.  Therefore
the canonical type for each field is declared explicitly in
``superset.info_builder.specs``; this module only provides helpers for
the rare cases where auto-deduction is sufficient.
"""

from __future__ import annotations

import sqlalchemy as sa

# Marshmallow class names exposed in the ``_info`` payload.
MA_INTEGER = "Integer"
MA_STRING = "String"
MA_BOOLEAN = "Boolean"
MA_DATETIME = "DateTime"
MA_DATE = "Date"
MA_LIST = "List"
MA_DICT = "Dict"
MA_NESTED = "Nested"
MA_ENUM = "Enum"
MA_RAW = "Raw"
MA_INFERRED = "Inferred"
MA_DECIMAL = "Decimal"
MA_FLOAT = "Float"


def sa_to_marshmallow_type(col_type: sa.types.TypeEngine[object]) -> str:
    """Best-effort mapping of an SA column type to a Marshmallow class name.

    Used as a *fallback* when ``specs.py`` does not declare an explicit
    type for a field.  ``Inferred``/``Nested``/``Enum`` are never auto-
    deduced — those must be set explicitly in the descriptor.
    """
    if isinstance(col_type, sa.Boolean):
        return MA_BOOLEAN
    if isinstance(col_type, sa.DateTime):
        return MA_DATETIME
    if isinstance(col_type, sa.Date):
        return MA_DATE
    if isinstance(col_type, sa.Integer):
        return MA_INTEGER
    if isinstance(col_type, sa.Float):
        return MA_FLOAT
    if isinstance(col_type, sa.Numeric):
        return MA_DECIMAL
    if isinstance(col_type, sa.JSON):
        return MA_RAW
    # ``sa.Text`` extends ``sa.String`` — both report as ``String`` in MA.
    if isinstance(col_type, sa.String):
        return MA_STRING
    return MA_RAW
