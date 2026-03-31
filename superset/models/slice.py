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

from superset.models.helpers import (
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
    tags = relationship(
        "Tag",
        secondary="tagged_object",
        primaryjoin="and_(Slice.id == foreign(TaggedObject.object_id), "
        "TaggedObject.object_type == 'chart')",
        secondaryjoin="Tag.id == foreign(TaggedObject.tag_id)",
        viewonly=True,
    )

    # -- Computed properties ---------------------------------------------------

    @property
    def url(self) -> str:
        return f"/explore/?form_data=%7B%22slice_id%22%3A%20{self.id}%7D"

    @property
    def edit_url(self) -> str:
        return f"/chart/edit/{self.id}"

    @property
    def slice_url(self) -> str:
        return self.url

    @property
    def datasource_name_text(self) -> str | None:
        if self.table:
            return self.table.table_name
        return None

    @property
    def datasource_url(self) -> str | None:
        if self.table:
            return (
                f"/explore/?datasource_type=table"
                f"&datasource_id={self.datasource_id}"
            )
        return None

    @property
    def thumbnail_url(self) -> str | None:
        if not self.changed_on:
            return None
        digest = self.changed_on.strftime("%Y%m%d%H%M%S")
        return f"/api/v1/chart/{self.id}/thumbnail/{digest}/"
