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

Pure SQLAlchemy -- no legacy WSGI dependencies.
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from typing import Any, TYPE_CHECKING

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
from sqlalchemy.orm import Mapped, mapped_column, object_session, relationship

from superset.models.helpers import (
    AuditMixinNullable,
    Base,
    ImportExportMixin,
    MediumText,
    metadata,
)

if TYPE_CHECKING:
    from superset.models.core import Theme
    from superset.models.embedded_dashboard import EmbeddedDashboard
    from superset.models.security import Role, User
    from superset.models.slice import Slice
    from superset.models.tags import Tag

logger = logging.getLogger(__name__)

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


class Dashboard(AuditMixinNullable, ImportExportMixin, Base):
    """A Superset dashboard."""

    __tablename__ = "dashboards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dashboard_title: Mapped[str | None] = mapped_column(String(500))
    position_json: Mapped[str | None] = mapped_column(MediumText())
    description: Mapped[str | None] = mapped_column(Text)
    css: Mapped[str | None] = mapped_column(MediumText())
    theme_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("themes.id"), nullable=True
    )
    certified_by: Mapped[str | None] = mapped_column(Text)
    certification_details: Mapped[str | None] = mapped_column(Text)
    json_metadata: Mapped[str | None] = mapped_column(MediumText())
    slug: Mapped[str | None] = mapped_column(String(255), unique=True)
    published: Mapped[bool | None] = mapped_column(Boolean, default=False)
    is_managed_externally: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    external_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"Dashboard<{self.id or self.slug}>"

    # -- relationships --------------------------------------------------------

    slices: Mapped[list["Slice"]] = relationship(
        "Slice",
        secondary=dashboard_slices,
        backref="dashboards",
    )
    owners: Mapped[list["User"]] = relationship(
        "User",
        secondary=dashboard_user,
        passive_deletes=True,
    )
    theme: Mapped["Theme | None"] = relationship(
        "Theme",
        foreign_keys=[theme_id],
    )
    roles: Mapped[list["Role"]] = relationship(
        "Role",
        secondary=DashboardRoles,
    )
    embedded: Mapped[list["EmbeddedDashboard"]] = relationship(
        "EmbeddedDashboard",
        back_populates="dashboard",
        cascade="all, delete-orphan",
    )
    tags: Mapped[list["Tag"]] = relationship(
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

    # -- Computed properties (match original upstream model) -------------------

    @property
    def url(self) -> str:
        return f"/superset/dashboard/{self.slug or self.id}/"

    @property
    def datasources(self) -> set[Any] | None:
        """Enumerate all distinct datasource objects across the dashboard's slices.

        Groups slices by their datasource model class (cls_model) and
        batch-queries each model to return a deduplicated set of instances.

        Returns ``None`` when the object is bound to an ``AsyncSession`` because
        synchronous I/O on the session's sync proxy raises ``MissingGreenlet``;
        callers fall back to an async-safe slice-iteration path in that case.
        """
        from sqlalchemy import select
        from sqlalchemy.ext.asyncio import async_object_session

        from superset.models.connectors import SqlaTable
        from superset.models.sql_lab import Query, SavedQuery

        # If this object is managed by an AsyncSession, synchronous I/O on the
        # underlying sync_session raises MissingGreenlet.  Return None so that
        # async callers use their own async-safe fallback path.
        if async_object_session(self) is not None:
            return None

        session = object_session(self)
        if session is None:
            return set()

        # Verbose but efficient database enumeration of dashboard datasources.
        datasources_by_cls_model: dict[Any, set[int]] = defaultdict(set)

        for slc in self.slices:
            if slc.datasource_type == "table":
                datasources_by_cls_model[SqlaTable].add(slc.datasource_id)
            elif slc.datasource_type == "query":
                datasources_by_cls_model[Query].add(slc.datasource_id)
            elif slc.datasource_type == "saved_query":
                datasources_by_cls_model[SavedQuery].add(slc.datasource_id)

        result: set[Any] = set()
        for cls_model, datasource_ids in datasources_by_cls_model.items():
            if not datasource_ids:
                continue
            rows = session.execute(
                select(cls_model).where(cls_model.id.in_(datasource_ids))
            ).scalars()
            result.update(rows)
        return result

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
        """Parsed json_metadata as a dict.

        Coerce a non-object parse (``[1,2]`` / ``"s"`` / ``5``) to ``{}`` so
        callers treating the result as a mapping don't raise. The schema now
        rejects non-object json_metadata on write, but imports / legacy rows
        may still carry one.
        """
        try:
            raw = self.json_metadata or "{}"
            # Strip trailing commas before closing braces/brackets so that
            # legacy rows written by older tools parse correctly.
            raw = re.sub(r",[ \t\r\n]+}", "}", raw)
            raw = re.sub(r",[ \t\r\n]+\]", "]", raw)
            parsed = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @property
    def position(self) -> dict[str, Any]:
        """Parsed position_json as a dict (non-object coerced to ``{}``)."""
        if self.position_json:
            try:
                parsed = json.loads(self.position_json)
            except (TypeError, json.JSONDecodeError):
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return {}

    @property
    def charts(self) -> list[str]:
        """Slice names of every attached chart (used for thumbnail digest hashing)."""
        return [slc.slice_name or "<empty>" for slc in (self.slices or [])]

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
    def digest(self) -> str | None:
        from superset.thumbnails.digest import get_dashboard_digest

        return get_dashboard_digest(self)

    @property
    def thumbnail_url(self) -> str | None:
        """
        Returns a thumbnail URL with a HEX digest. We want to avoid browser cache
        if the dashboard has changed.
        """
        if digest := self.digest:
            return f"/api/v1/dashboard/{self.id}/thumbnail/{digest}/"

        return None
