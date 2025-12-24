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
Mixed workflow scenarios for load testing.
Simulates realistic user behavior patterns.
"""

import logging
import time
from typing import Any, TYPE_CHECKING

from ..utils.helpers import random_choice
from ..utils.metrics import get_metrics_collector, MetricsTimer

if TYPE_CHECKING:
    from ..utils.api_client import SupersetAPIClient

logger = logging.getLogger(__name__)


class MixedWorkflowScenarios:
    """
    Mixed workflow scenarios that simulate realistic user behavior.
    These combine multiple API calls into cohesive user journeys.
    """

    def __init__(self, client: "SupersetAPIClient"):
        self.client = client
        self.metrics = get_metrics_collector()
        self._dashboard_ids: list[int] = []
        self._chart_ids: list[int] = []
        self._dataset_ids: list[int] = []
        self._database_ids: list[int] = []

    def _ensure_data_cached(self) -> None:
        """Ensure we have cached IDs for testing."""
        if not self._dashboard_ids:
            result = self.client.get_dashboards(page_size=50)
            if result and "result" in result:
                self._dashboard_ids = [d["id"] for d in result["result"]]

        if not self._chart_ids:
            result = self.client.get_charts(page_size=50)
            if result and "result" in result:
                self._chart_ids = [c["id"] for c in result["result"]]

        if not self._dataset_ids:
            result = self.client.get_datasets(page_size=50)
            if result and "result" in result:
                self._dataset_ids = [d["id"] for d in result["result"]]

        if not self._database_ids:
            result = self.client.get_databases(page_size=20)
            if result and "result" in result:
                self._database_ids = [d["id"] for d in result["result"]]

    def analyst_workflow(self) -> dict:
        """
        Scenario: Business Analyst Workflow
        - Open dashboard list
        - Select and view a dashboard
        - Apply filters
        - View chart details
        - Export data
        """
        self._ensure_data_cached()
        results: dict[str, Any] = {
            "dashboard_list": None,
            "dashboard_view": None,
            "charts": None,
            "chart_data": [],
            "export": None,
        }

        with MetricsTimer("workflow.analyst"):
            # Step 1: Browse dashboards
            results["dashboard_list"] = self.client.get_dashboards(page_size=25)
            time.sleep(0.5)

            # Step 2: Open a dashboard
            if self._dashboard_ids:
                dashboard_id = random_choice(self._dashboard_ids)
                results["dashboard_view"] = self.client.get_dashboard(dashboard_id)
                time.sleep(0.3)

                # Step 3: Get charts on dashboard
                charts_result = self.client.get(
                    f"/api/v1/dashboard/{dashboard_id}/charts",
                    name="GET /api/v1/dashboard/<id>/charts",
                )
                results["charts"] = charts_result
                time.sleep(0.2)

                # Step 4: Load data for each chart (simulating dashboard render)
                if charts_result and "result" in charts_result:
                    for chart in charts_result["result"][:5]:  # Limit to 5 charts
                        chart_id = chart.get("id")
                        if chart_id:
                            data = self.client.get_chart(chart_id)
                            if data:
                                results["chart_data"].append(data)
                            time.sleep(0.1)

                # Step 5: Export dashboard
                results["export"] = self.client.get(
                    "/api/v1/dashboard/export/",
                    name="GET /api/v1/dashboard/export",
                    params={"q": f"[{dashboard_id}]"},
                )

        return results

    def data_engineer_workflow(self) -> dict:
        """
        Scenario: Data Engineer Workflow
        - Check databases
        - Browse schemas and tables
        - Run SQL queries
        - Create dataset from query
        - Build a chart
        """
        self._ensure_data_cached()
        results: dict[str, Any] = {
            "databases": None,
            "schemas": None,
            "tables": None,
            "query_result": None,
            "dataset_created": None,
        }

        with MetricsTimer("workflow.data_engineer"):
            # Step 1: List databases
            results["databases"] = self.client.get_databases()
            time.sleep(0.3)

            if self._database_ids:
                db_id = random_choice(self._database_ids)

                # Step 2: Get schemas
                results["schemas"] = self.client.get_database_schemas(db_id)
                time.sleep(0.2)

                # Step 3: Get tables
                results["tables"] = self.client.get_database_tables(
                    db_id, "public", force_refresh=False
                )
                time.sleep(0.2)

                # Step 4: Execute SQL query
                sql = "SELECT COUNT(*) as cnt FROM events LIMIT 1"
                results["query_result"] = self.client.execute_sql(
                    database_id=db_id, sql=sql, run_async=False
                )
                time.sleep(0.5)

        return results

    def viewer_workflow(self) -> dict:
        """
        Scenario: Viewer/Consumer Workflow (Read-only)
        - Browse dashboards
        - View multiple dashboards
        - Check favorites
        - View charts
        """
        self._ensure_data_cached()
        results: dict[str, Any] = {
            "dashboards_viewed": [],
            "charts_viewed": [],
            "favorites_checked": None,
        }

        with MetricsTimer("workflow.viewer"):
            # Step 1: Get dashboard list
            self.client.get_dashboards(page_size=25)
            time.sleep(0.3)

            # Step 2: View 3 random dashboards
            if self._dashboard_ids:
                for _ in range(min(3, len(self._dashboard_ids))):
                    dashboard_id = random_choice(self._dashboard_ids)
                    dashboard = self.client.get_dashboard(dashboard_id)
                    if dashboard:
                        results["dashboards_viewed"].append(dashboard)
                    time.sleep(0.5)

            # Step 3: Check favorite status
            if self._dashboard_ids:
                ids_to_check = self._dashboard_ids[:10]
                results["favorites_checked"] = self.client.get(
                    "/api/v1/dashboard/favorite_status/",
                    name="GET /api/v1/dashboard/favorite_status",
                    params={"q": str(ids_to_check)},
                )

            # Step 4: View some charts
            if self._chart_ids:
                for _ in range(min(5, len(self._chart_ids))):
                    chart_id = random_choice(self._chart_ids)
                    chart = self.client.get_chart(chart_id)
                    if chart:
                        results["charts_viewed"].append(chart)
                    time.sleep(0.2)

        return results

    def power_user_workflow(self) -> dict:
        """
        Scenario: Power User Workflow
        - SQL Lab queries
        - Create charts
        - Modify dashboards
        - Heavy data exploration
        """
        self._ensure_data_cached()
        results: dict[str, Any] = {
            "queries_executed": [],
            "explore_results": [],
            "chart_created": None,
        }

        with MetricsTimer("workflow.power_user"):
            # Step 1: Execute multiple SQL queries
            if self._database_ids:
                db_id = random_choice(self._database_ids)

                queries = [
                    "SELECT COUNT(*) FROM events",
                    "SELECT event_type, COUNT(*) FROM events GROUP BY 1 LIMIT 10",
                    "SELECT DATE(timestamp), COUNT(*) FROM events GROUP BY 1 LIMIT 30",
                ]

                for sql in queries:
                    result = self.client.execute_sql(
                        database_id=db_id, sql=sql, run_async=False
                    )
                    if result:
                        results["queries_executed"].append(result)
                    time.sleep(0.3)

            # Step 2: Explore datasets
            if self._dataset_ids:
                for _ in range(3):
                    ds_id = random_choice(self._dataset_ids)
                    # Get samples
                    samples = self.client.post(
                        "/api/v1/datasource/samples",
                        name="POST /api/v1/datasource/samples",
                        json_data={
                            "datasource": {"id": ds_id, "type": "table"},
                            "force": False,
                        },
                    )
                    if samples:
                        results["explore_results"].append(samples)
                    time.sleep(0.2)

        return results

    def dashboard_heavy_load(self) -> dict:
        """
        Scenario: Heavy Dashboard Load
        - Load dashboard with many charts
        - Force refresh all data
        - Simulate concurrent chart data requests
        """
        self._ensure_data_cached()
        results: dict[str, Any] = {
            "dashboard": None,
            "chart_data_results": [],
            "total_charts": 0,
        }

        with MetricsTimer("workflow.dashboard_heavy"):
            if not self._dashboard_ids:
                return results

            dashboard_id = random_choice(self._dashboard_ids)

            # Get dashboard
            results["dashboard"] = self.client.get_dashboard(dashboard_id)

            # Get all charts
            charts_result = self.client.get(
                f"/api/v1/dashboard/{dashboard_id}/charts",
                name="GET /api/v1/dashboard/<id>/charts",
            )

            if charts_result and "result" in charts_result:
                charts = charts_result["result"]
                results["total_charts"] = len(charts)

                # Load data for all charts with force refresh
                for chart in charts:
                    chart_id = chart.get("id")
                    if chart_id:
                        # Get chart with query context
                        chart_detail = self.client.get_chart(chart_id)
                        if chart_detail and "result" in chart_detail:
                            query_context = chart_detail["result"].get("query_context")
                            if query_context:
                                import json

                                if isinstance(query_context, str):
                                    query_context = json.loads(query_context)
                                query_context["force"] = True
                                data = self.client.get_chart_data(query_context)
                                if data:
                                    results["chart_data_results"].append(data)

        return results

    def sqllab_intensive(self) -> dict:
        """
        Scenario: SQL Lab Intensive Usage
        - Multiple concurrent queries
        - Mix of sync and async
        - Query history checks
        """
        self._ensure_data_cached()
        results: dict[str, Any] = {
            "sync_queries": [],
            "async_queries": [],
            "query_history": None,
        }

        with MetricsTimer("workflow.sqllab_intensive"):
            if not self._database_ids:
                return results

            db_id = random_choice(self._database_ids)

            # Sync queries
            sync_sqls = [
                "SELECT 1",
                "SELECT COUNT(*) FROM events",
                "SELECT * FROM events LIMIT 100",
            ]

            for sql in sync_sqls:
                result = self.client.execute_sql(
                    database_id=db_id, sql=sql, run_async=False
                )
                if result:
                    results["sync_queries"].append(result)
                time.sleep(0.1)

            # Async queries
            async_sqls = [
                """
                SELECT event_type, COUNT(*) as cnt
                FROM events
                GROUP BY event_type
                ORDER BY cnt DESC
                """,
                """
                SELECT DATE(timestamp) as dt, COUNT(*)
                FROM events
                WHERE timestamp >= CURRENT_DATE - INTERVAL '30 days'
                GROUP BY dt
                ORDER BY dt
                """,
            ]

            for sql in async_sqls:
                result = self.client.execute_sql(
                    database_id=db_id, sql=sql, run_async=True
                )
                if result:
                    results["async_queries"].append(result)
                time.sleep(0.2)

            # Check query history
            results["query_history"] = self.client.get_queries(page_size=20)

        return results

    def api_stress_test(self) -> dict:
        """
        Scenario: API Stress Test
        - Rapid sequential API calls
        - Tests rate limiting and connection handling
        """
        results: dict[str, Any] = {
            "calls_made": 0,
            "successful": 0,
            "failed": 0,
        }

        with MetricsTimer("workflow.api_stress"):
            endpoints = [
                "/api/v1/dashboard/",
                "/api/v1/chart/",
                "/api/v1/dataset/",
                "/api/v1/database/",
                "/api/v1/query/",
                "/api/v1/saved_query/",
            ]

            for _ in range(20):
                endpoint = random_choice(endpoints)
                result = self.client.get(endpoint, name=f"GET {endpoint}")
                results["calls_made"] += 1
                if result:
                    results["successful"] += 1
                else:
                    results["failed"] += 1
                time.sleep(0.05)

        return results

    def cache_effectiveness_test(self) -> dict:
        """
        Scenario: Cache Effectiveness Test
        - Same queries repeated
        - Measures cache hit rate
        """
        self._ensure_data_cached()
        results: dict[str, Any] = {
            "first_pass": [],
            "second_pass": [],
            "cache_hits": 0,
            "cache_misses": 0,
        }

        with MetricsTimer("workflow.cache_test"):
            if not self._dataset_ids:
                return results

            ds_id = random_choice(self._dataset_ids)

            query_context = {
                "datasource": {"id": ds_id, "type": "table"},
                "queries": [
                    {
                        "metrics": [
                            {"expressionType": "SQL", "sqlExpression": "COUNT(*)"}
                        ],
                        "groupby": [],
                        "time_range": "Last week",
                        "row_limit": 1000,
                        "force": False,
                    }
                ],
                "result_format": "json",
                "result_type": "full",
                "force": False,
            }

            # First pass - likely cache miss
            for _ in range(3):
                result = self.client.get_chart_data(query_context)
                if result:
                    results["first_pass"].append(result)
                    is_cached = result.get("result", [{}])[0].get("is_cached", False)
                    if is_cached:
                        results["cache_hits"] += 1
                    else:
                        results["cache_misses"] += 1
                time.sleep(0.1)

            # Second pass - should hit cache
            for _ in range(3):
                result = self.client.get_chart_data(query_context)
                if result:
                    results["second_pass"].append(result)
                    is_cached = result.get("result", [{}])[0].get("is_cached", False)
                    if is_cached:
                        results["cache_hits"] += 1
                    else:
                        results["cache_misses"] += 1
                time.sleep(0.1)

        return results

    def full_user_session(self) -> dict:
        """
        Scenario: Full User Session
        - Complete realistic user session
        - From login to logout
        """
        results: dict[str, Any] = {
            "login": False,
            "csrf": None,
            "user_info": None,
            "dashboard_work": None,
            "sqllab_work": None,
        }

        with MetricsTimer("workflow.full_session"):
            # CSRF token (already logged in via Locust)
            results["csrf"] = self.client.refresh_csrf()

            # Get user info
            results["user_info"] = self.client.get("/api/v1/me/", name="GET /api/v1/me")
            time.sleep(0.2)

            # Do some dashboard work
            results["dashboard_work"] = self.viewer_workflow()
            time.sleep(0.5)

            # Do some SQL Lab work
            results["sqllab_work"] = self.sqllab_intensive()

        return results
