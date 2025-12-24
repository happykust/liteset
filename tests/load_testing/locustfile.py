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
Main Locust load testing file for Apache Superset.

Usage:
    # Run with web UI
    locust -f locustfile.py --host=http://localhost:8088

    # Run headless
    locust -f locustfile.py --host=http://localhost:8088 \
        --headless -u 100 -r 10 -t 30m

    # Run distributed (master)
    locust -f locustfile.py --master --host=http://localhost:8088

    # Run distributed (worker)
    locust -f locustfile.py --worker --master-host=<master-ip>

Environment Variables:
    SUPERSET_URL        - Superset base URL (default: http://localhost:8088)
    SUPERSET_USERNAME   - Login username (default: admin)
    SUPERSET_PASSWORD   - Login password (default: admin)
    LOAD_PROFILE        - Load profile: smoke, load, stress, spike, soak
    CACHE_MODE          - Cache mode: enabled, disabled, mixed
"""

import logging
import os
import random

from config import CacheMode, get_settings
from locust import between, events, HttpUser, task
from locust.runners import MasterRunner, WorkerRunner
from scenarios import (
    AuthScenarios,
    ChartScenarios,
    DashboardScenarios,
    DatabaseScenarios,
    DatasetScenarios,
    ExploreScenarios,
    MixedWorkflowScenarios,
    SQLLabScenarios,
)
from utils import get_metrics_collector, SupersetAPIClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Global settings
settings = get_settings()


@events.init.add_listener
def on_locust_init(environment, **kwargs):
    """Initialize load test environment."""
    if isinstance(environment.runner, MasterRunner):
        logger.info("Running as master node")
    elif isinstance(environment.runner, WorkerRunner):
        logger.info("Running as worker node")
    else:
        logger.info("Running as standalone node")

    logger.info(f"Load profile: {settings.profile.value}")
    logger.info(f"Cache mode: {settings.cache_mode.value}")
    logger.info(f"Target: {settings.superset.base_url}")


@events.test_stop.add_listener
def on_test_stop(environment, **kwargs):
    """Export metrics when test stops."""
    collector = get_metrics_collector()
    report = collector.get_report()

    logger.info("=" * 60)
    logger.info("LOAD TEST SUMMARY")
    logger.info("=" * 60)
    logger.info(f"Duration: {report['duration_seconds']:.1f}s")
    logger.info(f"Error Rate: {report['error_rate'] * 100:.2f}%")
    logger.info(f"Cache Hit Ratio: {report['cache']['hit_ratio'] * 100:.2f}%")

    if report.get("async_queries", {}).get("completed", 0) > 0:
        logger.info(
            f"Async Queries: {report['async_queries']['completed']} "
            f"(avg: {report['async_queries']['avg_time_ms']:.0f}ms)"
        )

    # Export to files
    try:
        collector.export_to_json()
        collector.export_to_csv()
        logger.info("Metrics exported to ./metrics_output/")
    except Exception as e:
        logger.error(f"Failed to export metrics: {e}")


class SupersetUser(HttpUser):
    """
    Base Superset user that handles authentication and provides
    access to all scenario classes.
    """

    # Wait between tasks (simulates user think time)
    wait_time = between(1, 5)

    # Abstract - subclasses define weight
    abstract = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.api_client: SupersetAPIClient | None = None
        self.auth: AuthScenarios | None = None
        self.dashboards: DashboardScenarios | None = None
        self.charts: ChartScenarios | None = None
        self.sqllab: SQLLabScenarios | None = None
        self.explore: ExploreScenarios | None = None
        self.datasets: DatasetScenarios | None = None
        self.databases: DatabaseScenarios | None = None
        self.workflows: MixedWorkflowScenarios | None = None

    def on_start(self):
        """Called when a simulated user starts."""
        # Initialize API client
        self.api_client = SupersetAPIClient(self, settings.superset.base_url)

        # Login
        success = self.api_client.login(
            settings.superset.username, settings.superset.password
        )

        if not success:
            logger.error("Failed to login, trying API login")
            success = self.api_client.login_api(
                settings.superset.username, settings.superset.password
            )

        if not success:
            logger.error("Authentication failed!")
            return

        # Initialize scenario classes
        self.auth = AuthScenarios(self.api_client)
        self.dashboards = DashboardScenarios(self.api_client)
        self.charts = ChartScenarios(self.api_client)
        self.sqllab = SQLLabScenarios(self.api_client)
        self.explore = ExploreScenarios(self.api_client)
        self.datasets = DatasetScenarios(self.api_client)
        self.databases = DatabaseScenarios(self.api_client)
        self.workflows = MixedWorkflowScenarios(self.api_client)

    def on_stop(self):
        """Called when a simulated user stops."""
        pass

    def should_use_cache(self) -> bool:
        """Determine if this request should use cache based on cache mode."""
        if settings.cache_mode == CacheMode.ENABLED:
            return True
        elif settings.cache_mode == CacheMode.DISABLED:
            return False
        else:  # MIXED
            return random.random() > 0.3  # 70% cached, 30% fresh


class DashboardViewerUser(SupersetUser):
    """
    User that primarily views dashboards.
    Represents typical business user behavior.
    """

    weight = 40  # Most common user type

    @task(20)
    def view_dashboard(self):
        """View a dashboard and load its charts."""
        if self.dashboards:
            self.dashboards.view_dashboard_with_charts()

    @task(15)
    def list_dashboards(self):
        """Browse dashboard list."""
        if self.dashboards:
            self.dashboards.list_dashboards()

    @task(10)
    def load_dashboard_data(self):
        """Load all chart data for a dashboard."""
        if self.dashboards:
            force = not self.should_use_cache()
            self.dashboards.load_dashboard_chart_data(force_refresh=force)

    @task(5)
    def check_favorites(self):
        """Check favorite dashboards."""
        if self.dashboards and self.dashboards._dashboard_cache:
            ids = [d["id"] for d in self.dashboards._dashboard_cache[:10]]
            self.dashboards.get_favorite_status(ids)

    @task(3)
    def search_dashboards(self):
        """Search for dashboards."""
        if self.dashboards:
            self.dashboards.search_dashboards("sales")


class ChartAnalystUser(SupersetUser):
    """
    User that focuses on chart analysis and data exploration.
    """

    weight = 25

    @task(20)
    def get_chart_data_timeseries(self):
        """Fetch timeseries chart data."""
        if self.charts:
            self.charts.get_chart_data_timeseries(force=not self.should_use_cache())

    @task(15)
    def get_chart_data_aggregated(self):
        """Fetch aggregated chart data."""
        if self.charts:
            self.charts.get_chart_data_aggregated(force=not self.should_use_cache())

    @task(10)
    def get_chart_data_pivot(self):
        """Fetch pivot table data."""
        if self.charts:
            self.charts.get_chart_data_pivot(force=not self.should_use_cache())

    @task(10)
    def list_charts(self):
        """Browse chart list."""
        if self.charts:
            self.charts.list_charts()

    @task(5)
    def get_chart_data_complex(self):
        """Execute complex chart query."""
        if self.charts:
            self.charts.get_chart_data_complex(force=not self.should_use_cache())

    @task(3)
    def explore_dataset(self):
        """Explore a dataset."""
        if self.explore:
            self.explore.explore_chart_workflow()


class SQLLabUser(SupersetUser):
    """
    User that primarily uses SQL Lab for ad-hoc queries.
    """

    weight = 20

    @task(15)
    def execute_simple_query(self):
        """Run simple SQL query."""
        if self.sqllab:
            self.sqllab.execute_simple_query()

    @task(10)
    def execute_medium_query(self):
        """Run medium complexity query."""
        if self.sqllab:
            self.sqllab.execute_medium_query()

    @task(8)
    def execute_complex_query(self):
        """Run complex query with CTEs."""
        if self.sqllab:
            self.sqllab.execute_complex_query()

    @task(5)
    def execute_async_query(self):
        """Run async query."""
        if self.sqllab:
            self.sqllab.execute_async_query(wait_for_result=True, max_wait_seconds=30)

    @task(5)
    def get_bootstrap(self):
        """Load SQL Lab bootstrap data."""
        if self.sqllab:
            self.sqllab.get_bootstrap_data()

    @task(3)
    def browse_tables(self):
        """Browse database tables."""
        if self.databases:
            self.databases.browse_schema()

    @task(2)
    def get_query_history(self):
        """Check query history."""
        if self.sqllab:
            self.sqllab.get_query_history()


class PowerUser(SupersetUser):
    """
    Power user that does everything - dashboards, charts, SQL Lab.
    """

    weight = 10

    @task(10)
    def analyst_workflow(self):
        """Full analyst workflow."""
        if self.workflows:
            self.workflows.analyst_workflow()

    @task(8)
    def power_user_workflow(self):
        """Power user workflow."""
        if self.workflows:
            self.workflows.power_user_workflow()

    @task(5)
    def dashboard_heavy_load(self):
        """Heavy dashboard load."""
        if self.workflows:
            self.workflows.dashboard_heavy_load()

    @task(5)
    def sqllab_intensive(self):
        """Intensive SQL Lab usage."""
        if self.workflows:
            self.workflows.sqllab_intensive()

    @task(3)
    def cache_test(self):
        """Test cache effectiveness."""
        if self.workflows:
            self.workflows.cache_effectiveness_test()

    @task(2)
    def create_chart(self):
        """Create a new chart."""
        if self.charts and self.datasets and self.datasets._dataset_cache:
            ds = random.choice(self.datasets._dataset_cache)
            self.charts.create_chart(datasource_id=ds["id"])

    @task(1)
    def create_dashboard(self):
        """Create a new dashboard."""
        if self.dashboards:
            self.dashboards.create_dashboard()


class APIStressUser(SupersetUser):
    """
    User for API stress testing - rapid API calls.
    """

    weight = 5

    wait_time = between(0.1, 0.5)  # Very fast

    @task(20)
    def rapid_api_calls(self):
        """Rapid API stress test."""
        if self.workflows:
            self.workflows.api_stress_test()

    @task(10)
    def list_all_resources(self):
        """List various resources."""
        if self.dashboards:
            self.dashboards.list_dashboards()
        if self.charts:
            self.charts.list_charts()
        if self.datasets:
            self.datasets.list_datasets()

    @task(5)
    def metadata_requests(self):
        """Request metadata endpoints."""
        if self.databases:
            self.databases.get_available_engines()
        if self.charts:
            self.charts.get_viz_types()


# Custom user classes for specific scenarios


class CacheTestUser(SupersetUser):
    """User specifically for testing cache behavior."""

    weight = 0  # Disabled by default

    @task
    def cache_warm_cold(self):
        """Test cache warm/cold scenarios."""
        if self.workflows:
            self.workflows.cache_effectiveness_test()


class AsyncQueryUser(SupersetUser):
    """User specifically for async query testing."""

    weight = 0  # Disabled by default

    wait_time = between(2, 5)

    @task
    def heavy_async_queries(self):
        """Execute heavy async queries."""
        if self.sqllab:
            self.sqllab.execute_heavy_query()
            self.sqllab.execute_async_query(wait_for_result=True)


# Utility functions for custom test scenarios


def run_smoke_test():
    """Quick smoke test configuration."""
    os.environ["LOAD_PROFILE"] = "smoke"
    logger.info("Running smoke test: 10 users, 5 minutes")


def run_load_test():
    """Standard load test configuration."""
    os.environ["LOAD_PROFILE"] = "load"
    logger.info("Running load test: 100 users, 30 minutes")


def run_stress_test():
    """Stress test configuration."""
    os.environ["LOAD_PROFILE"] = "stress"
    logger.info("Running stress test: 500 users, 15 minutes")


def run_spike_test():
    """Spike test configuration."""
    os.environ["LOAD_PROFILE"] = "spike"
    logger.info("Running spike test: 1000 users, 5 minutes")


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        profile = sys.argv[1]
        if profile == "smoke":
            run_smoke_test()
        elif profile == "load":
            run_load_test()
        elif profile == "stress":
            run_stress_test()
        elif profile == "spike":
            run_spike_test()
