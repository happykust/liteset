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
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from superset.db.base_dao import BaseAsyncDAO
from superset.db.daos.favorites_mixin import FavoriteMixin
from superset.models.core import FavStarClassName
from superset.models.dashboard import Dashboard
from superset.models.embedded_dashboard import EmbeddedDashboard
from superset.models.slice import Slice
from superset.utils.dashboard_filter_scopes_converter import copy_filter_scopes
from superset.utils.json import dumps, loads

logger = logging.getLogger(__name__)


class AsyncDashboardDAO(FavoriteMixin, BaseAsyncDAO[Dashboard]):
    model_cls = Dashboard
    _fav_class_name = FavStarClassName.DASHBOARD

    async def get_by_id_or_slug(
        self,
        id_or_slug: int | str,
    ) -> Dashboard | None:
        """Find a dashboard by integer ID, UUID, or slug."""
        # Try integer ID
        try:
            dash_id = int(id_or_slug)
            return await self.find_by_id(dash_id)
        except (ValueError, TypeError):
            pass

        # Try UUID
        try:
            uuid_val = UUID(str(id_or_slug))
            result = await self.find_one_or_none(uuid=uuid_val)
            if result:
                return result
        except ValueError:
            pass

        # Try slug
        return await self.find_one_or_none(slug=str(id_or_slug))

    async def find_with_filters_and_options(
        self,
        filters: list[Any],
        options: list[Any] | None = None,
    ) -> Dashboard | None:
        """Find a single dashboard matching all filters, with eager-loaded options."""
        stmt = select(Dashboard).where(*filters)
        if options:
            stmt = stmt.options(*options)
        result = await self.session.execute(stmt)
        return result.scalars().unique().one_or_none()

    async def find_by_id_with_options(
        self,
        dashboard_id: int,
        options: list[Any] | None = None,
    ) -> Dashboard | None:
        """Find a dashboard by ID with eager-loaded options."""
        stmt = select(Dashboard).where(Dashboard.id == dashboard_id)
        if options:
            stmt = stmt.options(*options)
        result = await self.session.execute(stmt)
        return result.scalars().unique().one_or_none()

    async def validate_slug_uniqueness(self, slug: str) -> bool:
        """Check that no dashboard exists with the given slug."""
        if not slug:
            return True
        existing = await self.find_one_or_none(slug=slug)
        return existing is None

    async def validate_update_slug_uniqueness(
        self,
        dashboard_id: int,
        slug: str | None,
    ) -> bool:
        """Check slug uniqueness excluding the dashboard being updated."""
        if slug is None:
            return True
        stmt = select(Dashboard).where(
            Dashboard.slug == slug,
            Dashboard.id != dashboard_id,
        )
        result = await self.session.execute(stmt)
        return result.scalars().one_or_none() is None

    async def set_dash_metadata(  # noqa: C901
        self,
        dashboard: Dashboard,
        data: dict[str, Any],
        old_to_new_slice_ids: dict[int, int] | None = None,
    ) -> None:
        """Update dashboard JSON metadata from data dict.

        Ports the original ``DashboardDAO.set_dash_metadata`` logic:
        - Syncs slices from position data
        - Adds UUID references into position entries
        - Handles ``filter_scopes`` remapping when slices are duplicated
        - Filters ``default_filters`` to only applicable slice IDs
        - Sets all remaining metadata keys with their proper defaults
        """
        new_filter_scopes: dict[str, Any] = {}
        md: dict[str, Any] = {}
        if dashboard.json_metadata:
            try:
                md = loads(dashboard.json_metadata)  # type: ignore[arg-type]
            except (ValueError, TypeError):
                pass

        # The original FAB/Marshmallow schema (JsonMetadataSchema) parses the
        # json_metadata string and extracts nested keys (positions, color_scheme,
        # etc.) as top-level fields in `data`.  Our controller passes
        # json_metadata as a raw string, so we must parse it here to extract
        # positions and other nested fields that set_dash_metadata relies on.
        if "json_metadata" in data and isinstance(data["json_metadata"], str):
            try:
                parsed_meta = loads(data["json_metadata"])
                if isinstance(parsed_meta, dict):
                    # Merge parsed keys into data for downstream lookups,
                    # but don't overwrite explicitly provided top-level keys.
                    for key in (
                        "positions",
                        "color_scheme",
                        "color_namespace",
                        "label_colors",
                        "shared_label_colors",
                        "map_label_colors",
                        "color_scheme_domain",
                        "expanded_slices",
                        "refresh_frequency",
                        "timed_refresh_immune_slices",
                        "default_filters",
                        "filter_scopes",
                        "cross_filters_enabled",
                        "filter_bar_orientation",
                        "native_filter_configuration",
                    ):
                        if key in parsed_meta and key not in data:
                            data[key] = parsed_meta[key]
            except (ValueError, TypeError):
                pass

        if (positions := data.get("positions")) is not None:
            # find slices in the position data
            slice_ids = [
                value.get("meta", {}).get("chartId")
                for value in positions.values()
                if isinstance(value, dict)
            ]

            stmt = select(Slice).where(Slice.id.in_(slice_ids))
            result = await self.session.execute(stmt)
            current_slices = list(result.scalars().all())

            await self.session.refresh(dashboard, attribute_names=["slices"])
            dashboard.slices = current_slices

            # add UUID to positions
            uuid_map = {slc.id: str(slc.uuid) for slc in current_slices}
            for obj in positions.values():
                if (
                    isinstance(obj, dict)
                    and obj["type"] == "CHART"
                    and obj["meta"]["chartId"]
                ):
                    chart_id = obj["meta"]["chartId"]
                    obj["meta"]["uuid"] = uuid_map.get(chart_id)

            # remove leading and trailing white spaces in the dumped json
            dashboard.position_json = dumps(  # type: ignore[assignment]
                positions,
                indent=None,
                separators=(",", ":"),
                sort_keys=True,
            )

            if "filter_scopes" in data:
                # replace filter_id and immune ids from old slice id to new slice id:
                # and remove slice ids that are not in dash anymore
                slc_id_dict: dict[int, int] = {}
                if old_to_new_slice_ids:
                    slc_id_dict = {
                        old: new
                        for old, new in old_to_new_slice_ids.items()
                        if new in slice_ids
                    }
                else:
                    slc_id_dict = {sid: sid for sid in slice_ids if sid is not None}
                new_filter_scopes = copy_filter_scopes(
                    old_to_new_slc_id_dict=slc_id_dict,
                    old_filter_scopes=loads(data["filter_scopes"] or "{}")
                    if isinstance(data["filter_scopes"], str)
                    else data["filter_scopes"],
                )

            default_filters_data = loads(data.get("default_filters", "{}"))
            applicable_filters = {
                key: v
                for key, v in default_filters_data.items()
                if int(key) in slice_ids
            }
            md["default_filters"] = dumps(applicable_filters)

            # positions have their own column, no need to store it in metadata
            md.pop("positions", None)

        if new_filter_scopes:
            md["filter_scopes"] = new_filter_scopes
        else:
            md.pop("filter_scopes", None)

        md.setdefault("timed_refresh_immune_slices", [])

        if data.get("color_namespace") is None:
            md.pop("color_namespace", None)
        else:
            md["color_namespace"] = data.get("color_namespace")

        md["expanded_slices"] = data.get("expanded_slices", {})
        md["refresh_frequency"] = data.get("refresh_frequency", 0)
        md["color_scheme"] = data.get("color_scheme", "")
        md["label_colors"] = data.get("label_colors", {})
        md["shared_label_colors"] = data.get("shared_label_colors", [])
        md["map_label_colors"] = data.get("map_label_colors", {})
        md["color_scheme_domain"] = data.get("color_scheme_domain", [])
        md["cross_filters_enabled"] = data.get("cross_filters_enabled", True)
        dashboard.json_metadata = dumps(md)  # type: ignore[assignment]

    async def copy_dashboard(
        self,
        original_dash: Dashboard,
        data: dict[str, Any],
        current_user: Any | None = None,
    ) -> Dashboard:
        """Create a copy of a dashboard including its slices.

        Ports the original ``DashboardDAO.copy_dashboard`` logic:
        - When ``duplicate_slices`` is True, each slice is cloned and
          ``positions`` metadata is updated with new chartId references.
        - Calls ``set_dash_metadata`` with the old-to-new slice ID mapping
          so that filter_scopes and default_filters are properly remapped.
        """
        dash = Dashboard()
        dash.owners = [current_user] if current_user else []
        dash.dashboard_title = data["dashboard_title"]
        dash.css = data.get("css")

        metadata = loads(data["json_metadata"])
        old_to_new_slice_ids: dict[int, int] = {}

        # Explicitly load lazy relationship to avoid MissingGreenlet in async context
        await self.session.refresh(original_dash, attribute_names=["slices"])

        if data.get("duplicate_slices"):
            # Duplicating slices as well, mapping old ids to new ones
            for slc in original_dash.slices:
                new_slice = Slice(
                    slice_name=slc.slice_name,
                    datasource_id=slc.datasource_id,
                    datasource_type=slc.datasource_type,
                    datasource_name=getattr(slc, "datasource_name", None),
                    viz_type=slc.viz_type,
                    params=slc.params,
                    description=getattr(slc, "description", None),
                    cache_timeout=getattr(slc, "cache_timeout", None),
                )
                if current_user is not None:
                    new_slice.owners = [current_user]
                self.session.add(new_slice)
                await self.session.flush()
                new_slice.dashboards.append(dash)
                old_to_new_slice_ids[slc.id] = new_slice.id

            # update chartId of layout entities
            for value in metadata.get("positions", {}).values():
                if isinstance(value, dict) and value.get("meta", {}).get("chartId"):
                    old_id = value["meta"]["chartId"]
                    new_id = old_to_new_slice_ids.get(old_id)
                    value["meta"]["chartId"] = new_id
        else:
            dash.slices = list(original_dash.slices)

        dash.params = original_dash.params
        await self.set_dash_metadata(dash, metadata, old_to_new_slice_ids)
        self.session.add(dash)
        return dash

    async def get_dashboard_changed_on(
        self,
        dashboard: Dashboard,
    ) -> datetime:
        """Return dashboard's last changed timestamp (truncated to seconds)."""
        changed_on = dashboard.changed_on
        if changed_on is None:
            return datetime.now().replace(microsecond=0)
        return changed_on.replace(microsecond=0)

    async def get_charts_for_dashboard(self, dashboard: Dashboard) -> list[Slice]:
        """Get all charts (slices) for a dashboard."""
        await self.session.refresh(dashboard, attribute_names=["slices"])
        return list(dashboard.slices) if dashboard.slices else []

    async def get_datasets_for_dashboard(self, dashboard: Dashboard) -> list[Any]:
        """Get all datasets used by a dashboard's charts."""
        slices = await self.get_charts_for_dashboard(dashboard)
        if not slices:
            return []
        datasource_ids = {
            s.datasource_id
            for s in slices
            if s.datasource_id and s.datasource_type == "table"
        }
        if not datasource_ids:
            return []
        from superset.models.connectors import SqlaTable

        stmt = (
            select(SqlaTable)
            .where(SqlaTable.id.in_(datasource_ids))
            .options(
                selectinload(SqlaTable.database),
                selectinload(SqlaTable.columns),
                selectinload(SqlaTable.metrics),
                selectinload(SqlaTable.owners),
            )
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_dashboard_and_slices_changed_on(
        self,
        dashboard: Dashboard,
    ) -> datetime:
        """Get max changed_on across dashboard and all its slices."""
        dash_changed = await self.get_dashboard_changed_on(dashboard)
        slices = await self.get_charts_for_dashboard(dashboard)
        if not slices:
            return dash_changed
        slice_times = [s.changed_on for s in slices if s.changed_on]
        if not slice_times:
            return dash_changed
        slices_max = max(slice_times)
        return max(dash_changed, slices_max.replace(microsecond=0))

    async def get_dashboard_and_datasets_changed_on(
        self,
        dashboard: Dashboard,
    ) -> datetime:
        """Get max changed_on across dashboard and all its datasets."""
        dash_changed = await self.get_dashboard_changed_on(dashboard)
        datasets = await self.get_datasets_for_dashboard(dashboard)
        if not datasets:
            return dash_changed
        ds_times = [d.changed_on for d in datasets if d.changed_on]
        if not ds_times:
            return dash_changed
        ds_max = max(ds_times)
        return max(dash_changed, ds_max.replace(microsecond=0))

    async def update_native_filters_config(
        self,
        dashboard: Dashboard,
        native_filter_configuration: list[dict[str, Any]],
    ) -> None:
        """Update native filter configuration in dashboard metadata."""
        md: dict[str, Any] = {}
        if dashboard.json_metadata:
            try:
                md = loads(dashboard.json_metadata)  # type: ignore[arg-type]
            except (ValueError, TypeError):
                pass
        md["native_filter_configuration"] = native_filter_configuration
        dashboard.json_metadata = dumps(md)  # type: ignore[assignment]

    async def update_colors_config(
        self,
        dashboard: Dashboard,
        data: dict[str, Any],
        mark_updated: bool = True,
    ) -> None:
        """Update color-related keys in dashboard metadata."""
        md: dict[str, Any] = {}
        if dashboard.json_metadata:
            try:
                md = loads(dashboard.json_metadata)  # type: ignore[arg-type]
            except (ValueError, TypeError):
                pass
        for key in (
            "color_namespace",
            "color_scheme",
            "label_colors",
            "shared_label_colors",
            "color_scheme_domain",
            "map_label_colors",
        ):
            if key in data:
                md[key] = data[key]
        dashboard.json_metadata = dumps(md)  # type: ignore[assignment]
        if not mark_updated:
            # Preserve the current changed_on so the onupdate trigger is suppressed
            prev_changed_on = dashboard.changed_on
            # Re-assign to override the SQLAlchemy onupdate after flush
            dashboard.changed_on = prev_changed_on


class AsyncEmbeddedDashboardDAO(BaseAsyncDAO[EmbeddedDashboard]):
    model_cls = EmbeddedDashboard

    async def find_by_dashboard_id(
        self,
        dashboard_id: int,
    ) -> EmbeddedDashboard | None:
        """Find embedded dashboard config by dashboard ID."""
        return await self.find_one_or_none(dashboard_id=dashboard_id)

    async def find_by_uuid(
        self,
        uuid_val: str,
    ) -> EmbeddedDashboard | None:
        """Find embedded dashboard by UUID."""
        try:
            parsed = UUID(uuid_val)
        except ValueError:
            return None
        return await self.find_one_or_none(uuid=parsed)

    async def upsert(
        self,
        dashboard_id: int,
        allowed_domains: list[str],
    ) -> EmbeddedDashboard:
        """Create or update embedded dashboard config."""
        existing = await self.find_by_dashboard_id(dashboard_id)
        if existing:
            existing.allow_domain_list = ",".join(allowed_domains)  # type: ignore[assignment]
            return existing
        embedded = EmbeddedDashboard(
            dashboard_id=dashboard_id,
            allow_domain_list=",".join(allowed_domains),
        )
        self.session.add(embedded)
        return embedded
