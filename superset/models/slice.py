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
"""Slice (chart) model and association tables.

Pure SQLAlchemy -- no legacy WSGI dependencies.
"""

from __future__ import annotations

import json
import logging
from typing import Any, TYPE_CHECKING

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

if TYPE_CHECKING:
    from superset.models.connectors import SqlaTable

logger = logging.getLogger(__name__)

# Association tables

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


# Slice model


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
    last_saved_by_fk = Column(Integer, ForeignKey("ab_user.id"), nullable=True)
    certified_by = Column(Text)
    certification_details = Column(Text)
    is_managed_externally = Column(Boolean, nullable=False, default=False)
    external_url = Column(Text, nullable=True)

    def __repr__(self) -> str:
        return self.slice_name or str(self.id)

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
        primaryjoin="and_(Slice.datasource_id == SqlaTable.id, "
        "Slice.datasource_type == 'table')",
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

    export_fields = [
        "slice_name",
        "description",
        "certified_by",
        "certification_details",
        "datasource_type",
        "datasource_name",
        "viz_type",
        "params",
        "query_context",
        "cache_timeout",
    ]
    export_parent = "table"
    extra_import_fields = ["is_managed_externally", "external_url"]

    # -- Computed properties ---------------------------------------------------

    @property
    def datasource(self) -> SqlaTable | None:
        """Return the related datasource (the SqlaTable relationship)."""
        return self.table

    @property
    def form_data(self) -> dict[str, Any]:
        """Parsed params dict enriched with slice/datasource identifiers."""
        form_data: dict[str, Any] = {}
        try:
            parsed = json.loads(self.params) if self.params else {}
            # ``params`` is an unvalidated JSON string column — a valid-but-
            # non-object value (``[1,2]`` / ``"s"`` / ``5``) would make the
            # ``form_data.update(...)`` below raise AttributeError, and since
            # the chart LIST endpoint builds form_data for every row, ONE bad
            # chart 500s the whole list. Coerce to {} like ``params_dict``.
            form_data = parsed if isinstance(parsed, dict) else {}
        except (TypeError, json.JSONDecodeError):
            logger.error("Malformed json in slice's params", exc_info=True)

        form_data.update(
            {
                "slice_id": self.id,
                "viz_type": self.viz_type,
                "datasource": f"{self.datasource_id}__{self.datasource_type}",
            }
        )

        if self.cache_timeout:
            form_data["cache_timeout"] = self.cache_timeout

        from superset.legacy import update_time_range

        update_time_range(form_data)
        return form_data

    @property
    def params_dict(self) -> dict[str, Any]:
        """Parsed params as a dict.

        ``params`` is an unvalidated JSON string — coerce a non-object parse
        (``[1,2]`` / ``"s"`` / ``5``) to ``{}`` so callers that treat the
        result as a mapping don't raise (mirrors ``form_data``).
        """
        try:
            parsed = json.loads(self.params or "{}")
        except (TypeError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @property
    def description_markeddown(self) -> str:
        from superset.utils.core import markdown

        return markdown(self.description)

    @property
    def data(self) -> dict[str, Any]:
        """Data used to render slice in templates."""
        changed_on_humanized = self.changed_on_delta_humanized or ""
        return {
            "cache_timeout": self.cache_timeout,
            "changed_on": (self.changed_on.isoformat() if self.changed_on else ""),
            "changed_on_humanized": changed_on_humanized,
            "datasource": self.datasource_name,
            "description": self.description,
            "description_markeddown": self.description_markeddown,
            "edit_url": self.edit_url,
            "form_data": self.form_data,
            "query_context": self.query_context,
            "modified": f'<span class="no-wrap">{changed_on_humanized}</span>',
            "owners": [owner.id for owner in (self.owners or [])],
            "slice_id": self.id,
            "slice_name": self.slice_name,
            "slice_url": self.slice_url,
            "certified_by": self.certified_by,
            "certification_details": self.certification_details,
            "is_managed_externally": self.is_managed_externally,
        }

    @property
    def url(self) -> str:
        return f"/explore/?slice_id={self.id}"

    @property
    def edit_url(self) -> str:
        return f"/chart/edit/{self.id}"

    @property
    def slice_url(self) -> str:
        # The frontend ``ChartList`` relies on the ``&form_data=...`` suffix
        # to open a chart row in Explore.
        import urllib.parse as _urlparse

        params = _urlparse.quote(json.dumps({"slice_id": self.id}))
        return f"/explore/?slice_id={self.id}&form_data={params}"

    @property
    def datasource_name_text(self) -> str | None:
        if self.table:
            if self.table.schema:
                return f"{self.table.schema}.{self.table.table_name}"
            return self.table.table_name
        return None

    @property
    def datasource_url(self) -> str | None:
        """Return the datasource explore URL.

        Delegates to self.table.explore_url which respects SqlaTable.default_endpoint,
        then falls back to the standard /explore/ URL.  For non-table datasources
        falls back to self.datasource.explore_url.
        """
        if self.table:
            # Mirror SqlaTable.explore_url: prefer default_endpoint when set.
            if getattr(self.table, "default_endpoint", None):
                return self.table.default_endpoint
            return f"/explore/?datasource_type=table&datasource_id={self.datasource_id}"
        if (datasource := self.datasource) is not None:
            if getattr(datasource, "default_endpoint", None):
                return datasource.default_endpoint
            ds_type = getattr(datasource, "type", "table")
            return (
                f"/explore/?datasource_type={ds_type}"
                f"&datasource_id={self.datasource_id}"
            )
        return None

    @property
    def digest(self) -> str | None:
        from superset.thumbnails.digest import get_chart_digest

        return get_chart_digest(self)

    @property
    def thumbnail_url(self) -> str | None:
        """
        Returns a thumbnail URL with a HEX digest. We want to avoid browser cache
        if the dashboard has changed.
        """
        if digest := self.digest:
            return f"/api/v1/chart/{self.id}/thumbnail/{digest}/"

        return None
