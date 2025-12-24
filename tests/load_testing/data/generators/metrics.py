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

"""Metrics data generator for time series workloads."""

import json
import uuid
from typing import Any

from .base import DataGenerator


class MetricsGenerator(DataGenerator):
    """
    Generates time series metrics data.
    Target: 50M+ rows for ClickHouse.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        self.metric_names = [
            # System metrics
            "cpu_usage",
            "memory_usage",
            "disk_usage",
            "disk_io_read",
            "disk_io_write",
            "network_in",
            "network_out",
            "load_average",
            "open_files",
            "threads",
            # Application metrics
            "request_latency",
            "request_count",
            "error_rate",
            "success_rate",
            "queue_size",
            "cache_hit_rate",
            "db_query_time",
            "api_response_time",
            # Business metrics
            "active_users",
            "page_views",
            "transactions",
            "revenue",
            "conversion_rate",
            "bounce_rate",
            "session_duration",
        ]

        self.hosts = [f"host-{i:03d}" for i in range(1, 101)]  # 100 hosts
        self.services = [
            "api-gateway",
            "web-server",
            "auth-service",
            "user-service",
            "order-service",
            "payment-service",
            "inventory-service",
            "notification-service",
            "analytics-service",
            "cache-service",
            "database-primary",
            "database-replica",
            "message-queue",
        ]
        self.environments = ["production", "staging", "development"]
        self.env_weights = [70, 20, 10]

        self.datacenters = ["us-east-1", "us-west-2", "eu-west-1", "ap-southeast-1"]

    def generate_row(self) -> dict[str, Any]:
        """Generate a single metrics row."""
        metric_name = self.random_choice(self.metric_names)
        value = self._generate_value(metric_name)

        host = self.random_choice(self.hosts)
        service = self.random_choice(self.services)

        tags = {
            "datacenter": self.random_choice(self.datacenters),
            "instance": f"{service}-{self.random_int(1, 5)}",
        }

        return {
            "metric_id": str(uuid.uuid4()),
            "metric_name": metric_name,
            "timestamp": self.random_timestamp().isoformat(),
            "value": value,
            "tags": json.dumps(tags),
            "host": host,
            "service": service,
            "environment": self.weighted_choice(self.environments, self.env_weights),
        }

    def _generate_value(self, metric_name: str) -> float:
        """Generate realistic value based on metric type."""
        # Percentage metrics (0-100)
        if metric_name in [
            "cpu_usage",
            "memory_usage",
            "disk_usage",
            "cache_hit_rate",
            "success_rate",
            "conversion_rate",
        ]:
            return self.random_float(0, 100)

        # Error rate (typically low)
        if metric_name == "error_rate":
            return self.random_float(0, 5)

        # Bounce rate (moderate)
        if metric_name == "bounce_rate":
            return self.random_float(20, 80)

        # Latency metrics (ms)
        if metric_name in ["request_latency", "db_query_time", "api_response_time"]:
            # Use log-normal-like distribution for latency
            base = self.random_float(10, 100)
            if self.random_bool(0.1):  # 10% slow requests
                base *= self.random_float(5, 20)
            return round(base, 2)

        # Count metrics
        if metric_name in [
            "request_count",
            "active_users",
            "page_views",
            "transactions",
        ]:
            return self.random_int(0, 10000)

        # IO metrics (bytes)
        if metric_name in [
            "network_in",
            "network_out",
            "disk_io_read",
            "disk_io_write",
        ]:
            return self.random_float(0, 1000000000)  # Up to 1GB

        # Queue size
        if metric_name == "queue_size":
            return self.random_int(0, 1000)

        # Revenue
        if metric_name == "revenue":
            return self.random_float(0, 100000)

        # Session duration (seconds)
        if metric_name == "session_duration":
            return self.random_float(30, 3600)

        # Default
        return self.random_float(0, 1000)

    def get_schema(self) -> dict[str, str]:
        """Get ClickHouse schema for metrics table."""
        return {
            "metric_id": "UUID",
            "metric_name": "LowCardinality(String)",
            "timestamp": "DateTime64(3)",
            "value": "Float64",
            "tags": "Map(String, String)",
            "host": "LowCardinality(String)",
            "service": "LowCardinality(String)",
            "environment": "LowCardinality(String)",
        }
