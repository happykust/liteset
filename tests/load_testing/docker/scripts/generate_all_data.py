#!/usr/bin/env python3
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
Generate large volumes of test data for all databases.
Target: 10-100 GB of data across ClickHouse, PostgreSQL, MySQL.

Usage:
    python generate_all_data.py --clickhouse-rows 100000000 --postgres-rows 10000000
"""

import argparse
import json
import logging
import os
import random
import uuid
from datetime import datetime, timedelta
from typing import Any, Generator

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


# ============================================
# Data Generation Functions
# ============================================

EVENT_TYPES = [
    "page_view",
    "click",
    "scroll",
    "form_submit",
    "purchase",
    "signup",
    "login",
    "logout",
    "search",
    "add_to_cart",
    "remove_from_cart",
    "checkout_start",
    "checkout_complete",
    "video_play",
    "video_pause",
    "download",
    "share",
    "like",
]

COUNTRIES = [
    "US",
    "UK",
    "DE",
    "FR",
    "JP",
    "BR",
    "IN",
    "CA",
    "AU",
    "MX",
    "IT",
    "ES",
    "NL",
    "KR",
    "SG",
]
PLATFORMS = ["web", "ios", "android", "desktop"]
BROWSERS = ["Chrome", "Firefox", "Safari", "Edge", "Opera"]
DEVICES = ["desktop", "mobile", "tablet"]
PAYMENT_METHODS = ["credit_card", "debit_card", "paypal", "bank_transfer", "apple_pay"]
ORDER_STATUSES = [
    "completed",
    "pending",
    "processing",
    "shipped",
    "cancelled",
    "refunded",
]
SEGMENTS = ["Premium", "Standard", "Basic", "Enterprise", "Free"]


def random_timestamp(days_back: int = 365) -> datetime:
    """Generate random timestamp within the past N days."""
    delta = timedelta(seconds=random.randint(0, days_back * 86400))
    return datetime.now() - delta


def generate_event() -> dict[str, Any]:
    """Generate a single event record."""
    return {
        "event_id": str(uuid.uuid4()),
        "user_id": random.randint(1, 1000000),
        "event_type": random.choice(EVENT_TYPES),
        "timestamp": random_timestamp(),
        "properties": json.dumps(
            {
                "source": random.choice(
                    ["organic", "paid", "social", "email", "direct"]
                ),
                "value": round(random.random() * 100, 2),
            }
        ),
        "session_id": str(uuid.uuid4()),
        "platform": random.choice(PLATFORMS),
        "country": random.choice(COUNTRIES),
        "region": f"region_{random.randint(1, 50)}",
        "device_type": random.choice(DEVICES),
        "browser": random.choice(BROWSERS),
        "value": round(random.random() * 1000, 2),
        "duration_ms": random.randint(0, 60000),
        "is_converted": 1 if random.random() < 0.1 else 0,
    }


def generate_metric() -> dict[str, Any]:
    """Generate a single metric record."""
    metric_names = [
        "cpu_usage",
        "memory_usage",
        "disk_io",
        "network_in",
        "network_out",
        "request_latency",
        "error_rate",
        "queue_size",
        "cache_hit_rate",
    ]
    return {
        "metric_id": str(uuid.uuid4()),
        "metric_name": random.choice(metric_names),
        "timestamp": random_timestamp(days_back=30),
        "value": round(random.random() * 100, 2),
        "tags": json.dumps(
            {
                "datacenter": random.choice(["us-east-1", "us-west-2", "eu-west-1"]),
                "env": random.choice(["prod", "staging"]),
            }
        ),
        "host": f"host-{random.randint(1, 100):03d}",
        "service": random.choice(["api", "web", "worker", "database", "cache"]),
        "environment": random.choice(["production", "staging", "development"]),
    }


def generate_sale() -> dict[str, Any]:
    """Generate a single sale record."""
    quantity = random.randint(1, 10)
    unit_price = round(5 + random.random() * 500, 2)
    discount = round(random.random() * 0.3, 2) if random.random() < 0.2 else 0
    return {
        "product_id": random.randint(1, 10000),
        "user_id": random.randint(1, 1000000),
        "amount": round(quantity * unit_price * (1 - discount), 2),
        "quantity": quantity,
        "discount": round(discount * 100, 2),
        "sale_date": random_timestamp(),
        "region_id": random.randint(1, 50),
        "category_id": random.randint(1, 100),
        "payment_method": random.choice(PAYMENT_METHODS),
        "status": random.choices(ORDER_STATUSES, weights=[60, 15, 10, 5, 5, 5])[0],
    }


def generate_order() -> dict[str, Any]:
    """Generate a single order record for MySQL."""
    return {
        "customer_id": random.randint(1, 100000),
        "order_date": random_timestamp(),
        "total_amount": round(10 + random.random() * 500, 2),
        "discount_amount": round(random.random() * 50, 2)
        if random.random() < 0.3
        else 0,
        "shipping_cost": round(5 + random.random() * 20, 2),
        "status": random.choice(ORDER_STATUSES),
        "payment_method": random.choice(PAYMENT_METHODS),
    }


def generate_batches(
    generator_func, total_rows: int, batch_size: int = 100000
) -> Generator[list[dict], None, None]:
    """Generate data in batches."""
    generated = 0
    while generated < total_rows:
        current_batch_size = min(batch_size, total_rows - generated)
        batch = [generator_func() for _ in range(current_batch_size)]
        yield batch
        generated += current_batch_size
        if generated % 1000000 == 0:
            logger.info(f"  Generated {generated:,} / {total_rows:,} rows")


# ============================================
# ClickHouse Data Loading
# ============================================


def load_clickhouse_data(host: str, rows: int, batch_size: int = 100000):
    """Load data into ClickHouse."""
    try:
        from clickhouse_driver import Client
    except ImportError:
        logger.error(
            "clickhouse-driver not installed. Run: pip install clickhouse-driver"
        )
        return

    logger.info(f"Connecting to ClickHouse at {host}...")
    client = Client(host=host)

    # Events table - 60% of data
    events_rows = int(rows * 0.6)
    logger.info(f"Generating {events_rows:,} events...")

    for batch in generate_batches(generate_event, events_rows, batch_size):
        # Convert to tuples for insertion
        data = [
            (
                e["event_id"],
                e["user_id"],
                e["event_type"],
                e["timestamp"],
                e["properties"],
                e["session_id"],
                e["platform"],
                e["country"],
                e["region"],
                e["device_type"],
                e["browser"],
                e["value"],
                e["duration_ms"],
                e["is_converted"],
            )
            for e in batch
        ]
        client.execute(
            """
            INSERT INTO loadtest.events
            (event_id, user_id, event_type, timestamp, properties,
             session_id, platform, country, region, device_type,
             browser, value, duration_ms, is_converted)
            VALUES
            """,
            data,
        )

    # Metrics table - 40% of data
    metrics_rows = int(rows * 0.4)
    logger.info(f"Generating {metrics_rows:,} metrics...")

    for batch in generate_batches(generate_metric, metrics_rows, batch_size):
        data = [  # type: ignore[misc]
            (
                m["metric_id"],
                m["metric_name"],
                m["timestamp"],
                m["value"],
                m["tags"],
                m["host"],
                m["service"],
                m["environment"],
            )
            for m in batch
        ]
        client.execute(
            """
            INSERT INTO loadtest.metrics
            (metric_id, metric_name, timestamp, value, tags, host, service, environment)
            VALUES
            """,
            data,
        )

    logger.info("ClickHouse data loading complete!")


# ============================================
# PostgreSQL Data Loading
# ============================================


def load_postgres_data(host: str, rows: int, batch_size: int = 50000):
    """Load data into PostgreSQL."""
    try:
        import psycopg2
        from psycopg2.extras import execute_values
    except ImportError:
        logger.error("psycopg2 not installed. Run: pip install psycopg2-binary")
        return

    logger.info(f"Connecting to PostgreSQL at {host}...")
    db_password = os.getenv("POSTGRES_PASSWORD", "loadtest")  # noqa: S105
    conn = psycopg2.connect(
        host=host, database="loadtest", user="loadtest", password=db_password
    )
    cursor = conn.cursor()

    logger.info(f"Generating {rows:,} sales records...")

    for batch in generate_batches(generate_sale, rows, batch_size):
        data = [
            (
                s["product_id"],
                s["user_id"],
                s["amount"],
                s["quantity"],
                s["discount"],
                s["sale_date"],
                s["region_id"],
                s["category_id"],
                s["payment_method"],
                s["status"],
            )
            for s in batch
        ]
        execute_values(
            cursor,
            """
            INSERT INTO sales
            (product_id, user_id, amount, quantity, discount, sale_date,
             region_id, category_id, payment_method, status)
            VALUES %s
            """,
            data,
        )
        conn.commit()

    cursor.close()
    conn.close()
    logger.info("PostgreSQL data loading complete!")


# ============================================
# MySQL Data Loading
# ============================================


def load_mysql_data(host: str, rows: int, batch_size: int = 50000):
    """Load data into MySQL."""
    try:
        import pymysql
    except ImportError:
        logger.error("pymysql not installed. Run: pip install pymysql")
        return

    logger.info(f"Connecting to MySQL at {host}...")
    mysql_password = os.getenv("MYSQL_PASSWORD", "loadtest")  # noqa: S105
    conn = pymysql.connect(
        host=host, database="loadtest", user="loadtest", password=mysql_password
    )
    cursor = conn.cursor()

    logger.info(f"Generating {rows:,} order records...")

    for batch in generate_batches(generate_order, rows, batch_size):
        data = [
            (
                o["customer_id"],
                o["order_date"],
                o["total_amount"],
                o["discount_amount"],
                o["shipping_cost"],
                o["status"],
                o["payment_method"],
            )
            for o in batch
        ]
        cursor.executemany(
            """
            INSERT INTO orders
            (customer_id, order_date, total_amount, discount_amount,
             shipping_cost, status, payment_method)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            data,
        )
        conn.commit()

    cursor.close()
    conn.close()
    logger.info("MySQL data loading complete!")


