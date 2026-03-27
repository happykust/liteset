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
"""Dashboard model and association tables.

Pure SQLAlchemy -- no Flask dependencies.
"""
from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
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

dashboard_slices = Table(
    "dashboard_slices",
    metadata,
    Column("id", Integer, primary_key=True),
    Column(
        "dashboard_id",
        Integer,
        ForeignKey("dashboards.id", ondelete="CASCADE"),
    ),
    Column(
        "slice_id",
        Integer,
        ForeignKey("slices.id", ondelete="CASCADE"),
    ),
    UniqueConstraint("dashboard_id", "slice_id"),
)

dashboard_user = Table(
    "dashboard_user",
    metadata,
    Column("id", Integer, primary_key=True),
    Column(
        "user_id",
        Integer,
        ForeignKey("ab_user.id", ondelete="CASCADE"),
    ),
    Column(
        "dashboard_id",
        Integer,
        ForeignKey("dashboards.id", ondelete="CASCADE"),
    ),
)

DashboardRoles = Table(
    "dashboard_roles",
    metadata,
    Column("id", Integer, primary_key=True),
    Column(
        "dashboard_id",
        Integer,
        ForeignKey("dashboards.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "role_id",
        Integer,
        ForeignKey("ab_role.id", ondelete="CASCADE"),
        nullable=False,
    ),
)


# ---------------------------------------------------------------------------
# Dashboard model
# ---------------------------------------------------------------------------


class Dashboard(AuditMixinNullable, ImportExportMixin, Base):
    """A Superset dashboard."""

    __tablename__ = "dashboards"

    id = Column(Integer, primary_key=True)
    dashboard_title = Column(String(500))
    position_json = Column(MediumText())
    description = Column(Text)
    css = Column(MediumText())
    theme_id = Column(
        Integer, ForeignKey("themes.id"), nullable=True
    )
    certified_by = Column(Text)
    certification_details = Column(Text)
    json_metadata = Column(MediumText())
    slug = Column(String(255), unique=True)
    published = Column(Boolean, default=False)
    is_managed_externally = Column(
        Boolean, nullable=False, default=False
    )
    external_url = Column(Text, nullable=True)

    # -- relationships --------------------------------------------------------

    slices = relationship(
        "Slice",
        secondary=dashboard_slices,
        backref="dashboards",
    )
    owners = relationship(
        "User",
        secondary=dashboard_user,
        passive_deletes=True,
    )
    theme = relationship(
        "Theme",
        foreign_keys=[theme_id],
    )
    roles = relationship(
        "Role",
        secondary=DashboardRoles,
    )
    embedded = relationship(
        "EmbeddedDashboard",
        back_populates="dashboard",
        cascade="all, delete-orphan",
    )
