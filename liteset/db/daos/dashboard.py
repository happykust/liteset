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
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select

from liteset.db.base_dao import BaseAsyncDAO
from liteset.db.daos.favorites_mixin import FavoriteMixin
from superset.models.core import FavStarClassName
from superset.models.dashboard import Dashboard
from superset.models.embedded_dashboard import EmbeddedDashboard
from superset.models.slice import Slice


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

    async def set_dash_metadata(
        self,
        dashboard: Dashboard,
        data: dict[str, Any],
    ) -> None:
        """Update dashboard JSON metadata from data dict.

        Syncs slices from position data and filters default_filters
        to only include applicable slice IDs.
        """
        md: dict[str, Any] = {}
        if dashboard.json_metadata:
            try:
                md = json.loads(dashboard.json_metadata)
            except (json.JSONDecodeError, TypeError):
                pass

        # Handle positions and sync slices
        if "positions" in data:
            positions = data["positions"]
            if isinstance(positions, str):
                try:
                    positions = json.loads(positions)
                except (json.JSONDecodeError, TypeError):
                    positions = {}

            # Extract chart IDs from positions
            slice_ids = [
                value.get("meta", {}).get("chartId")
                for value in positions.values()
                if isinstance(value, dict) and value.get("meta", {}).get("chartId")
            ]

            if slice_ids:
                # Sync dashboard slices from positions
                stmt = select(Slice).where(Slice.id.in_(slice_ids))
                result = await self.session.execute(stmt)
                current_slices = list(result.scalars().all())
                await self.session.refresh(dashboard, attribute_names=["slices"])
                dashboard.slices = current_slices

                # Add UUID to positions
                uuid_map = {s.id: str(s.uuid) for s in current_slices}
                for obj in positions.values():
                    if (
                        isinstance(obj, dict)
                        and obj.get("type") == "CHART"
                        and obj.get("meta", {}).get("chartId")
                    ):
                        chart_id = obj["meta"]["chartId"]
                        obj["meta"]["uuid"] = uuid_map.get(chart_id)

            dashboard.position_json = json.dumps(
                positions, indent=None, separators=(",", ":"), sort_keys=True,
            )

            # Filter default_filters to applicable slices only
            if "default_filters" in data:
                try:
                    default_filters_data = json.loads(
                        data["default_filters"]
                        if isinstance(data["default_filters"], str)
                        else json.dumps(data["default_filters"])
                    )
                    applicable_filters = {
                        key: v
                        for key, v in default_filters_data.items()
                        if int(key) in slice_ids
                    }
                    md["default_filters"] = json.dumps(applicable_filters)
                except (json.JSONDecodeError, TypeError, ValueError):
                    md["default_filters"] = data["default_filters"]

            # positions have their own column, no need in metadata
            md.pop("positions", None)

        # Update simple metadata keys
        for key in (
            "color_namespace",
            "color_scheme",
            "label_colors",
            "shared_label_colors",
            "color_scheme_domain",
            "refresh_frequency",
            "timed_refresh_immune_slices",
            "expanded_slices",
            "cross_filters_enabled",
            "native_filter_configuration",
        ):
            if key in data:
                md[key] = data[key]

        dashboard.json_metadata = json.dumps(md)

    async def copy_dashboard(
        self,
        original_dash: Dashboard,
        data: dict[str, Any],
        current_user: Any | None = None,
    ) -> Dashboard:
        """Create a copy of a dashboard including its slices."""
        dash = Dashboard()
        dash.dashboard_title = data.get("dashboard_title", original_dash.dashboard_title)
        dash.description = data.get("description", original_dash.description)
        dash.css = data.get("css", original_dash.css)
        dash.position_json = original_dash.position_json
        dash.json_metadata = original_dash.json_metadata
        dash.published = original_dash.published
        dash.owners = [current_user] if current_user else []

        # Explicitly load lazy relationship to avoid MissingGreenlet in async context
        await self.session.refresh(original_dash, attribute_names=["slices"])
        if original_dash.slices:
            dash.slices = list(original_dash.slices)

        self.session.add(dash)
        return dash

    async def get_dashboard_changed_on(
        self,
        dashboard: Dashboard,
    ) -> datetime:
        """Return dashboard's last changed timestamp (truncated to seconds)."""
        changed_on = dashboard.changed_on
        if changed_on is None:
            return datetime.now(tz=timezone.utc).replace(microsecond=0)
        # Ensure timezone-aware before truncating
        if changed_on.tzinfo is None:
            changed_on = changed_on.replace(tzinfo=timezone.utc)
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
            s.datasource_id for s in slices
            if s.datasource_id and s.datasource_type == "table"
        }
        if not datasource_ids:
            return []
        from superset.connectors.sqla.models import SqlaTable
        stmt = select(SqlaTable).where(SqlaTable.id.in_(datasource_ids))
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_dashboard_and_slices_changed_on(
        self, dashboard: Dashboard,
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
        if slices_max.tzinfo is None:
            slices_max = slices_max.replace(tzinfo=timezone.utc)
        return max(dash_changed, slices_max.replace(microsecond=0))

    async def get_dashboard_and_datasets_changed_on(
        self, dashboard: Dashboard,
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
        if ds_max.tzinfo is None:
            ds_max = ds_max.replace(tzinfo=timezone.utc)
        return max(dash_changed, ds_max.replace(microsecond=0))

    async def update_native_filters_config(
        self, dashboard: Dashboard, native_filter_configuration: list[dict],
    ) -> None:
        """Update native filter configuration in dashboard metadata."""
        md: dict[str, Any] = {}
        if dashboard.json_metadata:
            try:
                md = json.loads(dashboard.json_metadata)
            except (json.JSONDecodeError, TypeError):
                pass
        md["native_filter_configuration"] = native_filter_configuration
        dashboard.json_metadata = json.dumps(md)

    async def update_colors_config(
        self, dashboard: Dashboard, data: dict[str, Any],
    ) -> None:
        """Update color-related keys in dashboard metadata."""
        md: dict[str, Any] = {}
        if dashboard.json_metadata:
            try:
                md = json.loads(dashboard.json_metadata)
            except (json.JSONDecodeError, TypeError):
                pass
        for key in ("color_namespace", "color_scheme", "label_colors",
                    "shared_label_colors", "color_scheme_domain"):
            if key in data:
                md[key] = data[key]
        dashboard.json_metadata = json.dumps(md)


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
            existing.allowed_domains = allowed_domains
            return existing
        embedded = EmbeddedDashboard(
            dashboard_id=dashboard_id,
            allowed_domains=allowed_domains,
        )
        self.session.add(embedded)
        return embedded
