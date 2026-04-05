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
# mypy: ignore-errors
"""Dashboard model and association tables.

Pure SQLAlchemy -- no Flask dependencies.
"""

from __future__ import annotations

import json
import logging
from typing import Any

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

logger = logging.getLogger(__name__)

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
    theme_id = Column(Integer, ForeignKey("themes.id"), nullable=True)
    certified_by = Column(Text)
    certification_details = Column(Text)
    json_metadata = Column(MediumText())
    slug = Column(String(255), unique=True)
    published = Column(Boolean, default=False)
    is_managed_externally = Column(Boolean, nullable=False, default=False)
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
    tags = relationship(
        "Tag",
        secondary="tagged_object",
        primaryjoin="and_(Dashboard.id == foreign(TaggedObject.object_id), "
        "TaggedObject.object_type == 'dashboard')",
        secondaryjoin="Tag.id == foreign(TaggedObject.tag_id)",
        viewonly=True,
    )

    export_fields = [
        "dashboard_title",
        "position_json",
        "json_metadata",
        "description",
        "css",
        "slug",
        "certified_by",
        "certification_details",
        "published",
    ]
    extra_import_fields = ["is_managed_externally", "external_url", "theme_id"]

    # -- Computed properties (match original FAB model) ------------------------

    @property
    def url(self) -> str:
        return f"/superset/dashboard/{self.slug or self.id}/"

    @property
    def status(self) -> str:
        return "published" if self.published else "draft"

    @property
    def params(self) -> str:
        """Alias: ``params`` reads from ``json_metadata``."""
        return self.json_metadata

    @params.setter
    def params(self, value: str) -> None:
        """Alias: setting ``params`` writes to ``json_metadata``."""
        self.json_metadata = value

    @property
    def params_dict(self) -> dict[str, Any]:
        """Parsed json_metadata as a dict."""
        try:
            return json.loads(self.json_metadata or "{}") or {}
        except (TypeError, json.JSONDecodeError):
            return {}

    @property
    def position(self) -> dict[str, Any]:
        """Parsed position_json as a dict."""
        if self.position_json:
            try:
                return json.loads(self.position_json)
            except (TypeError, json.JSONDecodeError):
                return {}
        return {}

    @property
    def changed_by_name(self) -> str:
        """Return the name of the user who last changed the dashboard."""
        if not self.changed_by:
            return ""
        return str(self.changed_by)

    @property
    def data(self) -> dict[str, Any]:
        """Full data dict for serialisation, matching the original model."""
        positions = self.position_json
        if positions:
            try:
                positions = json.loads(positions)
            except (TypeError, json.JSONDecodeError):
                positions = None
        return {
            "id": self.id,
            "metadata": self.params_dict,
            "certified_by": self.certified_by,
            "certification_details": self.certification_details,
            "css": self.css,
            "dashboard_title": self.dashboard_title,
            "published": self.published,
            "slug": self.slug,
            "slices": [slc.data for slc in (self.slices or [])],
            "position_json": positions,
            "last_modified_time": (
                self.changed_on.replace(microsecond=0).timestamp()
                if self.changed_on
                else None
            ),
            "is_managed_externally": self.is_managed_externally,
        }

    @property
    def thumbnail_url(self) -> str | None:
        if not self.changed_on:
            return None
        digest = self.changed_on.strftime("%Y%m%d%H%M%S")
        return f"/api/v1/dashboard/{self.id}/thumbnail/{digest}/"
