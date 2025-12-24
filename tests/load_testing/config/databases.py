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
Database configurations for load testing.
Supports ClickHouse, PostgreSQL, MySQL, and Trino.
"""

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class DatabaseType(Enum):
    """Supported database types."""

    CLICKHOUSE = "clickhouse"
    POSTGRESQL = "postgresql"
    MYSQL = "mysql"
    TRINO = "trino"
    SQLITE = "sqlite"


@dataclass
class DatabaseConfig:
    """Configuration for a database connection."""

    name: str
    db_type: DatabaseType
    host: str
    port: int
    database: str
    username: str
    password: str

    # Additional options
    options: dict[str, Any] = field(default_factory=dict)

    # Data configuration
    schema: str | None = None
    tables: list[str] = field(default_factory=list)

    # Test queries
    test_queries: list[str] = field(default_factory=list)

    @property
    def sqlalchemy_uri(self) -> str:
        """Generate SQLAlchemy URI for the database."""
        if self.db_type == DatabaseType.CLICKHOUSE:
            # ClickHouse native driver
            return f"clickhousedb://{self.username}:{self.password}@{self.host}:{self.port}/{self.database}"
        elif self.db_type == DatabaseType.POSTGRESQL:
            return f"postgresql+psycopg2://{self.username}:{self.password}@{self.host}:{self.port}/{self.database}"
        elif self.db_type == DatabaseType.MYSQL:
            return f"mysql+pymysql://{self.username}:{self.password}@{self.host}:{self.port}/{self.database}"
        elif self.db_type == DatabaseType.TRINO:
            catalog = self.options.get("catalog", "hive")
            schema = self.schema or "default"
            return f"trino://{self.username}@{self.host}:{self.port}/{catalog}/{schema}"
        elif self.db_type == DatabaseType.SQLITE:
            return f"sqlite:///{self.database}"
        else:
            raise ValueError(f"Unsupported database type: {self.db_type}")

    @property
    def superset_payload(self) -> dict[str, Any]:
        """Generate payload for Superset database creation API."""
        return {
            "database_name": self.name,
            "sqlalchemy_uri": self.sqlalchemy_uri,
            "expose_in_sqllab": True,
            "allow_run_async": True,
            "allow_ctas": True,
            "allow_cvas": True,
            "allow_dml": True,
            "allow_file_upload": True,
            "extra": {
                "metadata_params": {},
                "engine_params": {},
                "metadata_cache_timeout": {},
                "schemas_allowed_for_file_upload": [self.schema] if self.schema else [],
            },
        }


# Pre-configured database configurations
CLICKHOUSE_CONFIG = DatabaseConfig(
    name="ClickHouse LoadTest",
    db_type=DatabaseType.CLICKHOUSE,
    host=os.getenv("CLICKHOUSE_HOST", "localhost"),
    port=int(os.getenv("CLICKHOUSE_PORT", "8123")),
    database=os.getenv("CLICKHOUSE_DATABASE", "loadtest"),
    username=os.getenv("CLICKHOUSE_USER", "default"),
    password=os.getenv("CLICKHOUSE_PASSWORD", ""),
    schema="loadtest",
    tables=[
        "events",
        "events_distributed",
        "metrics",
        "metrics_distributed",
    ],
    test_queries=[
        # Simple count
        "SELECT count() FROM events",
        # Aggregation by date
        "SELECT toDate(timestamp) as date, count() FROM events "
        "GROUP BY date ORDER BY date",
        # Complex aggregation
        """
        SELECT 
            event_type,
            count() as cnt,
            uniqExact(user_id) as unique_users,
            avg(JSONExtractFloat(properties, 'value')) as avg_value
        FROM events
        WHERE timestamp >= now() - INTERVAL 7 DAY
        GROUP BY event_type
        ORDER BY cnt DESC
        LIMIT 100
        """,
        # Timeseries with granularity
        """
        SELECT 
            toStartOfHour(timestamp) as hour,
            count() as events,
            uniqExact(user_id) as users
        FROM events
        WHERE timestamp >= now() - INTERVAL 24 HOUR
        GROUP BY hour
        ORDER BY hour
        """,
        # Heavy aggregation
        """
        SELECT 
            toDate(timestamp) as date,
            event_type,
            count() as cnt,
            uniqExact(user_id) as unique_users,
            quantile(0.5)(JSONExtractFloat(properties, 'duration')) as median_duration,
            quantile(0.95)(JSONExtractFloat(properties, 'duration')) as p95_duration
        FROM events
        WHERE timestamp >= now() - INTERVAL 30 DAY
        GROUP BY date, event_type
        ORDER BY date, cnt DESC
        """,
    ],
)

POSTGRESQL_CONFIG = DatabaseConfig(
    name="PostgreSQL LoadTest",
    db_type=DatabaseType.POSTGRESQL,
    host=os.getenv("POSTGRES_HOST", "localhost"),
    port=int(os.getenv("POSTGRES_PORT", "5432")),
    database=os.getenv("POSTGRES_DATABASE", "loadtest"),
    username=os.getenv("POSTGRES_USER", "loadtest"),
    password=os.getenv("POSTGRES_PASSWORD", "loadtest"),
    schema="public",
    tables=[
        "sales",
        "users",
        "products",
        "categories",
        "regions",
    ],
    test_queries=[
        # Simple select
        "SELECT * FROM sales LIMIT 1000",
        # Aggregation
        """
        SELECT 
            DATE_TRUNC('day', sale_date) as date,
            COUNT(*) as sales_count,
            SUM(amount) as total_amount
        FROM sales
        WHERE sale_date >= CURRENT_DATE - INTERVAL '30 days'
        GROUP BY date
        ORDER BY date
        """,
        # Join query
        """
        SELECT 
            c.category_name,
            r.region_name,
            COUNT(*) as sales_count,
            SUM(s.amount) as total_amount,
            AVG(s.amount) as avg_amount
        FROM sales s
        JOIN products p ON s.product_id = p.product_id
        JOIN categories c ON p.category_id = c.category_id
        JOIN regions r ON s.region_id = r.region_id
        WHERE s.sale_date >= CURRENT_DATE - INTERVAL '90 days'
        GROUP BY c.category_name, r.region_name
        ORDER BY total_amount DESC
        LIMIT 100
        """,
        # Window function
        """
        SELECT 
            sale_date,
            amount,
            SUM(amount) OVER (
                ORDER BY sale_date ROWS BETWEEN 6 PRECEDING AND CURRENT ROW
            ) as rolling_7d_sum,
            AVG(amount) OVER (
                ORDER BY sale_date ROWS BETWEEN 29 PRECEDING AND CURRENT ROW
            ) as rolling_30d_avg
        FROM sales
        WHERE sale_date >= CURRENT_DATE - INTERVAL '60 days'
        ORDER BY sale_date
        """,
        # Subquery
        """
        SELECT 
            u.user_id,
            u.name,
            u.country,
            user_stats.total_purchases,
            user_stats.total_amount
        FROM users u
        JOIN (
            SELECT 
                user_id,
                COUNT(*) as total_purchases,
                SUM(amount) as total_amount
            FROM sales
            GROUP BY user_id
            HAVING COUNT(*) > 10
        ) user_stats ON u.user_id = user_stats.user_id
        ORDER BY user_stats.total_amount DESC
        LIMIT 100
        """,
    ],
)

MYSQL_CONFIG = DatabaseConfig(
    name="MySQL LoadTest",
    db_type=DatabaseType.MYSQL,
    host=os.getenv("MYSQL_HOST", "localhost"),
    port=int(os.getenv("MYSQL_PORT", "3306")),
    database=os.getenv("MYSQL_DATABASE", "loadtest"),
    username=os.getenv("MYSQL_USER", "loadtest"),
    password=os.getenv("MYSQL_PASSWORD", "loadtest"),
    schema="loadtest",
    tables=[
        "orders",
        "order_items",
        "customers",
    ],
    test_queries=[
        # Simple select
        "SELECT * FROM orders LIMIT 1000",
        # Aggregation
        """
        SELECT 
            DATE(order_date) as date,
            COUNT(*) as order_count,
            SUM(total_amount) as revenue
        FROM orders
        WHERE order_date >= DATE_SUB(CURDATE(), INTERVAL 30 DAY)
        GROUP BY date
        ORDER BY date
        """,
        # Join query
        """
        SELECT 
            c.customer_segment,
            COUNT(DISTINCT o.order_id) as orders,
            SUM(oi.quantity) as items_sold,
            SUM(oi.price * oi.quantity) as revenue
        FROM orders o
        JOIN customers c ON o.customer_id = c.customer_id
        JOIN order_items oi ON o.order_id = oi.order_id
        WHERE o.order_date >= DATE_SUB(CURDATE(), INTERVAL 90 DAY)
        GROUP BY c.customer_segment
        ORDER BY revenue DESC
        """,
    ],
)

TRINO_CONFIG = DatabaseConfig(
    name="Trino LoadTest",
    db_type=DatabaseType.TRINO,
    host=os.getenv("TRINO_HOST", "localhost"),
    port=int(os.getenv("TRINO_PORT", "8080")),
    database="",
    username=os.getenv("TRINO_USER", "trino"),
    password="",
    schema="loadtest",
    options={"catalog": "hive"},
    tables=[
        "analytics_events",
        "user_sessions",
    ],
    test_queries=[
        # Simple query
        "SELECT * FROM analytics_events LIMIT 1000",
        # Aggregation across catalogs
        """
        SELECT 
            date_trunc('day', event_time) as date,
            event_name,
            count(*) as event_count,
            count(distinct user_id) as unique_users
        FROM analytics_events
        WHERE event_time >= current_date - interval '7' day
        GROUP BY 1, 2
        ORDER BY 1, 3 DESC
        """,
    ],
)


def get_database_configs() -> list[DatabaseConfig]:
    """Get all configured database connections."""
    configs = []

    # Check which databases are enabled via environment
    if os.getenv("ENABLE_CLICKHOUSE", "true").lower() == "true":
        configs.append(CLICKHOUSE_CONFIG)

    if os.getenv("ENABLE_POSTGRESQL", "true").lower() == "true":
        configs.append(POSTGRESQL_CONFIG)

    if os.getenv("ENABLE_MYSQL", "true").lower() == "true":
        configs.append(MYSQL_CONFIG)

    if os.getenv("ENABLE_TRINO", "false").lower() == "true":
        configs.append(TRINO_CONFIG)

    return configs


def get_database_by_name(name: str) -> DatabaseConfig | None:
    """Get database configuration by name."""
    for config in get_database_configs():
        if config.name == name:
            return config
    return None


def get_database_by_type(db_type: DatabaseType) -> DatabaseConfig | None:
    """Get first database configuration of given type."""
    for config in get_database_configs():
        if config.db_type == db_type:
            return config
    return None
