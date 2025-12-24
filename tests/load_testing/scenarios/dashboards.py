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

"""
Dashboard scenarios for load testing.
"""

import json
import logging
import time
from typing import Any, TYPE_CHECKING

from ..utils.helpers import random_choice, random_string
from ..utils.metrics import get_metrics_collector, MetricsTimer

if TYPE_CHECKING:
    from ..utils.api_client import SupersetAPIClient

logger = logging.getLogger(__name__)


class DashboardScenarios:
    """Dashboard-related load testing scenarios."""

    def __init__(self, client: "SupersetAPIClient"):
        self.client = client
        self.metrics = get_metrics_collector()
        self._dashboard_cache: list[dict] = []
        self._chart_cache: dict[int, list[dict]] = {}

    def _refresh_dashboard_cache(self) -> None:
        """Refresh local cache of dashboard IDs."""
        result = self.client.get_dashboards(page_size=100)
        if result and "result" in result:
            self._dashboard_cache = result["result"]

    def _get_random_dashboard_id(self) -> int | None:
        """Get random dashboard ID from cache."""
        if not self._dashboard_cache:
            self._refresh_dashboard_cache()
        if self._dashboard_cache:
            return random_choice(self._dashboard_cache).get("id")
        return None

    def list_dashboards(
        self, page: int = 0, page_size: int = 25, filters: list | None = None
    ) -> dict | None:
        """
        Scenario: List dashboards with pagination
        Simulates browsing dashboard list.
        """
        with MetricsTimer("dashboard.list"):
            result = self.client.get_dashboards(
                page=page, page_size=page_size, filters=filters
            )
            if result and "result" in result:
                self._dashboard_cache = result["result"]
            return result

    def list_dashboards_paginated(self, total_pages: int = 5) -> list[dict]:
        """
        Scenario: Paginate through dashboard list
        Simulates user browsing multiple pages.
        """
        all_results = []
        for page in range(total_pages):
            result = self.list_dashboards(page=page, page_size=25)
            if result and "result" in result:
                all_results.extend(result["result"])
                if len(result["result"]) < 25:
                    break
            time.sleep(0.1)  # Small delay between pages
        return all_results

    def view_dashboard(self, dashboard_id: int | None = None) -> dict | None:
        """
        Scenario: View single dashboard
        Simulates opening a dashboard.
        """
        if dashboard_id is None:
            dashboard_id = self._get_random_dashboard_id()

        if dashboard_id is None:
            logger.warning("No dashboard available to view")
            return None

        with MetricsTimer("dashboard.view"):
            return self.client.get_dashboard(dashboard_id)

    def view_dashboard_with_charts(
        self, dashboard_id: int | None = None
    ) -> tuple[dict | None, list[dict]]:
        """
        Scenario: View dashboard and load all charts
        Simulates full dashboard load with all chart data.
        """
        if dashboard_id is None:
            dashboard_id = self._get_random_dashboard_id()

        if dashboard_id is None:
            logger.warning("No dashboard available to view")
            return None, []

        # Get dashboard
        with MetricsTimer("dashboard.view_full"):
            dashboard = self.client.get_dashboard(dashboard_id)

        if not dashboard:
            return None, []

        # Get charts for dashboard
        charts_result = self.client.get_dashboard_charts(dashboard_id)
        charts = []

        if charts_result and "result" in charts_result:
            charts = charts_result["result"]
            self._chart_cache[dashboard_id] = charts

        return dashboard, charts

    def load_dashboard_chart_data(
        self, dashboard_id: int | None = None, force_refresh: bool = False
    ) -> list[dict]:
        """
        Scenario: Load all chart data for a dashboard
        Heavy scenario - fetches data for all charts on dashboard.
        """
        if dashboard_id is None:
            dashboard_id = self._get_random_dashboard_id()

        if dashboard_id is None:
            return []

        # Get charts if not cached
        if dashboard_id not in self._chart_cache:
            charts_result = self.client.get_dashboard_charts(dashboard_id)
            if charts_result and "result" in charts_result:
                self._chart_cache[dashboard_id] = charts_result["result"]

        charts = self._chart_cache.get(dashboard_id, [])
        results = []

        for chart in charts:
            chart_id = chart.get("id")
            if not chart_id:
                continue

            # Build query context from chart's query_context or viz params
            query_context = chart.get("query_context")
            if query_context:
                if isinstance(query_context, str):
                    query_context = json.loads(query_context)

                # Set force flag
                if force_refresh:
                    query_context["force"] = True
                    for query in query_context.get("queries", []):
                        query["force"] = True

                with MetricsTimer("dashboard.chart_data", {"chart_id": str(chart_id)}):
                    result = self.client.get_chart_data(query_context)
                    if result:
                        results.append(result)

                        # Track cache hit/miss
                        is_cached = result.get("result", [{}])[0].get(
                            "is_cached", False
                        )
                        self.metrics.record_cache_hit(is_cached)

        return results

    def get_dashboard_charts(self, dashboard_id: int | None = None) -> dict | None:
        """
        Scenario: Get charts metadata for dashboard
        Lighter than loading full chart data.
        """
        if dashboard_id is None:
            dashboard_id = self._get_random_dashboard_id()

        if dashboard_id is None:
            return None

        with MetricsTimer("dashboard.get_charts"):
            result = self.client.get_dashboard_charts(dashboard_id)
            if result and "result" in result:
                self._chart_cache[dashboard_id] = result["result"]
            return result

    def get_dashboard_datasets(self, dashboard_id: int | None = None) -> dict | None:
        """
        Scenario: Get datasets used by dashboard
        """
        if dashboard_id is None:
            dashboard_id = self._get_random_dashboard_id()

        if dashboard_id is None:
            return None

        with MetricsTimer("dashboard.get_datasets"):
            return self.client.get(
                f"/api/v1/dashboard/{dashboard_id}/datasets",
                name="GET /api/v1/dashboard/<id>/datasets",
            )

    def apply_filter(
        self, dashboard_id: int, filter_id: str, filter_value: Any
    ) -> dict | None:
        """
        Scenario: Apply native filter to dashboard
        Simulates user interacting with dashboard filters.
        """
        # This is typically done client-side, but we can simulate
        # by reloading charts with updated filter state
        with MetricsTimer("dashboard.apply_filter"):
            # In practice, this triggers chart data reloads
            # We'll simulate by loading chart data with the filter
            return self.load_dashboard_chart_data(  # type: ignore[return-value]
                dashboard_id, force_refresh=True
            )

    def add_to_favorites(self, dashboard_id: int | None = None) -> dict | None:
        """
        Scenario: Add dashboard to favorites
        """
        if dashboard_id is None:
            dashboard_id = self._get_random_dashboard_id()

        if dashboard_id is None:
            return None

        with MetricsTimer("dashboard.add_favorite"):
            return self.client.add_favorite("Dashboard", dashboard_id)

    def remove_from_favorites(self, dashboard_id: int | None = None) -> dict | None:
        """
        Scenario: Remove dashboard from favorites
        """
        if dashboard_id is None:
            dashboard_id = self._get_random_dashboard_id()

        if dashboard_id is None:
            return None

        with MetricsTimer("dashboard.remove_favorite"):
            return self.client.remove_favorite("Dashboard", dashboard_id)

    def get_favorite_status(self, dashboard_ids: list[int]) -> dict | None:
        """
        Scenario: Check favorite status for dashboards
        """
        params = {"q": json.dumps(dashboard_ids)}
        with MetricsTimer("dashboard.favorite_status"):
            return self.client.get(
                "/api/v1/dashboard/favorite_status/",
                name="GET /api/v1/dashboard/favorite_status",
                params=params,
            )

    def export_dashboard(self, dashboard_id: int | None = None) -> bytes | None:
        """
        Scenario: Export dashboard
        Downloads dashboard as ZIP file.
        """
        if dashboard_id is None:
            dashboard_id = self._get_random_dashboard_id()

        if dashboard_id is None:
            return None

        params = {"q": json.dumps([dashboard_id])}
        with MetricsTimer("dashboard.export"):
            return self.client.get(
                "/api/v1/dashboard/export/",
                name="GET /api/v1/dashboard/export",
                params=params,
            )

    def create_dashboard(
        self, title: str | None = None, slug: str | None = None
    ) -> dict | None:
        """
        Scenario: Create new dashboard
        """
        if title is None:
            title = f"Load Test Dashboard {random_string(8)}"
        if slug is None:
            slug = f"load-test-{random_string(8)}"

        payload = {
            "dashboard_title": title,
            "slug": slug,
            "published": False,
        }

        with MetricsTimer("dashboard.create"):
            return self.client.post(
                "/api/v1/dashboard/", name="POST /api/v1/dashboard", json_data=payload
            )

    def update_dashboard(
        self, dashboard_id: int, updates: dict[str, Any]
    ) -> dict | None:
        """
        Scenario: Update dashboard
        """
        with MetricsTimer("dashboard.update"):
            return self.client.put(
                f"/api/v1/dashboard/{dashboard_id}",
                name="PUT /api/v1/dashboard/<id>",
                json_data=updates,
            )

    def delete_dashboard(self, dashboard_id: int) -> dict | None:
        """
        Scenario: Delete dashboard
        """
        with MetricsTimer("dashboard.delete"):
            return self.client.delete(
                f"/api/v1/dashboard/{dashboard_id}",
                name="DELETE /api/v1/dashboard/<id>",
            )

    def copy_dashboard(
        self, dashboard_id: int, new_title: str | None = None
    ) -> dict | None:
        """
        Scenario: Copy/duplicate dashboard
        """
        if new_title is None:
            new_title = f"Copy of Dashboard {random_string(6)}"

        payload = {"dashboard_title": new_title, "duplicate_slices": True}

        with MetricsTimer("dashboard.copy"):
            return self.client.post(
                f"/api/v1/dashboard/{dashboard_id}/copy/",
                name="POST /api/v1/dashboard/<id>/copy",
                json_data=payload,
            )

    def get_dashboard_thumbnail(self, dashboard_id: int | None = None) -> bytes | None:
        """
        Scenario: Get dashboard thumbnail
        """
        if dashboard_id is None:
            dashboard_id = self._get_random_dashboard_id()

        if dashboard_id is None:
            return None

        with MetricsTimer("dashboard.thumbnail"):
            return self.client.get(
                f"/api/v1/dashboard/{dashboard_id}/thumbnail/",
                name="GET /api/v1/dashboard/<id>/thumbnail",
            )

    def get_filter_state(self, dashboard_id: int, key: str) -> dict | None:
        """
        Scenario: Get dashboard filter state
        """
        with MetricsTimer("dashboard.filter_state"):
            return self.client.get(
                f"/api/v1/dashboard/{dashboard_id}/filter_state/{key}",
                name="GET /api/v1/dashboard/<id>/filter_state/<key>",
            )

    def save_filter_state(self, dashboard_id: int, filter_state: dict) -> dict | None:
        """
        Scenario: Save dashboard filter state
        """
        with MetricsTimer("dashboard.save_filter_state"):
            return self.client.post(
                f"/api/v1/dashboard/{dashboard_id}/filter_state",
                name="POST /api/v1/dashboard/<id>/filter_state",
                json_data={"value": json.dumps(filter_state)},
            )

    def get_embedded_dashboard(self, uuid: str) -> dict | None:
        """
        Scenario: Get embedded dashboard configuration
        """
        with MetricsTimer("dashboard.get_embedded"):
            return self.client.get(
                f"/api/v1/dashboard/{uuid}/embedded",
                name="GET /api/v1/dashboard/<uuid>/embedded",
            )

    def search_dashboards(self, query: str) -> dict | None:
        """
        Scenario: Search dashboards by title
        """
        filters = [{"col": "dashboard_title", "opr": "ct", "value": query}]
        with MetricsTimer("dashboard.search"):
            return self.list_dashboards(filters=filters)

    def filter_by_owner(self, user_id: int) -> dict | None:
        """
        Scenario: Filter dashboards by owner
        """
        filters = [{"col": "owners", "opr": "rel_m_m", "value": [user_id]}]
        with MetricsTimer("dashboard.filter_by_owner"):
            return self.list_dashboards(filters=filters)

    def filter_by_tag(self, tag_name: str) -> dict | None:
        """
        Scenario: Filter dashboards by tag
        """
        filters = [{"col": "tags", "opr": "dashboard_tags", "value": tag_name}]
        with MetricsTimer("dashboard.filter_by_tag"):
            return self.list_dashboards(filters=filters)
