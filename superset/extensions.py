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
"""Compatibility shim for ``superset.extensions``.

Legacy migrations import:
  - ``encrypted_field_factory``

Migrations use ``encrypted_field_factory.create(sa.String(...))`` to define
column types in local ORM models declared inside migration scripts. The shim
returns the underlying SA type as-is (no encryption) which is safe because
these local models are only used for data migration queries -- the actual table
schema is managed by Alembic ``op.`` calls, not by these ORM definitions.
"""
from __future__ import annotations

from typing import Any

import sqlalchemy as sa


class _EncryptedFieldFactory:
    """Minimal stub that mimics ``EncryptedFieldFactory.create()``.

    Returns the wrapped SA type unchanged so migrations can define column
    types without requiring the encryption adapter to be initialised.
    """

    def create(self, sa_type: sa.types.TypeEngine | type, *args: Any, **kwargs: Any) -> sa.types.TypeEngine:  # noqa: E501
        if isinstance(sa_type, type):
            return sa_type()
        return sa_type


encrypted_field_factory = _EncryptedFieldFactory()

__all__ = ["encrypted_field_factory"]
