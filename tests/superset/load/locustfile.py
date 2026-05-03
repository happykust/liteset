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
"""Locust load test scenarios for Superset.

Scenarios:
A. Single user — baseline latency
B. Parallel load — RPS under 50 concurrent users
C. Scaling — degradation point (10 -> 50 -> 100 -> 200 users)
D. Dashboard simulation — 50 parallel chart/data requests
E. Long queries — async IO-bound advantage
"""

from __future__ import annotations

from locust import between, HttpUser, tag, task

CHART_DATA_PAYLOAD = {
    "datasource": {"id": 1, "type": "table"},
    "queries": [
        {
            "columns": ["ds", "count"],
            "metrics": ["count"],
            "row_limit": 100,
        }
    ],
}


class SupersetUser(HttpUser):
    wait_time = between(0.1, 0.5)

    def on_start(self):
        self.client.post(
            "/api/v1/security/login",
            json={
                "username": "admin",
                "password": "admin",
                "provider": "db",
            },
        )

    @task(10)
    @tag("chart", "scenario-b", "scenario-d")
    def chart_data(self):
        self.client.post("/api/v1/chart/data", json=CHART_DATA_PAYLOAD)

    @task(5)
    @tag("dashboard", "scenario-b")
    def dashboard_list(self):
        self.client.get("/api/v1/dashboard/")

    @task(5)
    @tag("chart", "scenario-b")
    def chart_list(self):
        self.client.get("/api/v1/chart/")

    @task(3)
    @tag("dataset", "scenario-b")
    def dataset_list(self):
        self.client.get("/api/v1/dataset/")

    @task(1)
    @tag("database", "scenario-b")
    def database_list(self):
        self.client.get("/api/v1/database/")

    @task(2)
    @tag("health")
    def health_check(self):
        self.client.get("/health")

    @task(1)
    @tag("async-event", "scenario-b")
    def async_event_poll(self):
        self.client.get("/api/v1/async_event/")

    @task(1)
    @tag("sql", "scenario-e")
    def long_sql_query(self):
        self.client.post(
            "/api/v1/sqllab/execute/",
            json={
                "database_id": 1,
                "sql": "SELECT pg_sleep(5), * FROM large_table LIMIT 1000",
                "schema": "public",
            },
        )