# ============================================
# Main
# ============================================


def main():
    parser = argparse.ArgumentParser(description="Generate test data for load testing")

    parser.add_argument(
        "--clickhouse-host",
        default=os.getenv("CLICKHOUSE_HOST", "clickhouse"),
        help="ClickHouse host",
    )
    parser.add_argument(
        "--clickhouse-rows",
        type=int,
        default=int(os.getenv("CLICKHOUSE_ROWS", "100000000")),
        help="Number of rows for ClickHouse (default: 100M)",
    )
    parser.add_argument(
        "--postgres-host",
        default=os.getenv("POSTGRES_HOST", "postgres"),
        help="PostgreSQL host",
    )
    parser.add_argument(
        "--postgres-rows",
        type=int,
        default=int(os.getenv("POSTGRES_ROWS", "10000000")),
        help="Number of rows for PostgreSQL (default: 10M)",
    )
    parser.add_argument(
        "--mysql-host", default=os.getenv("MYSQL_HOST", "mysql"), help="MySQL host"
    )
    parser.add_argument(
        "--mysql-rows",
        type=int,
        default=int(os.getenv("MYSQL_ROWS", "5000000")),
        help="Number of rows for MySQL (default: 5M)",
    )
    parser.add_argument(
        "--batch-size", type=int, default=100000, help="Batch size for inserts"
    )
    parser.add_argument(
        "--skip-clickhouse", action="store_true", help="Skip ClickHouse data generation"
    )
    parser.add_argument(
        "--skip-postgres", action="store_true", help="Skip PostgreSQL data generation"
    )
    parser.add_argument(
        "--skip-mysql", action="store_true", help="Skip MySQL data generation"
    )

    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("LOAD TEST DATA GENERATION")
    logger.info("=" * 60)
    logger.info(f"ClickHouse: {args.clickhouse_rows:,} rows")
    logger.info(f"PostgreSQL: {args.postgres_rows:,} rows")
    logger.info(f"MySQL:      {args.mysql_rows:,} rows")
    logger.info("=" * 60)

    if not args.skip_clickhouse:
        try:
            load_clickhouse_data(
                args.clickhouse_host, args.clickhouse_rows, args.batch_size
            )
        except Exception as e:
            logger.error(f"ClickHouse loading failed: {e}")

    if not args.skip_postgres:
        try:
            load_postgres_data(args.postgres_host, args.postgres_rows, args.batch_size)
        except Exception as e:
            logger.error(f"PostgreSQL loading failed: {e}")

    if not args.skip_mysql:
        try:
            load_mysql_data(args.mysql_host, args.mysql_rows, args.batch_size)
        except Exception as e:
            logger.error(f"MySQL loading failed: {e}")

    logger.info("=" * 60)
    logger.info("DATA GENERATION COMPLETE")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
