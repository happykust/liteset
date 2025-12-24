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
Load testing configuration settings.
"""

import os
from dataclasses import dataclass, field
from enum import Enum
from functools import lru_cache


class LoadProfile(Enum):
    """Load testing profiles with different intensity levels."""

    SMOKE = "smoke"  # Quick validation: 10 users, 5 min
    LOAD = "load"  # Standard load: 100 users, 30 min
    STRESS = "stress"  # Stress test: 500 users, 15 min
    SPIKE = "spike"  # Spike test: 1000 users, 5 min
    SOAK = "soak"  # Endurance test: 50 users, 4 hours
    BREAKPOINT = "breakpoint"  # Find breaking point: ramp up until failure


class CacheMode(Enum):
    """Cache configuration modes."""

    ENABLED = "enabled"  # Normal caching
    DISABLED = "disabled"  # No caching
    MIXED = "mixed"  # Mix of cached and non-cached requests


@dataclass
class LoadProfileConfig:
    """Configuration for a specific load profile."""

    users: int
    spawn_rate: float
    duration_seconds: int

    @classmethod
    def from_profile(cls, profile: LoadProfile) -> "LoadProfileConfig":
        configs = {
            LoadProfile.SMOKE: cls(users=10, spawn_rate=1, duration_seconds=300),
            LoadProfile.LOAD: cls(users=100, spawn_rate=10, duration_seconds=1800),
            LoadProfile.STRESS: cls(users=500, spawn_rate=50, duration_seconds=900),
            LoadProfile.SPIKE: cls(users=1000, spawn_rate=100, duration_seconds=300),
            LoadProfile.SOAK: cls(users=50, spawn_rate=5, duration_seconds=14400),
            LoadProfile.BREAKPOINT: cls(
                users=2000, spawn_rate=10, duration_seconds=3600
            ),
        }
        return configs[profile]


@dataclass
class SupersetConfig:
    """Superset server configuration."""

    base_url: str = "http://localhost:8088"
    username: str = "admin"
    password: str = "admin"

    # API endpoints
    api_v1_prefix: str = "/api/v1"

    # Timeouts
    request_timeout: int = 60
    long_request_timeout: int = 300

    # Retry configuration
    max_retries: int = 3
    retry_delay: float = 1.0


@dataclass
class RedisConfig:
    """Redis cache configuration."""

    host: str = "localhost"
    port: int = 6379
    db: int = 0
    password: str | None = None


@dataclass
class CeleryConfig:
    """Celery worker configuration for async queries."""

    broker_url: str = "redis://localhost:6379/0"
    result_backend: str = "redis://localhost:6379/1"

    # Polling configuration for async results
    poll_interval: float = 0.5
    max_poll_attempts: int = 120  # 60 seconds max wait


@dataclass
class MetricsConfig:
    """Metrics collection configuration."""

    # Enable detailed metrics
    collect_response_times: bool = True
    collect_throughput: bool = True
    collect_error_rates: bool = True
    collect_percentiles: bool = True

    # Percentiles to track
    percentiles: list[float] = field(
        default_factory=lambda: [0.5, 0.75, 0.90, 0.95, 0.99]
    )

    # Custom metrics
    track_db_query_time: bool = True
    track_cache_hits: bool = True
    track_async_query_time: bool = True

    # Export configuration
    export_to_csv: bool = True
    export_to_json: bool = True
    export_interval_seconds: int = 10


@dataclass
class ScenarioWeights:
    """
    Weights for different scenario types.
    Higher weight = more frequent execution.
    Total doesn't need to sum to 100.
    """

    # Authentication
    auth_login: int = 1
    auth_csrf: int = 5

    # Dashboards
    dashboard_list: int = 10
    dashboard_view: int = 20
    dashboard_filter: int = 15
    dashboard_export: int = 2
    dashboard_favorite: int = 3
    dashboard_create: int = 1
    dashboard_update: int = 1
    dashboard_delete: int = 1

    # Charts
    chart_list: int = 10
    chart_data_simple: int = 25
    chart_data_complex: int = 15
    chart_data_timeseries: int = 20
    chart_data_pivot: int = 10
    chart_thumbnail: int = 5
    chart_create: int = 1
    chart_update: int = 1
    chart_delete: int = 1
    chart_export: int = 2
    chart_cache_warmup: int = 3

    # SQL Lab
    sqllab_bootstrap: int = 5
    sqllab_execute_simple: int = 15
    sqllab_execute_complex: int = 10
    sqllab_execute_async: int = 8
    sqllab_results: int = 10
    sqllab_estimate: int = 5
    sqllab_format: int = 3

    # Explore
    explore_form_data: int = 8
    explore_permalink: int = 5
    explore_datasource: int = 10

    # Datasets
    dataset_list: int = 8
    dataset_get: int = 5
    dataset_samples: int = 5
    dataset_related: int = 3

    # Databases
    database_list: int = 5
    database_schemas: int = 5
    database_tables: int = 8
    database_table_metadata: int = 5

    # Tags
    tag_list: int = 3
    tag_objects: int = 3

    # Queries history
    query_list: int = 5
    query_get: int = 3
    saved_query_list: int = 3

    # Reports
    report_list: int = 2
    report_logs: int = 2

    # Mixed workflows
    workflow_analyst: int = 10
    workflow_data_engineer: int = 5
    workflow_viewer: int = 15
    workflow_power_user: int = 5


@dataclass
class TestDataConfig:
    """Configuration for test data generation."""

    # Data volumes (approximate)
    events_count: int = 100_000_000  # 100M events for ClickHouse
    sales_count: int = 10_000_000  # 10M sales records
    users_count: int = 1_000_000  # 1M users
    metrics_count: int = 50_000_000  # 50M metric points

    # Date ranges
    data_start_date: str = "2020-01-01"
    data_end_date: str = "2024-12-31"

    # Cardinalities
    product_count: int = 10000
    category_count: int = 100
    region_count: int = 50
    country_count: int = 200
    event_type_count: int = 50
    metric_name_count: int = 100

    # Batch sizes for data generation
    batch_size: int = 100000

    # Compression
    use_compression: bool = True


@dataclass
class Settings:
    """Main settings container."""

    superset: SupersetConfig = field(default_factory=SupersetConfig)
    redis: RedisConfig = field(default_factory=RedisConfig)
    celery: CeleryConfig = field(default_factory=CeleryConfig)
    metrics: MetricsConfig = field(default_factory=MetricsConfig)
    weights: ScenarioWeights = field(default_factory=ScenarioWeights)
    test_data: TestDataConfig = field(default_factory=TestDataConfig)

    # Load profile
    profile: LoadProfile = LoadProfile.LOAD
    profile_config: LoadProfileConfig = field(
        default_factory=lambda: LoadProfileConfig.from_profile(LoadProfile.LOAD)
    )

    # Cache mode
    cache_mode: CacheMode = CacheMode.MIXED

    # Logging
    log_level: str = "INFO"
    log_requests: bool = False
    log_responses: bool = False

    # Test execution
    fail_fast: bool = False
    stop_on_error_rate: float = 0.5  # Stop if error rate exceeds 50%

    @classmethod
    def from_env(cls) -> "Settings":
        """Create settings from environment variables."""
        settings = cls()

        # Superset config from env
        settings.superset.base_url = os.getenv(
            "SUPERSET_URL", settings.superset.base_url
        )
        settings.superset.username = os.getenv(
            "SUPERSET_USERNAME", settings.superset.username
        )
        settings.superset.password = os.getenv(
            "SUPERSET_PASSWORD", settings.superset.password
        )

        # Redis config from env
        settings.redis.host = os.getenv("REDIS_HOST", settings.redis.host)
        settings.redis.port = int(os.getenv("REDIS_PORT", str(settings.redis.port)))
        settings.redis.password = os.getenv("REDIS_PASSWORD", settings.redis.password)

        # Celery config from env
        settings.celery.broker_url = os.getenv(
            "CELERY_BROKER_URL", settings.celery.broker_url
        )
        settings.celery.result_backend = os.getenv(
            "CELERY_RESULT_BACKEND", settings.celery.result_backend
        )

        # Load profile from env
        profile_name = os.getenv("LOAD_PROFILE", "load").upper()
        if hasattr(LoadProfile, profile_name):
            settings.profile = LoadProfile[profile_name]
            settings.profile_config = LoadProfileConfig.from_profile(settings.profile)

        # Cache mode from env
        cache_mode_name = os.getenv("CACHE_MODE", "mixed").upper()
        if hasattr(CacheMode, cache_mode_name):
            settings.cache_mode = CacheMode[cache_mode_name]

        # Logging
        settings.log_level = os.getenv("LOG_LEVEL", settings.log_level)
        settings.log_requests = os.getenv("LOG_REQUESTS", "false").lower() == "true"
        settings.log_responses = os.getenv("LOG_RESPONSES", "false").lower() == "true"

        return settings

    def get_api_url(self, endpoint: str) -> str:
        """Get full API URL for an endpoint."""
        if not endpoint.startswith("/"):
            endpoint = f"/{endpoint}"
        return f"{self.superset.base_url}{self.superset.api_v1_prefix}{endpoint}"

    def get_view_url(self, path: str) -> str:
        """Get full URL for a view/page."""
        if not path.startswith("/"):
            path = f"/{path}"
        return f"{self.superset.base_url}{path}"


@lru_cache()
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings.from_env()
