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
from sqlalchemy.orm import object_session, relationship

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

    def __repr__(self) -> str:
        # 1:1 superset_old/models/dashboard.py:184 — /related/ dropdown text.
        return f"Dashboard<{self.id or self.slug}>"

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

    # -- Computed properties (match original upstream model) -------------------

    @property
    def url(self) -> str:
        return f"/superset/dashboard/{self.slug or self.id}/"

    @property
    def datasources(self) -> set[Any] | None:
        """Enumerate all distinct datasource objects across the dashboard's slices.

        Mirrors the original Dashboard.datasources property
        (superset_old/models/dashboard.py:196-212). Groups slices by their
        datasource model class (cls_model), then batch-queries each model to
        return a deduplicated set of BaseDatasource instances.

        In liteset, ``Slice.cls_model`` is not ported, but the only concrete
        datasource type is SqlaTable, so we import it directly and batch-query
        by datasource_id.

        Returns ``None`` when the object is bound to an ``AsyncSession``.  In
        that case synchronous I/O on the session's sync proxy raises
        ``MissingGreenlet``; callers (security/manager.py ``raise_for_access``
        and ``can_access_dashboard``) fall back to the async-safe
        slice-iteration path when ``datasources is None``.
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
            # legacy rows written by older tools (which allowed trailing commas)
            # parse correctly, matching the original json_to_dict behaviour
            # (superset_old/models/helpers.py:140-146).
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
        """Slice names of every attached chart, mirroring the original
        ``Dashboard.charts`` property used for thumbnail digest hashing.
        """
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
