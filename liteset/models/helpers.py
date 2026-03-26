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
"""Model base, mixin classes, and helper types for Liteset.

Pure SQLAlchemy — no Flask dependencies.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Column, Text
from sqlalchemy.dialects.mysql import LONGTEXT, MEDIUMTEXT
from sqlalchemy.ext.declarative import declared_attr
from sqlalchemy.orm import DeclarativeBase, Mapped, relationship
from sqlalchemy.sql.sqltypes import Variant
from sqlalchemy_utils import UUIDType


class Base(DeclarativeBase):
    """Declarative base for all Liteset models."""

    __allow_unmapped__ = True


metadata = Base.metadata


# ---------------------------------------------------------------------------
# Column type helpers
# ---------------------------------------------------------------------------


def MediumText() -> Variant:  # noqa: N802
    return Text().with_variant(MEDIUMTEXT(), "mysql")


def LongText() -> Variant:  # noqa: N802
    return Text().with_variant(LONGTEXT(), "mysql")


# ---------------------------------------------------------------------------
# Mixins
# ---------------------------------------------------------------------------


class UUIDMixin:
    """Adds a ``uuid`` column (unique, non-PK by default)."""

    uuid = sa.Column(
        UUIDType(binary=True),
        primary_key=False,
        unique=True,
        default=uuid.uuid4,
    )


class AuditMixinNullable:
    """Audit columns: created/changed timestamps and user FKs.

    Replaces Flask-AppBuilder's AuditMixin with pure SQLAlchemy columns.
    All fields are nullable so legacy rows without audit data still load.
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


class ImportExportMixin(UUIDMixin):
    """Marker mixin for models that support YAML import/export.

    The actual import/export logic lives in liteset.importexport.
    This mixin provides the ``uuid`` column and export field declarations.
    """

    export_parent: str | None = None
    export_children: list[str] = []  # noqa: RUF012
    export_fields: list[str] = []  # noqa: RUF012
    extra_import_fields: list[str] = []  # noqa: RUF012


class ExtraJSONMixin:
    """Provides an ``extra`` Text column with a default of ``'{}'``."""

    extra = sa.Column(Text, default="{}")


class CertificationMixin:
    """Certification tracking columns."""

    certified_by = sa.Column(Text, nullable=True)
    certification_details = sa.Column(Text, nullable=True)
