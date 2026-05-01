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
"""Key-value entry model for the key-value store.

Pure SQLAlchemy -- no Flask dependencies.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy import Column, DateTime, ForeignKey, Integer, LargeBinary, String
from sqlalchemy.types import TypeDecorator

from superset.models.helpers import AuditMixinNullable, Base, ImportExportMixin


class BytesUUID(TypeDecorator[UUID]):
    """Store a Python UUID in a ``bytea`` column as 16 raw bytes.

    The ``key_value.uuid`` column was created by migration
    6766938c6065 using ``sqlalchemy_utils.UUIDType(binary=True)``
    which produces ``BINARY(16)`` / ``bytea``.  In SQLAlchemy 2.x
    ``UUIDType(binary=True)`` on PostgreSQL maps to the native
    ``uuid`` type via ``load_dialect_impl``, which conflicts with
    the existing ``bytea`` column and fails with asyncpg:

        column "uuid" is of type bytea but expression is of type uuid

    This TypeDecorator avoids the dialect-specific override: it
    always uses ``LargeBinary(16)`` and performs UUID↔bytes
    conversion in Python.  Compatible with both psycopg2 and
    asyncpg because we only ever bind ``bytes`` parameters.
    """

    impl = LargeBinary(16)
    cache_ok = True

    def process_bind_param(
        self,
        value: UUID | bytes | str | None,
        dialect: object,
    ) -> bytes | None:
        if value is None:
            return None
        if isinstance(value, UUID):
            return value.bytes
        if isinstance(value, bytes):
            return value
        if isinstance(value, str):
            return UUID(value).bytes
        raise TypeError(f"Cannot convert {type(value).__name__} to UUID bytes")

    def process_result_value(
        self,
        value: bytes | None,
        dialect: object,
    ) -> UUID | None:
        if value is None:
            return None
        return UUID(bytes=bytes(value))


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class KeyValueEntry(AuditMixinNullable, ImportExportMixin, Base):
    """Structured key-value entry for the key-value store
    subsystem (table ``key_value``).

    This is the newer, fully-featured key-value model that supports binary
    (LargeBinary) values, resource-namespaced keys, TTL-based expiration
    via ``expires_on``, and full audit tracking (created/changed by/on).

    Not to be confused with :class:`superset.models.core.KeyValue` which maps
    to the legacy ``keyvalue`` table and only stores plain text without audit
    columns or resource namespacing. Both tables coexist because existing
    data in ``keyvalue`` must remain accessible during the migration period.

    Note: ``created_on``, ``created_by_fk``, ``changed_on``, and
    ``changed_by_fk`` are declared explicitly here (overriding the mixin)
    because the key-value store requires precise control over these columns.
    """

    __tablename__ = "key_value"

    id = Column(Integer, primary_key=True)
    resource = Column(String(32), nullable=False)
    value = Column(LargeBinary(length=2**24 - 1), nullable=False)
    # UUID column created by migration 6766938c6065; the original
    # Flask model at superset_old/key_value/models.py forgets to
    # declare it but code assigns ``entry.uuid = key`` directly.
    # Declare it explicitly so our async DAO can query it.
    uuid = Column(BytesUUID(), default=uuid4)

    # Explicit audit columns (override AuditMixinNullable defaults)
    created_on = Column(DateTime, nullable=True)
    created_by_fk = Column(Integer, ForeignKey("ab_user.id"), nullable=True)
    changed_on = Column(DateTime, nullable=True)
    changed_by_fk = Column(Integer, ForeignKey("ab_user.id"), nullable=True)
    expires_on = Column(DateTime, nullable=True)
