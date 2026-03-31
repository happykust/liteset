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
"""Model base, mixin classes, and helper types for Superset.

Pure SQLAlchemy — no Flask dependencies.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Text, types as sa_types
from sqlalchemy.dialects.mysql import LONGTEXT, MEDIUMTEXT
from sqlalchemy.ext.declarative import declared_attr
from sqlalchemy.orm import DeclarativeBase, relationship, RelationshipProperty
from sqlalchemy.sql.sqltypes import Variant


class Base(DeclarativeBase):
    """Declarative base for all Superset models."""

    __allow_unmapped__ = True


metadata = Base.metadata


# ---------------------------------------------------------------------------
# Column type helpers
# ---------------------------------------------------------------------------


def MediumText() -> Variant:  # noqa: N802
    return Text().with_variant(MEDIUMTEXT(), "mysql")


def LongText() -> Variant:  # noqa: N802
    return Text().with_variant(LONGTEXT(), "mysql")


class BinaryUUID(sa_types.TypeDecorator[uuid.UUID]):
    """UUID stored as 16 bytes (bytea on PostgreSQL, BINARY(16) on MySQL).

    Drop-in replacement for ``sqlalchemy_utils.UUIDType(binary=True)``
    that works correctly with both asyncpg (binary protocol) and
    psycopg2 (text protocol).
    """

    impl = sa_types.LargeBinary(length=16)
    cache_ok = True

    def process_bind_param(
        self, value: uuid.UUID | str | bytes | None, dialect: Any
    ) -> bytes | None:
        if value is None:
            return None
        if isinstance(value, str):
            value = uuid.UUID(value)
        if isinstance(value, uuid.UUID):
            return value.bytes
        return value  # already bytes

    def process_result_value(
        self, value: bytes | memoryview | None, dialect: Any
    ) -> uuid.UUID | None:
        if value is None:
            return None
        if isinstance(value, memoryview):
            value = bytes(value)
        if isinstance(value, bytes):
            return uuid.UUID(bytes=value)
        if isinstance(value, uuid.UUID):
            return value
        return uuid.UUID(str(value))


# ---------------------------------------------------------------------------
# Mixins
# ---------------------------------------------------------------------------


class UUIDMixin:
    """Adds a ``uuid`` column (unique, non-PK by default)."""

    uuid = sa.Column(
        BinaryUUID(),
        primary_key=False,
        unique=True,
        default=uuid.uuid4,
    )


class AuditMixinNullable:
    """Audit columns: created/changed timestamps and user FKs.

    Replaces Flask-AppBuilder's AuditMixin with pure SQLAlchemy columns.
    All fields are nullable so legacy rows without audit data still load.

    Computed properties match the original FAB AuditMixin so that
    ``serialize_list_response`` can read them via ``getattr``.
    """

    created_on = sa.Column(sa.DateTime, default=datetime.now, nullable=True)
    changed_on = sa.Column(
        sa.DateTime,
        default=datetime.now,
        onupdate=datetime.now,
        nullable=True,
    )

    @declared_attr
    def created_by_fk(cls):  # noqa: N805
        return sa.Column(
            sa.Integer,
            sa.ForeignKey("ab_user.id"),
            nullable=True,
        )

    @declared_attr
    def changed_by_fk(cls):  # noqa: N805
        return sa.Column(
            sa.Integer,
            sa.ForeignKey("ab_user.id"),
            nullable=True,
        )

    @declared_attr
    def created_by(cls):  # noqa: N805
        return relationship(
            "User",
            primaryjoin=f"{cls.__name__}.created_by_fk == User.id",
            foreign_keys=f"[{cls.__name__}.created_by_fk]",
            viewonly=True,
        )

    @declared_attr
    def changed_by(cls):  # noqa: N805
        return relationship(
            "User",
            primaryjoin=f"{cls.__name__}.changed_by_fk == User.id",
            foreign_keys=f"[{cls.__name__}.changed_by_fk]",
            viewonly=True,
        )

    # -- Computed properties (match original FAB @renders decorators) ------

    @property
    def changed_on_utc(self) -> str | None:
        if self.changed_on is None:
            return None
        import pytz

        dt = self.changed_on
        if dt.tzinfo is None:
            dt = pytz.utc.localize(dt)
        else:
            dt = dt.astimezone(pytz.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%f%z")

    @property
    def changed_on_delta_humanized(self) -> str | None:
        if self.changed_on is None:
            return None
        import humanize

        return humanize.naturaltime(datetime.now() - self.changed_on)

    @property
    def created_on_delta_humanized(self) -> str | None:
        if self.created_on is None:
            return None
        import humanize

        return humanize.naturaltime(datetime.now() - self.created_on)

    @property
    def changed_by_name(self) -> str:
        if self.changed_by:
            return str(self.changed_by)
        return ""


class ImportExportMixin(UUIDMixin):
    """Marker mixin for models that support YAML import/export.

    The actual import/export logic lives in superset.importexport.
    This mixin provides the ``uuid`` column and export field declarations.
    """

    export_parent: str | None = None
    export_children: list[str] = []  # noqa: RUF012
    export_fields: list[str] = []  # noqa: RUF012
    extra_import_fields: list[str] = []  # noqa: RUF012


class ExtraJSONMixin:
    """Provides an ``extra_json`` Text column with a default of ``'{}'``."""

    extra_json = sa.Column("extra_json", Text, default="{}")


class CertificationMixin:
    """Certification tracking columns."""

    certified_by = sa.Column(Text, nullable=True)
    certification_details = sa.Column(Text, nullable=True)
