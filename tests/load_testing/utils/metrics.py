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
Custom metrics collection for load testing.
"""

import csv
import json
import logging
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from statistics import mean, median, stdev
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class MetricPoint:
    """Single metric measurement point."""

    name: str
    value: float
    timestamp: float = field(default_factory=time.time)
    tags: dict[str, str] = field(default_factory=dict)


@dataclass
class MetricSummary:
    """Summary statistics for a metric."""

    name: str
    count: int
    total: float
    min_val: float
    max_val: float
    avg: float
    median: float
    p50: float
    p75: float
    p90: float
    p95: float
    p99: float
    std_dev: float


class MetricsCollector:
    """
    Collects and aggregates custom metrics for load testing.
    Thread-safe implementation.
    """

    def __init__(self, export_dir: str | None = None):
        self._metrics: dict[str, list[MetricPoint]] = defaultdict(list)
        self._lock = threading.RLock()
        self._export_dir = Path(export_dir) if export_dir else Path("./metrics_output")
        self._export_dir.mkdir(parents=True, exist_ok=True)
        self._start_time = time.time()

        # Counters
        self._counters: dict[str, int] = defaultdict(int)

        # Cache hit tracking
        self._cache_hits: int = 0
        self._cache_misses: int = 0

        # Error tracking
        self._errors: dict[str, int] = defaultdict(int)

        # Database query times by database
        self._db_query_times: dict[str, list[float]] = defaultdict(list)

        # Async query tracking
        self._async_queries_started: int = 0
        self._async_queries_completed: int = 0
        self._async_query_times: list[float] = []

    def record(
        self, name: str, value: float, tags: dict[str, str] | None = None
    ) -> None:
        """Record a metric value."""
        point = MetricPoint(
            name=name, value=value, timestamp=time.time(), tags=tags or {}
        )
        with self._lock:
            self._metrics[name].append(point)

    def increment(self, name: str, amount: int = 1) -> None:
        """Increment a counter."""
        with self._lock:
            self._counters[name] += amount

    def record_response_time(
        self, endpoint: str, response_time_ms: float, success: bool = True
    ) -> None:
        """Record API response time."""
        metric_name = f"response_time.{endpoint}"
        self.record(metric_name, response_time_ms, {"success": str(success)})

        if success:
            self.increment(f"requests.success.{endpoint}")
        else:
            self.increment(f"requests.failure.{endpoint}")

    def record_cache_hit(self, hit: bool = True) -> None:
        """Record cache hit or miss."""
        with self._lock:
            if hit:
                self._cache_hits += 1
            else:
                self._cache_misses += 1

    def record_db_query_time(self, database: str, query_time_ms: float) -> None:
        """Record database query execution time."""
        with self._lock:
            self._db_query_times[database].append(query_time_ms)
        self.record(f"db_query_time.{database}", query_time_ms)

    def record_async_query_start(self) -> None:
        """Record async query start."""
        with self._lock:
            self._async_queries_started += 1

    def record_async_query_complete(self, total_time_ms: float) -> None:
        """Record async query completion."""
        with self._lock:
            self._async_queries_completed += 1
            self._async_query_times.append(total_time_ms)
        self.record("async_query_time", total_time_ms)

    def record_error(self, error_type: str, endpoint: str | None = None) -> None:
        """Record an error occurrence."""
        with self._lock:
            key = f"{error_type}:{endpoint}" if endpoint else error_type
            self._errors[key] += 1

    def get_cache_hit_ratio(self) -> float:
        """Get cache hit ratio."""
        with self._lock:
            total = self._cache_hits + self._cache_misses
            if total == 0:
                return 0.0
            return self._cache_hits / total

    def get_summary(self, name: str) -> MetricSummary | None:
        """Get summary statistics for a metric."""
        with self._lock:
            points = self._metrics.get(name, [])

        if not points:
            return None

        values = sorted([p.value for p in points])
        count = len(values)

        def percentile(data: list[float], p: float) -> float:
            idx = int(len(data) * p)
            return data[min(idx, len(data) - 1)]

        return MetricSummary(
            name=name,
            count=count,
            total=sum(values),
            min_val=min(values),
            max_val=max(values),
            avg=mean(values),
            median=median(values),
            p50=percentile(values, 0.50),
            p75=percentile(values, 0.75),
            p90=percentile(values, 0.90),
            p95=percentile(values, 0.95),
            p99=percentile(values, 0.99),
            std_dev=stdev(values) if count > 1 else 0.0,
        )

    def get_all_summaries(self) -> dict[str, MetricSummary]:
        """Get summaries for all metrics."""
        with self._lock:
            metric_names = list(self._metrics.keys())

        summaries = {}
        for name in metric_names:
            summary = self.get_summary(name)
            if summary:
                summaries[name] = summary

        return summaries

    def get_counters(self) -> dict[str, int]:
        """Get all counter values."""
        with self._lock:
            return dict(self._counters)

    def get_errors(self) -> dict[str, int]:
        """Get all error counts."""
        with self._lock:
            return dict(self._errors)

    def get_throughput(self) -> dict[str, float]:
        """Calculate throughput (requests per second) for each endpoint."""
        elapsed_seconds = time.time() - self._start_time
        if elapsed_seconds == 0:
            return {}

        counters = self.get_counters()
        throughput = {}

        for key, count in counters.items():
            if key.startswith("requests."):
                throughput[key] = count / elapsed_seconds

        return throughput

    def get_error_rate(self) -> float:
        """Calculate overall error rate."""
        counters = self.get_counters()

        total_success = sum(
            v for k, v in counters.items() if k.startswith("requests.success")
        )
        total_failure = sum(
            v for k, v in counters.items() if k.startswith("requests.failure")
        )

        total = total_success + total_failure
        if total == 0:
            return 0.0

        return total_failure / total

    def get_report(self) -> dict[str, Any]:
        """Generate comprehensive metrics report."""
        elapsed = time.time() - self._start_time

        report = {
            "timestamp": datetime.now().isoformat(),
            "duration_seconds": elapsed,
            "summaries": {},
            "counters": self.get_counters(),
            "errors": self.get_errors(),
            "throughput": self.get_throughput(),
            "error_rate": self.get_error_rate(),
            "cache": {
                "hits": self._cache_hits,
                "misses": self._cache_misses,
                "hit_ratio": self.get_cache_hit_ratio(),
            },
            "async_queries": {
                "started": self._async_queries_started,
                "completed": self._async_queries_completed,
                "avg_time_ms": (
                    mean(self._async_query_times) if self._async_query_times else 0
                ),
            },
            "database_queries": {},
        }

        # Add metric summaries
        for name, summary in self.get_all_summaries().items():
            report["summaries"][name] = {  # type: ignore[index]
                "count": summary.count,
                "min": summary.min_val,
                "max": summary.max_val,
                "avg": summary.avg,
                "median": summary.median,
                "p50": summary.p50,
                "p75": summary.p75,
                "p90": summary.p90,
                "p95": summary.p95,
                "p99": summary.p99,
                "std_dev": summary.std_dev,
            }

        # Add database query stats
        with self._lock:
            for db, times in self._db_query_times.items():
                if times:
                    report["database_queries"][db] = {  # type: ignore[index]
                        "count": len(times),
                        "avg_ms": mean(times),
                        "max_ms": max(times),
                        "min_ms": min(times),
                    }

        return report

    def export_to_json(self, filename: str | None = None) -> str:
        """Export metrics to JSON file."""
        if filename is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"metrics_{timestamp}.json"

        filepath = self._export_dir / filename
        report = self.get_report()

        with open(filepath, "w") as f:
            json.dump(report, f, indent=2, default=str)

        logger.info(f"Metrics exported to {filepath}")
        return str(filepath)

    def export_to_csv(self, filename: str | None = None) -> str:
        """Export raw metrics to CSV file."""
        if filename is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"metrics_{timestamp}.csv"

        filepath = self._export_dir / filename

        with self._lock:
            all_points = []
            for name, points in self._metrics.items():
                for point in points:
                    all_points.append(
                        {
                            "name": point.name,
                            "value": point.value,
                            "timestamp": point.timestamp,
                            **point.tags,
                        }
                    )

        if all_points:
            # Get all unique tag keys
            all_tags: set[str] = set()
            for point in all_points:  # type: ignore[assignment]
                all_tags.update(point.keys())  # type: ignore[union-attr]

            fieldnames = ["name", "value", "timestamp"] + sorted(
                all_tags - {"name", "value", "timestamp"}
            )

            with open(filepath, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(all_points)

        logger.info(f"Metrics exported to {filepath}")
        return str(filepath)

    def reset(self) -> None:
        """Reset all metrics."""
        with self._lock:
            self._metrics.clear()
            self._counters.clear()
            self._errors.clear()
            self._cache_hits = 0
            self._cache_misses = 0
            self._db_query_times.clear()
            self._async_queries_started = 0
            self._async_queries_completed = 0
            self._async_query_times.clear()
            self._start_time = time.time()


# Global metrics collector instance
_global_collector: MetricsCollector | None = None
_collector_lock = threading.Lock()


def get_metrics_collector() -> MetricsCollector:
    """Get or create global metrics collector."""
    global _global_collector
    with _collector_lock:
        if _global_collector is None:
            _global_collector = MetricsCollector()
        return _global_collector


def track_custom_metric(
    name: str, value: float, tags: dict[str, str] | None = None
) -> None:
    """Convenience function to track a custom metric."""
    get_metrics_collector().record(name, value, tags)


def track_response_time(endpoint: str, response_time_ms: float, success: bool = True):
    """Convenience function to track response time."""
    get_metrics_collector().record_response_time(endpoint, response_time_ms, success)


def track_cache(hit: bool = True) -> None:
    """Convenience function to track cache hit/miss."""
    get_metrics_collector().record_cache_hit(hit)


def track_db_query(database: str, query_time_ms: float) -> None:
    """Convenience function to track database query time."""
    get_metrics_collector().record_db_query_time(database, query_time_ms)


def track_error(error_type: str, endpoint: str | None = None) -> None:
    """Convenience function to track errors."""
    get_metrics_collector().record_error(error_type, endpoint)


class MetricsTimer:
    """Context manager for timing operations and recording metrics."""

    def __init__(
        self,
        metric_name: str,
        tags: dict[str, str] | None = None,
        collector: MetricsCollector | None = None,
    ):
        self.metric_name = metric_name
        self.tags = tags or {}
        self.collector = collector or get_metrics_collector()
        self.start_time: float = 0
        self.duration_ms: float = 0

    def __enter__(self):
        self.start_time = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.duration_ms = (time.perf_counter() - self.start_time) * 1000
        self.tags["success"] = str(exc_type is None)
        self.collector.record(self.metric_name, self.duration_ms, self.tags)
