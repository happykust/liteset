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
Chart and chart data scenarios for load testing.
These are the most performance-critical scenarios.
"""

import json
import logging
from typing import Any, TYPE_CHECKING

from ..utils.helpers import (
    build_pivot_query_context,
    build_query_context,
    build_simple_query_context,
    build_timeseries_query_context,
    random_choice,
    random_granularity,
    random_row_limit,
    random_string,
    random_time_range,
    random_viz_type,
)
from ..utils.metrics import get_metrics_collector, MetricsTimer

if TYPE_CHECKING:
    from ..utils.api_client import SupersetAPIClient

logger = logging.getLogger(__name__)


class ChartScenarios:
    """Chart-related load testing scenarios."""

    def __init__(self, client: "SupersetAPIClient"):
        self.client = client
        self.metrics = get_metrics_collector()
        self._chart_cache: list[dict] = []
        self._dataset_cache: list[dict] = []

    def _refresh_chart_cache(self) -> None:
        """Refresh local cache of charts."""
        result = self.client.get_charts(page_size=100)
        if result and "result" in result:
            self._chart_cache = result["result"]

    def _refresh_dataset_cache(self) -> None:
        """Refresh local cache of datasets."""
        result = self.client.get_datasets(page_size=100)
        if result and "result" in result:
            self._dataset_cache = result["result"]

    def _get_random_chart(self) -> dict | None:
        """Get random chart from cache."""
        if not self._chart_cache:
            self._refresh_chart_cache()
        if self._chart_cache:
            return random_choice(self._chart_cache)
        return None

    def _get_random_dataset(self) -> dict | None:
        """Get random dataset from cache."""
        if not self._dataset_cache:
            self._refresh_dataset_cache()
        if self._dataset_cache:
            return random_choice(self._dataset_cache)
        return None

    def list_charts(
        self, page: int = 0, page_size: int = 25, filters: list | None = None
    ) -> dict | None:
        """
        Scenario: List charts with pagination
        """
        with MetricsTimer("chart.list"):
            result = self.client.get_charts(
                page=page, page_size=page_size, filters=filters
            )
            if result and "result" in result:
                self._chart_cache = result["result"]
            return result

    def get_chart(self, chart_id: int | None = None) -> dict | None:
        """
        Scenario: Get single chart metadata
        """
        if chart_id is None:
            chart = self._get_random_chart()
            chart_id = chart.get("id") if chart else None

        if chart_id is None:
            return None

        with MetricsTimer("chart.get"):
            return self.client.get_chart(chart_id)

    def get_chart_data_simple(
        self,
        datasource_id: int | None = None,
        columns: list[str] | None = None,
        row_limit: int = 1000,
        force: bool = False,
    ) -> dict | None:
        """
        Scenario: Simple chart data query
        Fetches raw data with minimal transformations.
        """
        if datasource_id is None:
            dataset = self._get_random_dataset()
            datasource_id = dataset.get("id") if dataset else None

        if datasource_id is None:
            return None

        if columns is None:
            columns = ["*"]  # Select all

        query_context = build_simple_query_context(
            datasource_id=datasource_id,
            columns=columns,
            row_limit=row_limit,
            force=force,
        )

        with MetricsTimer("chart.data_simple", {"datasource_id": str(datasource_id)}):
            result = self.client.get_chart_data(query_context)
            if result:
                is_cached = result.get("result", [{}])[0].get("is_cached", False)
                self.metrics.record_cache_hit(is_cached)
            return result

    def get_chart_data_aggregated(
        self,
        datasource_id: int | None = None,
        groupby: list[str] | None = None,
        metrics: list[str | dict] | None = None,
        filters: list[dict] | None = None,
        time_range: str = "Last month",
        row_limit: int = 10000,
        force: bool = False,
    ) -> dict | None:
        """
        Scenario: Aggregated chart data query
        Complex query with GROUP BY and metrics.
        """
        if datasource_id is None:
            dataset = self._get_random_dataset()
            datasource_id = dataset.get("id") if dataset else None

        if datasource_id is None:
            return None

        if metrics is None:
            metrics = [{"expressionType": "SQL", "sqlExpression": "COUNT(*)"}]

        query_context = build_query_context(
            datasource_id=datasource_id,
            groupby=groupby,
            metrics=metrics,
            filters=filters,
            time_range=time_range,
            row_limit=row_limit,
            force=force,
        )

        with MetricsTimer(
            "chart.data_aggregated", {"datasource_id": str(datasource_id)}
        ):
            result = self.client.get_chart_data(query_context)
            if result:
                is_cached = result.get("result", [{}])[0].get("is_cached", False)
                self.metrics.record_cache_hit(is_cached)
            return result

    def get_chart_data_timeseries(
        self,
        datasource_id: int | None = None,
        time_column: str | None = None,
        metric: str | dict | None = None,
        groupby: list[str] | None = None,
        time_range: str | None = None,
        time_grain: str | None = None,
        force: bool = False,
    ) -> dict | None:
        """
        Scenario: Timeseries chart data query
        Critical for line/bar charts with time dimension.
        """
        if datasource_id is None:
            dataset = self._get_random_dataset()
            datasource_id = dataset.get("id") if dataset else None

        if datasource_id is None:
            return None

        if time_column is None:
            time_column = "ds"  # Common default

        if metric is None:
            metric = {"expressionType": "SQL", "sqlExpression": "COUNT(*)"}

        if time_range is None:
            time_range = random_time_range()

        if time_grain is None:
            time_grain = random_granularity()

        query_context = build_timeseries_query_context(
            datasource_id=datasource_id,
            time_column=time_column,
            metric=metric,
            time_range=time_range,
            time_grain=time_grain,
            groupby=groupby,
            force=force,
        )

        with MetricsTimer(
            "chart.data_timeseries", {"datasource_id": str(datasource_id)}
        ):
            result = self.client.get_chart_data(query_context)
            if result:
                is_cached = result.get("result", [{}])[0].get("is_cached", False)
                self.metrics.record_cache_hit(is_cached)
            return result

    def get_chart_data_pivot(
        self,
        datasource_id: int | None = None,
        groupby_rows: list[str] | None = None,
        groupby_cols: list[str] | None = None,
        metrics: list[str | dict] | None = None,
        time_range: str = "Last quarter",
        force: bool = False,
    ) -> dict | None:
        """
        Scenario: Pivot table data query
        Heavy query with multiple dimensions and aggregations.
        """
        if datasource_id is None:
            dataset = self._get_random_dataset()
            datasource_id = dataset.get("id") if dataset else None

        if datasource_id is None:
            return None

        if groupby_rows is None:
            groupby_rows = ["category"]

        if groupby_cols is None:
            groupby_cols = ["region"]

        if metrics is None:
            metrics = [
                {"expressionType": "SQL", "sqlExpression": "SUM(amount)"},
                {"expressionType": "SQL", "sqlExpression": "COUNT(*)"},
            ]

        query_context = build_pivot_query_context(
            datasource_id=datasource_id,
            groupby_rows=groupby_rows,
            groupby_cols=groupby_cols,
            metrics=metrics,
            time_range=time_range,
            force=force,
        )

        with MetricsTimer("chart.data_pivot", {"datasource_id": str(datasource_id)}):
            result = self.client.get_chart_data(query_context)
            if result:
                is_cached = result.get("result", [{}])[0].get("is_cached", False)
                self.metrics.record_cache_hit(is_cached)
            return result

    def get_chart_data_complex(
        self, datasource_id: int | None = None, force: bool = False
    ) -> dict | None:
        """
        Scenario: Complex chart data query
        Multiple metrics, filters, and dimensions.
        """
        if datasource_id is None:
            dataset = self._get_random_dataset()
            datasource_id = dataset.get("id") if dataset else None

        if datasource_id is None:
            return None

        # Build complex query with multiple features
        query_context = {
            "datasource": {"id": datasource_id, "type": "table"},
            "queries": [
                {
                    "columns": [],
                    "groupby": ["category", "region"],
                    "metrics": [
                        {"expressionType": "SQL", "sqlExpression": "SUM(amount)"},
                        {"expressionType": "SQL", "sqlExpression": "COUNT(*)"},
                        {"expressionType": "SQL", "sqlExpression": "AVG(amount)"},
                    ],
                    "filters": [{"col": "amount", "op": ">", "val": 0}],
                    "time_range": "Last year",
                    "row_limit": random_row_limit(),
                    "order_desc": True,
                    "force": force,
                    "extras": {"having": "COUNT(*) > 10", "where": "1=1"},
                }
            ],
            "result_format": "json",
            "result_type": "full",
            "force": force,
        }

        with MetricsTimer("chart.data_complex", {"datasource_id": str(datasource_id)}):
            result = self.client.get_chart_data(query_context)
            if result:
                is_cached = result.get("result", [{}])[0].get("is_cached", False)
                self.metrics.record_cache_hit(is_cached)
            return result

    def get_chart_data_cached(self, chart_id: int | None = None) -> dict | None:
        """
        Scenario: Get chart data with cache preference
        Should hit cache if data hasn't changed.
        """
        if chart_id is None:
            chart = self._get_random_chart()
            chart_id = chart.get("id") if chart else None

        if chart_id is None:
            return None

        # First get chart to get its query context
        chart_data = self.client.get_chart(chart_id)
        if not chart_data or "result" not in chart_data:
            return None

        query_context = chart_data["result"].get("query_context")
        if not query_context:
            return None

        if isinstance(query_context, str):
            query_context = json.loads(query_context)

        # Don't force refresh
        query_context["force"] = False
        for query in query_context.get("queries", []):
            query["force"] = False

        with MetricsTimer("chart.data_cached", {"chart_id": str(chart_id)}):
            result = self.client.get_chart_data(query_context)
            if result:
                is_cached = result.get("result", [{}])[0].get("is_cached", False)
                self.metrics.record_cache_hit(is_cached)
            return result

    def get_chart_data_no_cache(self, chart_id: int | None = None) -> dict | None:
        """
        Scenario: Get chart data bypassing cache
        Forces fresh data fetch from database.
        """
        if chart_id is None:
            chart = self._get_random_chart()
            chart_id = chart.get("id") if chart else None

        if chart_id is None:
            return None

        # First get chart to get its query context
        chart_data = self.client.get_chart(chart_id)
        if not chart_data or "result" not in chart_data:
            return None

        query_context = chart_data["result"].get("query_context")
        if not query_context:
            return None

        if isinstance(query_context, str):
            query_context = json.loads(query_context)

        # Force refresh
        query_context["force"] = True
        for query in query_context.get("queries", []):
            query["force"] = True

        with MetricsTimer("chart.data_no_cache", {"chart_id": str(chart_id)}):
            result = self.client.get_chart_data(query_context)
            if result:
                # Should always be cache miss
                self.metrics.record_cache_hit(False)
            return result

    def warm_up_cache(
        self, chart_ids: list[int] | None = None, dashboard_id: int | None = None
    ) -> dict | None:
        """
        Scenario: Warm up chart cache
        Pre-populate cache for faster subsequent loads.
        """
        payload: dict[str, Any] = {}
        if chart_ids:
            payload["chart_ids"] = chart_ids
        if dashboard_id:
            payload["dashboard_id"] = dashboard_id

        with MetricsTimer("chart.cache_warmup"):
            return self.client.post(
                "/api/v1/chart/warm_up_cache",
                name="POST /api/v1/chart/warm_up_cache",
                json_data=payload,
            )

    def get_chart_thumbnail(self, chart_id: int | None = None) -> bytes | None:
        """
        Scenario: Get chart thumbnail image
        """
        if chart_id is None:
            chart = self._get_random_chart()
            chart_id = chart.get("id") if chart else None

        if chart_id is None:
            return None

        with MetricsTimer("chart.thumbnail"):
            return self.client.get(
                f"/api/v1/chart/{chart_id}/thumbnail/",
                name="GET /api/v1/chart/<id>/thumbnail",
            )

    def create_chart(
        self,
        datasource_id: int,
        datasource_type: str = "table",
        slice_name: str | None = None,
        viz_type: str | None = None,
        params: dict | None = None,
    ) -> dict | None:
        """
        Scenario: Create new chart
        """
        if slice_name is None:
            slice_name = f"Load Test Chart {random_string(8)}"

        if viz_type is None:
            viz_type = random_viz_type()

        if params is None:
            params = {
                "viz_type": viz_type,
                "metrics": ["count"],
                "row_limit": 1000,
            }

        payload = {
            "slice_name": slice_name,
            "viz_type": viz_type,
            "datasource_id": datasource_id,
            "datasource_type": datasource_type,
            "params": json.dumps(params),
        }

        with MetricsTimer("chart.create"):
            return self.client.post(
                "/api/v1/chart/", name="POST /api/v1/chart", json_data=payload
            )

    def update_chart(self, chart_id: int, updates: dict[str, Any]) -> dict | None:
        """
        Scenario: Update chart
        """
        with MetricsTimer("chart.update"):
            return self.client.put(
                f"/api/v1/chart/{chart_id}",
                name="PUT /api/v1/chart/<id>",
                json_data=updates,
            )

    def delete_chart(self, chart_id: int) -> dict | None:
        """
        Scenario: Delete chart
        """
        with MetricsTimer("chart.delete"):
            return self.client.delete(
                f"/api/v1/chart/{chart_id}", name="DELETE /api/v1/chart/<id>"
            )

    def export_charts(self, chart_ids: list[int]) -> bytes | None:
        """
        Scenario: Export charts
        """
        params = {"q": json.dumps(chart_ids)}
        with MetricsTimer("chart.export"):
            return self.client.get(
                "/api/v1/chart/export/", name="GET /api/v1/chart/export", params=params
            )

    def add_to_favorites(self, chart_id: int | None = None) -> dict | None:
        """
        Scenario: Add chart to favorites
        """
        if chart_id is None:
            chart = self._get_random_chart()
            chart_id = chart.get("id") if chart else None

        if chart_id is None:
            return None

        with MetricsTimer("chart.add_favorite"):
            return self.client.add_favorite("Slice", chart_id)

    def get_viz_types(self) -> dict | None:
        """
        Scenario: Get available visualization types
        """
        with MetricsTimer("chart.viz_types"):
            return self.client.get(
                "/api/v1/chart/viz_types", name="GET /api/v1/chart/viz_types"
            )

    def search_charts(self, query: str) -> dict | None:
        """
        Scenario: Search charts by name
        """
        filters = [{"col": "slice_name", "opr": "ct", "value": query}]
        with MetricsTimer("chart.search"):
            return self.list_charts(filters=filters)
