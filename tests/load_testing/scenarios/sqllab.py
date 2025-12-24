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
SQL Lab scenarios for load testing.
Critical for testing query execution performance.
"""

import logging
import time
from typing import TYPE_CHECKING

from ..utils.helpers import random_choice, random_string, wait_for_async_query
from ..utils.metrics import get_metrics_collector, MetricsTimer

if TYPE_CHECKING:
    from ..utils.api_client import SupersetAPIClient

logger = logging.getLogger(__name__)


# Sample SQL queries for testing different complexity levels
SIMPLE_QUERIES = [
    "SELECT * FROM {table} LIMIT 100",
    "SELECT COUNT(*) FROM {table}",
    "SELECT * FROM {table} WHERE 1=1 LIMIT 1000",
    "SELECT DISTINCT {column} FROM {table} LIMIT 100",
]

MEDIUM_QUERIES = [
    """
    SELECT {column}, COUNT(*) as cnt
    FROM {table}
    GROUP BY {column}
    ORDER BY cnt DESC
    LIMIT 100
    """,
    """
    SELECT DATE({date_column}) as date, COUNT(*) as cnt
    FROM {table}
    WHERE {date_column} >= CURRENT_DATE - INTERVAL '30 days'
    GROUP BY date
    ORDER BY date
    """,
    """
    SELECT {column}, SUM({metric_column}) as total
    FROM {table}
    GROUP BY {column}
    HAVING SUM({metric_column}) > 0
    ORDER BY total DESC
    LIMIT 100
    """,
]

COMPLEX_QUERIES = [
    """
    WITH daily_stats AS (
        SELECT
            DATE({date_column}) as date,
            {column},
            COUNT(*) as cnt,
            SUM({metric_column}) as total
        FROM {table}
        WHERE {date_column} >= CURRENT_DATE - INTERVAL '90 days'
        GROUP BY date, {column}
    )
    SELECT
        date,
        {column},
        cnt,
        total,
        AVG(cnt) OVER (
            PARTITION BY {column} ORDER BY date ROWS 6 PRECEDING
        ) as rolling_avg
    FROM daily_stats
    ORDER BY date, total DESC
    """,
    """
    SELECT
        t1.{column} as category,
        COUNT(DISTINCT t1.id) as unique_count,
        SUM(t1.{metric_column}) as total_amount,
        AVG(t1.{metric_column}) as avg_amount,
        PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY t1.{metric_column}) as median
    FROM {table} t1
    WHERE t1.{date_column} BETWEEN CURRENT_DATE - INTERVAL '180 days' AND CURRENT_DATE
    GROUP BY t1.{column}
    HAVING COUNT(*) > 10
    ORDER BY total_amount DESC
    LIMIT 50
    """,
]

HEAVY_QUERIES = [
    """
    SELECT
        DATE_TRUNC('hour', {date_column}) as hour,
        {column},
        COUNT(*) as events,
        COUNT(DISTINCT user_id) as unique_users,
        SUM({metric_column}) as total,
        AVG({metric_column}) as avg_val,
        STDDEV({metric_column}) as stddev_val
    FROM {table}
    WHERE {date_column} >= CURRENT_DATE - INTERVAL '7 days'
    GROUP BY hour, {column}
    ORDER BY hour, events DESC
    """,
    """
    WITH user_activity AS (
        SELECT
            user_id,
            DATE({date_column}) as activity_date,
            COUNT(*) as actions,
            SUM({metric_column}) as total_value
        FROM {table}
        WHERE {date_column} >= CURRENT_DATE - INTERVAL '30 days'
        GROUP BY user_id, activity_date
    ),
    user_stats AS (
        SELECT
            user_id,
            COUNT(DISTINCT activity_date) as active_days,
            SUM(actions) as total_actions,
            AVG(actions) as avg_daily_actions,
            SUM(total_value) as lifetime_value
        FROM user_activity
        GROUP BY user_id
    )
    SELECT
        CASE
            WHEN active_days >= 20 THEN 'power_user'
            WHEN active_days >= 10 THEN 'regular'
            WHEN active_days >= 5 THEN 'casual'
            ELSE 'inactive'
        END as user_segment,
        COUNT(*) as user_count,
        AVG(total_actions) as avg_actions,
        AVG(lifetime_value) as avg_ltv
    FROM user_stats
    GROUP BY user_segment
    ORDER BY user_count DESC
    """,
]


class SQLLabScenarios:
    """SQL Lab load testing scenarios."""

    def __init__(self, client: "SupersetAPIClient"):
        self.client = client
        self.metrics = get_metrics_collector()
        self._database_cache: list[dict] = []
        self._table_cache: dict[int, list[str]] = {}

    def _refresh_database_cache(self) -> None:
        """Refresh local cache of databases."""
        result = self.client.get_databases(page_size=100)
        if result and "result" in result:
            self._database_cache = result["result"]

    def _get_random_database(self) -> dict | None:
        """Get random database from cache."""
        if not self._database_cache:
            self._refresh_database_cache()
        if self._database_cache:
            return random_choice(self._database_cache)
        return None

    def get_bootstrap_data(self) -> dict | None:
        """
        Scenario: Get SQL Lab bootstrap data
        Initial load when opening SQL Lab.
        """
        with MetricsTimer("sqllab.bootstrap"):
            return self.client.get("/api/v1/sqllab/", name="GET /api/v1/sqllab")

    def execute_simple_query(
        self,
        database_id: int | None = None,
        schema: str | None = None,
        table: str = "events",
        column: str = "id",
    ) -> dict | None:
        """
        Scenario: Execute simple SQL query
        Fast, lightweight query.
        """
        if database_id is None:
            db = self._get_random_database()
            database_id = db.get("id") if db else None

        if database_id is None:
            return None

        query_template = random_choice(SIMPLE_QUERIES)
        sql = query_template.format(table=table, column=column)

        with MetricsTimer("sqllab.execute_simple", {"database_id": str(database_id)}):
            result = self.client.execute_sql(
                database_id=database_id, sql=sql, schema=schema, run_async=False
            )

            if result:
                # Track database query time if available
                query_time = result.get("query", {}).get("executionTime")
                if query_time:
                    self.metrics.record_db_query_time(f"db_{database_id}", query_time)

            return result

    def execute_medium_query(
        self,
        database_id: int | None = None,
        schema: str | None = None,
        table: str = "events",
        column: str = "event_type",
        date_column: str = "timestamp",
        metric_column: str = "value",
    ) -> dict | None:
        """
        Scenario: Execute medium complexity query
        Includes GROUP BY and aggregations.
        """
        if database_id is None:
            db = self._get_random_database()
            database_id = db.get("id") if db else None

        if database_id is None:
            return None

        query_template = random_choice(MEDIUM_QUERIES)
        sql = query_template.format(
            table=table,
            column=column,
            date_column=date_column,
            metric_column=metric_column,
        )

        with MetricsTimer("sqllab.execute_medium", {"database_id": str(database_id)}):
            result = self.client.execute_sql(
                database_id=database_id, sql=sql, schema=schema, run_async=False
            )

            if result:
                query_time = result.get("query", {}).get("executionTime")
                if query_time:
                    self.metrics.record_db_query_time(f"db_{database_id}", query_time)

            return result

    def execute_complex_query(
        self,
        database_id: int | None = None,
        schema: str | None = None,
        table: str = "events",
        column: str = "event_type",
        date_column: str = "timestamp",
        metric_column: str = "value",
    ) -> dict | None:
        """
        Scenario: Execute complex SQL query
        CTEs, window functions, subqueries.
        """
        if database_id is None:
            db = self._get_random_database()
            database_id = db.get("id") if db else None

        if database_id is None:
            return None

        query_template = random_choice(COMPLEX_QUERIES)
        sql = query_template.format(
            table=table,
            column=column,
            date_column=date_column,
            metric_column=metric_column,
        )

        with MetricsTimer("sqllab.execute_complex", {"database_id": str(database_id)}):
            result = self.client.execute_sql(
                database_id=database_id, sql=sql, schema=schema, run_async=False
            )

            if result:
                query_time = result.get("query", {}).get("executionTime")
                if query_time:
                    self.metrics.record_db_query_time(f"db_{database_id}", query_time)

            return result

    def execute_heavy_query(
        self,
        database_id: int | None = None,
        schema: str | None = None,
        table: str = "events",
        column: str = "event_type",
        date_column: str = "timestamp",
        metric_column: str = "value",
    ) -> dict | None:
        """
        Scenario: Execute heavy analytical query
        Maximum complexity for stress testing.
        """
        if database_id is None:
            db = self._get_random_database()
            database_id = db.get("id") if db else None

        if database_id is None:
            return None

        query_template = random_choice(HEAVY_QUERIES)
        sql = query_template.format(
            table=table,
            column=column,
            date_column=date_column,
            metric_column=metric_column,
        )

        with MetricsTimer("sqllab.execute_heavy", {"database_id": str(database_id)}):
            result = self.client.execute_sql(
                database_id=database_id, sql=sql, schema=schema, run_async=False
            )

            if result:
                query_time = result.get("query", {}).get("executionTime")
                if query_time:
                    self.metrics.record_db_query_time(f"db_{database_id}", query_time)

            return result

    def execute_async_query(
        self,
        database_id: int | None = None,
        schema: str | None = None,
        sql: str | None = None,
        wait_for_result: bool = True,
        max_wait_seconds: int = 60,
    ) -> dict | None:
        """
        Scenario: Execute asynchronous query
        Tests Celery worker performance.
        """
        if database_id is None:
            db = self._get_random_database()
            database_id = db.get("id") if db else None

        if database_id is None:
            return None

        if sql is None:
            # Use a heavy query for async
            sql = random_choice(HEAVY_QUERIES).format(
                table="events",
                column="event_type",
                date_column="timestamp",
                metric_column="value",
            )

        self.metrics.record_async_query_start()
        start_time = time.time()

        with MetricsTimer("sqllab.execute_async_start"):
            result = self.client.execute_sql(
                database_id=database_id, sql=sql, schema=schema, run_async=True
            )

        if not result:
            return None

        query_id = result.get("query", {}).get("id")
        if not query_id:
            return result

        if wait_for_result:
            # Poll for completion
            def poll_query():
                return self.client.get(
                    f"/api/v1/query/{query_id}",
                    name="GET /api/v1/query/<id> [async poll]",
                )

            final_result = wait_for_async_query(
                poll_func=poll_query,
                max_attempts=max_wait_seconds * 2,
                poll_interval=0.5,
                timeout=max_wait_seconds,
            )

            total_time = (time.time() - start_time) * 1000
            self.metrics.record_async_query_complete(total_time)

            return final_result

        return result

    def get_query_results(self, key: str) -> dict | None:
        """
        Scenario: Fetch query results by key
        """
        with MetricsTimer("sqllab.get_results"):
            return self.client.get_sql_results(key)

    def estimate_query_cost(
        self,
        database_id: int | None = None,
        schema: str | None = None,
        sql: str | None = None,
    ) -> dict | None:
        """
        Scenario: Estimate query cost
        Tests query planning without execution.
        """
        if database_id is None:
            db = self._get_random_database()
            database_id = db.get("id") if db else None

        if database_id is None:
            return None

        if sql is None:
            sql = "SELECT * FROM events LIMIT 1000"

        payload = {
            "database_id": database_id,
            "sql": sql,
            "schema": schema,
        }

        with MetricsTimer("sqllab.estimate_cost"):
            return self.client.post(
                "/api/v1/sqllab/estimate/",
                name="POST /api/v1/sqllab/estimate",
                json_data=payload,
            )

    def format_sql(self, sql: str) -> dict | None:
        """
        Scenario: Format SQL query
        Tests SQL formatting service.
        """
        payload = {"sql": sql}

        with MetricsTimer("sqllab.format_sql"):
            return self.client.post(
                "/api/v1/sqllab/format_sql/",
                name="POST /api/v1/sqllab/format_sql",
                json_data=payload,
            )

    def export_query_results(self, client_id: str, database_id: int) -> bytes | None:
        """
        Scenario: Export query results to CSV
        """
        with MetricsTimer("sqllab.export_csv"):
            return self.client.get(
                "/api/v1/sqllab/export/",
                name="GET /api/v1/sqllab/export",
                params={"client_id": client_id, "database_id": database_id},
            )

    def get_query_history(self, page: int = 0, page_size: int = 25) -> dict | None:
        """
        Scenario: Get query history
        """
        with MetricsTimer("sqllab.query_history"):
            return self.client.get_queries(page=page, page_size=page_size)

    def get_saved_queries(self, page: int = 0, page_size: int = 25) -> dict | None:
        """
        Scenario: Get saved queries
        """
        with MetricsTimer("sqllab.saved_queries"):
            return self.client.get_saved_queries(page=page, page_size=page_size)

    def save_query(
        self,
        database_id: int,
        sql: str,
        label: str | None = None,
        schema: str | None = None,
    ) -> dict | None:
        """
        Scenario: Save query for later use
        """
        if label is None:
            label = f"Load Test Query {random_string(8)}"

        payload = {
            "db_id": database_id,
            "sql": sql,
            "label": label,
            "schema": schema,
        }

        with MetricsTimer("sqllab.save_query"):
            return self.client.post(
                "/api/v1/saved_query/",
                name="POST /api/v1/saved_query",
                json_data=payload,
            )

    def stop_query(self, client_id: str) -> dict | None:
        """
        Scenario: Stop running query
        """
        payload = {"client_id": client_id}

        with MetricsTimer("sqllab.stop_query"):
            return self.client.post(
                "/api/v1/query/stop", name="POST /api/v1/query/stop", json_data=payload
            )

    def validate_sql(
        self, database_id: int, sql: str, schema: str | None = None
    ) -> dict | None:
        """
        Scenario: Validate SQL syntax
        """
        payload = {
            "database_id": database_id,
            "sql": sql,
            "schema": schema,
        }

        with MetricsTimer("sqllab.validate_sql"):
            return self.client.post(
                "/api/v1/database/validate_sql/",
                name="POST /api/v1/database/validate_sql",
                json_data=payload,
            )

    def create_table_as(
        self,
        database_id: int,
        sql: str,
        table_name: str | None = None,
        schema: str | None = None,
    ) -> dict | None:
        """
        Scenario: Create Table As Select (CTAS)
        Tests materialization capabilities.
        """
        if table_name is None:
            table_name = f"load_test_ctas_{random_string(8)}"

        with MetricsTimer("sqllab.ctas"):
            return self.client.execute_sql(
                database_id=database_id,
                sql=sql,
                schema=schema,
                run_async=True,
                select_as_cta=True,
                ctas_method="TABLE",
                tmp_table_name=table_name,
            )

    def execute_concurrent_queries(
        self, database_id: int, num_queries: int = 5, schema: str | None = None
    ) -> list[dict]:
        """
        Scenario: Execute multiple concurrent queries
        Tests connection pool and concurrency handling.
        """
        results = []

        for i in range(num_queries):
            sql = f"SELECT {i}, COUNT(*) FROM events GROUP BY 1"
            result = self.client.execute_sql(
                database_id=database_id, sql=sql, schema=schema, run_async=True
            )
            if result:
                results.append(result)

        return results
