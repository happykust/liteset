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
"""Slice (chart) model and association tables.

Pure SQLAlchemy -- no Flask dependencies.
"""
from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
)
from sqlalchemy.orm import relationship

from liteset.models.helpers import (
    AuditMixinNullable,
    Base,
    ImportExportMixin,
    MediumText,
    metadata,
)


# ---------------------------------------------------------------------------
# Association tables
# ---------------------------------------------------------------------------

slice_user = Table(
    "slice_user",
    metadata,
    Column("id", Integer, primary_key=True),
    Column(
        "user_id",
        Integer,
        ForeignKey("ab_user.id", ondelete="CASCADE"),
    ),
    Column(
        "slice_id",
        Integer,
        ForeignKey("slices.id", ondelete="CASCADE"),
    ),
)


# ---------------------------------------------------------------------------
# Slice model
# ---------------------------------------------------------------------------


class Slice(AuditMixinNullable, ImportExportMixin, Base):
    """A saved chart / visualization (historically called a 'slice')."""

    __tablename__ = "slices"

    id = Column(Integer, primary_key=True)
    slice_name = Column(String(250))
    datasource_id = Column(Integer)
    datasource_type = Column(String(200))
    datasource_name = Column(String(2000))
    viz_type = Column(String(250))
    params = Column(MediumText())
    query_context = Column(MediumText())
    description = Column(Text)
    cache_timeout = Column(Integer)
    perm = Column(String(1000))
    schema_perm = Column(String(1000))
    catalog_perm = Column(String(1000))
    last_saved_at = Column(DateTime, nullable=True)
    last_saved_by_fk = Column(
        Integer, ForeignKey("ab_user.id"), nullable=True
    )
    certified_by = Column(Text)
    certification_details = Column(Text)
    is_managed_externally = Column(
        Boolean, nullable=False, default=False
    )
    external_url = Column(Text, nullable=True)

    # -- relationships --------------------------------------------------------

    last_saved_by = relationship(
        "User",
        foreign_keys=[last_saved_by_fk],
    )
    owners = relationship(
        "User",
        secondary=slice_user,
        passive_deletes=True,
    )
    table = relationship(
        "SqlaTable",
        foreign_keys=[datasource_id],
        primaryjoin="Slice.datasource_id == SqlaTable.id",
        viewonly=True,
    )
