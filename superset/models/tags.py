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
"""Tag models: Tag, TaggedObject, and supporting enums.

Pure SQLAlchemy -- no Flask dependencies.
"""
from __future__ import annotations

import enum

from sqlalchemy import (
    Column,
    Enum,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from superset.models.helpers import AuditMixinNullable, Base, metadata

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class TagType(enum.Enum):
    """Category of tag."""

    custom = 1
    type = 2
    owner = 3
    favorited_by = 4


class ObjectType(enum.Enum):
    """Entity types that can be tagged."""

    query = 1
    chart = 2
    dashboard = 3
    dataset = 4


# ---------------------------------------------------------------------------
# Association tables
# ---------------------------------------------------------------------------

user_favorite_tag_table = Table(
    "user_favorite_tag_table",
    metadata,
    Column("id", Integer, primary_key=True),
    Column(
        "user_id",
        Integer,
        ForeignKey("ab_user.id", ondelete="CASCADE"),
    ),
    Column(
        "tag_id",
        Integer,
        ForeignKey("tag.id", ondelete="CASCADE"),
    ),
)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class Tag(Base, AuditMixinNullable):
    """A tag that can be attached to various Superset objects."""

    __tablename__ = "tag"

    id = Column(Integer, primary_key=True)
    name = Column(String(250), unique=True)
    type = Column(Enum(TagType))
    description = Column(Text)

    # -- relationships --------------------------------------------------------

    objects = relationship(
        "TaggedObject",
        back_populates="tag",
    )
    users_favorited = relationship(
        "User",
        secondary=user_favorite_tag_table,
    )


class TaggedObject(Base, AuditMixinNullable):
    """Association between a Tag and a Superset object."""

    __tablename__ = "tagged_object"
    __table_args__ = (
        UniqueConstraint(
            "tag_id",
            "object_id",
            "object_type",
            name="uix_tagged_object",
        ),
    )

    id = Column(Integer, primary_key=True)
    tag_id = Column(
        Integer, ForeignKey("tag.id"), nullable=True
    )
    object_id = Column(Integer)
    object_type = Column(Enum(ObjectType))

    # -- relationships --------------------------------------------------------

    tag = relationship(
        "Tag",
        foreign_keys=[tag_id],
        back_populates="objects",
    )
