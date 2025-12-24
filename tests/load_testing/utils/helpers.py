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
Helper utilities for load testing.
"""

import hashlib
import random
import string
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable, TypeVar

T = TypeVar("T")


def random_string(length: int = 10) -> str:
    """Generate random alphanumeric string."""
    return "".join(random.choices(string.ascii_letters + string.digits, k=length))


def random_choice(items: list[T]) -> T:
    """Safely choose random item from list."""
    if not items:
        raise ValueError("Cannot choose from empty list")
    return random.choice(items)


def random_choices(items: list[T], k: int = 1) -> list[T]:
    """Safely choose k random items from list (with replacement)."""
    if not items:
        raise ValueError("Cannot choose from empty list")
    return random.choices(items, k=k)


def random_sample(items: list[T], k: int = 1) -> list[T]:
    """Safely sample k random items from list (without replacement)."""
    if not items:
        raise ValueError("Cannot sample from empty list")
    k = min(k, len(items))
    return random.sample(items, k)


def weighted_random_choice(items: list[T], weights: list[int]) -> T:
    """Choose random item based on weights."""
    if not items:
        raise ValueError("Cannot choose from empty list")
    if len(items) != len(weights):
        raise ValueError("Items and weights must have same length")
    return random.choices(items, weights=weights, k=1)[0]


def generate_uuid() -> str:
    """Generate UUID string."""
    return str(uuid.uuid4())


def generate_cache_key(*args) -> str:
    """Generate cache key from arguments."""
    key_str = ":".join(str(arg) for arg in args)
    return hashlib.md5(key_str.encode()).hexdigest()


def random_date_range(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    """Generate random date range within bounds."""
    delta = end - start
    random_start_offset = random.randint(0, delta.days - 1)
    random_range_days = random.randint(1, min(30, delta.days - random_start_offset))

    range_start = start + timedelta(days=random_start_offset)
    range_end = range_start + timedelta(days=random_range_days)

    return range_start, range_end


def random_granularity() -> str:
    """Return random time granularity."""
    granularities = [
        "PT1M",  # 1 minute
        "PT5M",  # 5 minutes
        "PT15M",  # 15 minutes
        "PT1H",  # 1 hour
        "P1D",  # 1 day
        "P1W",  # 1 week
        "P1M",  # 1 month
    ]
    return random_choice(granularities)


def random_time_range() -> str:
    """Return random time range string."""
    time_ranges = [
        "Last day",
        "Last week",
        "Last month",
        "Last quarter",
        "Last year",
        "No filter",
    ]
    return random_choice(time_ranges)


def random_row_limit() -> int:
    """Return random row limit."""
    limits = [100, 500, 1000, 5000, 10000, 50000]
    return random_choice(limits)


def random_viz_type() -> str:
    """Return random visualization type."""
    viz_types = [
        "echarts_timeseries_line",
        "echarts_timeseries_bar",
        "echarts_area",
        "big_number_total",
        "big_number",
        "table",
        "pivot_table_v2",
        "echarts_pie",
        "dist_bar",
        "bar",
        "line",
        "area",
        "scatter",
        "bubble",
        "treemap",
        "box_plot",
        "histogram",
        "funnel",
        "gauge_chart",
        "heatmap",
        "world_map",
        "filter_box",
    ]
    return random_choice(viz_types)


def wait_for_async_query(
    poll_func: Callable[[], dict | None],
    success_statuses: list[str] | None = None,
    failure_statuses: list[str] | None = None,
    max_attempts: int = 60,
    poll_interval: float = 1.0,
    timeout: float | None = None,
) -> dict | None:
    """
    Wait for async operation to complete by polling.

    Args:
        poll_func: Function that returns current status dict
        success_statuses: List of status values indicating success
        failure_statuses: List of status values indicating failure
        max_attempts: Maximum number of poll attempts
        poll_interval: Seconds between polls
        timeout: Optional total timeout in seconds

    Returns:
        Final result dict or None if timeout/failure
    """
    if success_statuses is None:
        success_statuses = ["success", "completed", "done"]
    if failure_statuses is None:
        failure_statuses = ["failed", "error", "stopped", "cancelled"]

    start_time = time.time()

    for attempt in range(max_attempts):
        if timeout and (time.time() - start_time) > timeout:
            return None

        result = poll_func()
        if result is None:
            time.sleep(poll_interval)
            continue

        status = None
        # Try different status field locations
        if isinstance(result, dict):
            status = (
                result.get("status")
                or result.get("result", {}).get("status")
                or result.get("state")
            )

        if status:
            status_lower = status.lower()
            if status_lower in [s.lower() for s in success_statuses]:
                return result
            if status_lower in [s.lower() for s in failure_statuses]:
                return result

        time.sleep(poll_interval)

    return None


def build_query_context(
    datasource_id: int,
    datasource_type: str = "table",
    columns: list[str] | None = None,
    metrics: list[dict | str] | None = None,
    filters: list[dict] | None = None,
    groupby: list[str] | None = None,
    time_column: str | None = None,
    time_range: str | None = None,
    time_grain: str | None = None,
    row_limit: int = 1000,
    order_desc: bool = True,
    force: bool = False,
    result_format: str = "json",
    result_type: str = "full",
) -> dict[str, Any]:
    """
    Build a query context for chart data API.

    This is the payload structure expected by POST /api/v1/chart/data
    """
    query: dict[str, Any] = {
        "row_limit": row_limit,
        "order_desc": order_desc,
        "force": force,
    }

    if columns:
        query["columns"] = columns

    if metrics:
        query["metrics"] = metrics

    if filters:
        query["filters"] = filters

    if groupby:
        query["groupby"] = groupby

    if time_column:
        query["time_column"] = time_column
        query["granularity"] = time_column

    if time_range:
        query["time_range"] = time_range

    if time_grain:
        query["time_grain_sqla"] = time_grain

    return {
        "datasource": {"id": datasource_id, "type": datasource_type},
        "queries": [query],
        "result_format": result_format,
        "result_type": result_type,
        "force": force,
    }


def build_timeseries_query_context(
    datasource_id: int,
    time_column: str,
    metric: str | dict,
    time_range: str = "Last week",
    time_grain: str = "P1D",
    groupby: list[str] | None = None,
    filters: list[dict] | None = None,
    row_limit: int = 10000,
    force: bool = False,
) -> dict[str, Any]:
    """Build query context specifically for timeseries charts."""
    metrics_list = [metric] if isinstance(metric, str) else [metric]  # type: ignore[list-item]

    return build_query_context(  # type: ignore[arg-type]
        datasource_id=datasource_id,
        metrics=metrics_list,
        groupby=groupby,
        filters=filters,
        time_column=time_column,
        time_range=time_range,
        time_grain=time_grain,
        row_limit=row_limit,
        force=force,
    )


def build_pivot_query_context(
    datasource_id: int,
    groupby_rows: list[str],
    groupby_cols: list[str],
    metrics: list[str | dict],
    time_range: str = "Last month",
    row_limit: int = 50000,
    force: bool = False,
) -> dict[str, Any]:
    """Build query context for pivot table."""
    return {
        "datasource": {"id": datasource_id, "type": "table"},
        "queries": [
            {
                "groupby": groupby_rows,
                "columns": groupby_cols,
                "metrics": metrics,
                "time_range": time_range,
                "row_limit": row_limit,
                "order_desc": True,
                "force": force,
            }
        ],
        "result_format": "json",
        "result_type": "full",
        "force": force,
    }


def build_simple_query_context(
    datasource_id: int, columns: list[str], row_limit: int = 1000, force: bool = False
) -> dict[str, Any]:
    """Build simple query context for raw data retrieval."""
    return build_query_context(
        datasource_id=datasource_id, columns=columns, row_limit=row_limit, force=force
    )


class Timer:
    """Context manager for timing operations."""

    def __init__(self):
        self.start_time: float = 0
        self.end_time: float = 0
        self.duration: float = 0

    def __enter__(self):
        self.start_time = time.perf_counter()
        return self

    def __exit__(self, *args):
        self.end_time = time.perf_counter()
        self.duration = self.end_time - self.start_time


def exponential_backoff(
    attempt: int, base_delay: float = 1.0, max_delay: float = 60.0, jitter: bool = True
) -> float:
    """Calculate exponential backoff delay."""
    delay = min(base_delay * (2**attempt), max_delay)
    if jitter:
        delay = delay * (0.5 + random.random())
    return delay


def chunk_list(lst: list[T], chunk_size: int) -> list[list[T]]:
    """Split list into chunks of specified size."""
    return [lst[i : i + chunk_size] for i in range(0, len(lst), chunk_size)]
