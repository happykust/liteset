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
"""Compatibility shim for ``superset.connectors.sqla.models``.

Provides lightweight stand-ins used by legacy Alembic migrations.
Does NOT import the real SA 2.0 models to avoid registry conflicts
with old-style ``declarative_base()`` inside migration files.
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.ext.declarative import declarative_base

# Standalone declarative base — isolated from the main app's Base
_Base = declarative_base()


class SqlaTable(_Base):
    """Minimal ORM model pointing at the ``tables`` table.

    Only the columns referenced by migrations are defined here.
    """

    __tablename__ = "tables"
    __table_args__ = {"extend_existing": True}

    id = sa.Column(sa.Integer, primary_key=True)
    table_name = sa.Column(sa.String(250))
    schema = sa.Column(sa.String(255))
    database_id = sa.Column(sa.Integer)
    fetch_values_predicate = sa.Column(sa.Text)
    perm = sa.Column(sa.String(1000))
    schema_perm = sa.Column(sa.String(1000))


# Constants used by dataset migrations
ADDITIVE_METRIC_TYPES: set[str] = {"count", "sum", "doublesum"}
ADDITIVE_METRIC_TYPES_LOWER: set[str] = {
    t.lower() for t in ADDITIVE_METRIC_TYPES
}

__all__ = [
    "SqlaTable",
    "ADDITIVE_METRIC_TYPES",
    "ADDITIVE_METRIC_TYPES_LOWER",
]
