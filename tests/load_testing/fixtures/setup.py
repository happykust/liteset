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
Main fixture setup script for load testing.
Creates databases, datasets, charts, and dashboards in Superset.

Usage:
    python -m fixtures.setup --url http://localhost:8088 \
        --username admin --password admin
"""

import argparse
import logging
import os
import sys
from dataclasses import dataclass

from .superset_client import SupersetClient

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


@dataclass
class DatabaseConfig:
    """Database connection configuration."""

    name: str
    sqlalchemy_uri: str
    tables: list[str]
    schema: str | None = None


# Chart templates for different visualization types
CHART_TEMPLATES = {
    "timeseries": {
        "viz_type": "echarts_timeseries_line",
        "params": {
            "metrics": ["count"],
            "groupby": [],
            "time_grain_sqla": "P1D",
            "time_range": "Last month",
            "row_limit": 10000,
            "truncate_metric": True,
            "show_legend": True,
        },
    },
    "bar": {
        "viz_type": "echarts_bar",
        "params": {
            "metrics": ["count"],
            "groupby": [],
            "time_range": "Last month",
            "row_limit": 100,
            "bar_stacked": False,
            "show_legend": True,
        },
    },
    "pie": {
        "viz_type": "pie",
        "params": {
            "metrics": ["count"],
            "groupby": [],
            "time_range": "Last month",
            "row_limit": 25,
            "donut": False,
            "show_labels": True,
        },
    },
    "table": {
        "viz_type": "table",
        "params": {
            "metrics": ["count"],
            "groupby": [],
            "time_range": "Last month",
            "row_limit": 1000,
            "page_length": 50,
            "include_search": True,
        },
    },
    "big_number": {
        "viz_type": "big_number_total",
        "params": {
            "metrics": ["count"],
            "time_range": "Last month",
            "subheader": "Total Count",
        },
    },
    "big_number_trend": {
        "viz_type": "big_number",
        "params": {
            "metrics": ["count"],
            "time_range": "Last month",
            "time_grain_sqla": "P1D",
            "compare_lag": 7,
        },
    },
    "area": {
        "viz_type": "echarts_area",
        "params": {
            "metrics": ["count"],
            "groupby": [],
            "time_grain_sqla": "P1D",
            "time_range": "Last month",
            "row_limit": 10000,
            "show_legend": True,
            "stack": True,
        },
    },
    "heatmap": {
        "viz_type": "heatmap",
        "params": {
            "metrics": ["count"],
            "all_columns_x": [],
            "all_columns_y": [],
            "time_range": "Last month",
            "row_limit": 10000,
        },
    },
    "scatter": {
        "viz_type": "echarts_scatter",
        "params": {
            "x_axis": None,
            "y_axis": None,
            "size": None,
            "time_range": "Last month",
            "row_limit": 5000,
        },
    },
    "funnel": {
        "viz_type": "funnel",
        "params": {
            "metrics": ["count"],
            "groupby": [],
            "time_range": "Last month",
            "row_limit": 50,
        },
    },
    "gauge": {
        "viz_type": "gauge_chart",
        "params": {
            "metrics": ["count"],
            "time_range": "Last month",
            "min_val": 0,
            "max_val": 100,
        },
    },
    "pivot": {
        "viz_type": "pivot_table_v2",
        "params": {
            "metrics": ["count"],
            "groupbyRows": [],
            "groupbyColumns": [],
            "time_range": "Last month",
            "row_limit": 10000,
        },
    },
    "world_map": {
        "viz_type": "world_map",
        "params": {
            "metrics": ["count"],
            "entity": None,
            "time_range": "Last month",
        },
    },
    "treemap": {
        "viz_type": "treemap_v2",
        "params": {
            "metrics": ["count"],
            "groupby": [],
            "time_range": "Last month",
            "row_limit": 1000,
        },
    },
    "sunburst": {
        "viz_type": "sunburst_v2",
        "params": {
            "metrics": ["count"],
            "groupby": [],
            "time_range": "Last month",
        },
    },
}


class SupersetFixtureSetup:
    """
    Sets up test fixtures in Superset for load testing.
    Creates databases, datasets, charts, and dashboards.
    """

    def __init__(
        self,
        superset_url: str,
        username: str,
        password: str,
        clickhouse_uri: str | None = None,
        postgres_uri: str | None = None,
        mysql_uri: str | None = None,
    ):
        self.client = SupersetClient(superset_url, username, password)
        self.databases: list[DatabaseConfig] = []
        self.created_databases: dict[str, int] = {}
        self.created_datasets: dict[str, int] = {}
        self.created_charts: list[int] = []
        self.created_dashboards: list[int] = []

        # Configure databases
        if clickhouse_uri:
            self.databases.append(
                DatabaseConfig(
                    name="LoadTest ClickHouse",
                    sqlalchemy_uri=clickhouse_uri,
                    schema="loadtest",
                    tables=["events", "metrics", "user_sessions"],
                )
            )

        if postgres_uri:
            self.databases.append(
                DatabaseConfig(
                    name="LoadTest PostgreSQL",
                    sqlalchemy_uri=postgres_uri,
                    schema="public",
                    tables=[
                        "sales",
                        "users",
                        "products",
                        "events",
                        "categories",
                        "regions",
                    ],
                )
            )

        if mysql_uri:
            self.databases.append(
                DatabaseConfig(
                    name="LoadTest MySQL",
                    sqlalchemy_uri=mysql_uri,
                    schema="loadtest",
                    tables=[
                        "orders",
                        "order_items",
                        "customers",
                        "inventory",
                        "warehouses",
                    ],
                )
            )

    def run(self) -> dict:
        """Run the full fixture setup."""
        logger.info("=" * 60)
        logger.info("SUPERSET LOAD TEST FIXTURE SETUP")
        logger.info("=" * 60)

        # Login
        if not self.client.login():
            logger.error("Failed to login to Superset!")
            return {"success": False, "error": "Login failed"}

        logger.info("✓ Logged in successfully")

        results = {
            "databases": 0,
            "datasets": 0,
            "charts": 0,
            "dashboards": 0,
        }

        try:
            # Step 1: Create database connections
            results["databases"] = self._setup_databases()

            # Step 2: Create datasets
            results["datasets"] = self._setup_datasets()

            # Step 3: Create charts
            results["charts"] = self._setup_charts()

            # Step 4: Create dashboards
            results["dashboards"] = self._setup_dashboards()

            # Summary
            logger.info("=" * 60)
            logger.info("SETUP COMPLETE")
            logger.info(f"  Databases:  {results['databases']}")
            logger.info(f"  Datasets:   {results['datasets']}")
            logger.info(f"  Charts:     {results['charts']}")
            logger.info(f"  Dashboards: {results['dashboards']}")
            logger.info("=" * 60)

            return {"success": True, **results}

        except Exception as e:
            logger.error(f"Setup failed: {e}")
            return {"success": False, "error": str(e), **results}

    def _setup_databases(self) -> int:
        """Create database connections."""
        logger.info("\n📦 Setting up databases...")
        count = 0

        # Check existing databases
        existing = {db["database_name"]: db["id"] for db in self.client.get_databases()}

        for db_config in self.databases:
            if db_config.name in existing:
                db_id = existing[db_config.name]
                logger.info(f"  Database '{db_config.name}' exists (id={db_id})")
                self.created_databases[db_config.name] = existing[db_config.name]
                count += 1
                continue

            result = self.client.create_database(
                name=db_config.name,
                sqlalchemy_uri=db_config.sqlalchemy_uri,
                extra={"allows_virtual_table_explore": True},
            )

            if result and "id" in result:
                db_id = result["id"]
                self.created_databases[db_config.name] = db_id
                logger.info(f"  ✓ Created database '{db_config.name}' (id={db_id})")
                count += 1
            else:
                logger.error(f"  ✗ Failed to create database '{db_config.name}'")

        return count

    def _setup_datasets(self) -> int:
        """Create datasets from database tables."""
        logger.info("\n📊 Setting up datasets...")
        count = 0

        # Check existing datasets
        existing = {
            f"{ds.get('schema', '')}.{ds['table_name']}": ds["id"]
            for ds in self.client.get_datasets()
        }

        for db_config in self.databases:
            db_id = self.created_databases.get(db_config.name)
            if not db_id:
                continue

            for table in db_config.tables:
                dataset_key = f"{db_config.schema or ''}.{table}"

                if dataset_key in existing:
                    ds_id = existing[dataset_key]
                    logger.info(f"  Dataset '{table}' already exists (id={ds_id})")
                    self.created_datasets[table] = ds_id
                    # Refresh to get latest columns
                    self.client.refresh_dataset(ds_id)
                    count += 1
                    continue

                result = self.client.create_dataset(
                    database_id=db_id, table_name=table, schema=db_config.schema
                )

                if result and "id" in result:
                    ds_id = result["id"]
                    self.created_datasets[table] = ds_id
                    logger.info(f"  ✓ Created dataset '{table}' (id={ds_id})")
                    # Refresh to populate columns
                    self.client.refresh_dataset(ds_id)
                    count += 1
                else:
                    logger.error(f"  ✗ Failed to create dataset '{table}'")

        return count

    def _setup_charts(self) -> int:
        """Create charts for each dataset."""
        logger.info("\n📈 Setting up charts...")
        count = 0

        # Get dataset details for columns
        datasets_detail = {}
        for ds in self.client.get_datasets():
            ds_full = self.client.get(f"/api/v1/dataset/{ds['id']}")
            if ds_full and "result" in ds_full:
                datasets_detail[ds["id"]] = ds_full["result"]

        # Chart configurations per dataset type
        chart_configs = self._get_chart_configs()

        for table_name, ds_id in self.created_datasets.items():
            ds_detail = datasets_detail.get(ds_id, {})
            columns = [c["column_name"] for c in ds_detail.get("columns", [])]
            metrics = [m["metric_name"] for m in ds_detail.get("metrics", [])]

            # Get chart configs for this table
            configs = chart_configs.get(
                table_name, self._get_default_charts(columns, metrics)
            )

            for chart_name, config in configs.items():
                full_name = f"{table_name} - {chart_name}"

                result = self.client.create_chart(
                    slice_name=full_name,
                    viz_type=config["viz_type"],
                    datasource_id=ds_id,
                    params=config.get("params", {}),
                )

                if result and "id" in result:
                    chart_id = result["id"]
                    self.created_charts.append(chart_id)
                    logger.info(f"  ✓ Created chart '{full_name}' (id={chart_id})")
                    count += 1
                else:
                    logger.error(f"  ✗ Failed to create chart '{full_name}'")

        return count

    def _get_chart_configs(self) -> dict:
        """Get chart configurations for specific tables."""
        return {
            # ClickHouse events
            "events": {
                "Event Count Over Time": {
                    "viz_type": "echarts_timeseries_line",
                    "params": {
                        "metrics": [
                            {
                                "expressionType": "SQL",
                                "sqlExpression": "COUNT(*)",
                                "label": "count",
                            }
                        ],
                        "groupby": [],
                        "time_grain_sqla": "P1D",
                        "time_range": "Last month",
                        "x_axis": "timestamp",
                    },
                },
                "Events by Type": {
                    "viz_type": "pie",
                    "params": {
                        "metrics": [
                            {
                                "expressionType": "SQL",
                                "sqlExpression": "COUNT(*)",
                                "label": "count",
                            }
                        ],
                        "groupby": ["event_type"],
                        "time_range": "Last month",
                        "row_limit": 20,
                    },
                },
                "Events by Country": {
                    "viz_type": "echarts_bar",
                    "params": {
                        "metrics": [
                            {
                                "expressionType": "SQL",
                                "sqlExpression": "COUNT(*)",
                                "label": "count",
                            }
                        ],
                        "groupby": ["country"],
                        "time_range": "Last month",
                        "row_limit": 20,
                    },
                },
                "Platform Distribution": {
                    "viz_type": "pie",
                    "params": {
                        "metrics": [
                            {
                                "expressionType": "SQL",
                                "sqlExpression": "COUNT(*)",
                                "label": "count",
                            }
                        ],
                        "groupby": ["platform"],
                        "time_range": "Last month",
                    },
                },
                "Events by Device": {
                    "viz_type": "echarts_bar",
                    "params": {
                        "metrics": [
                            {
                                "expressionType": "SQL",
                                "sqlExpression": "COUNT(*)",
                                "label": "count",
                            }
                        ],
                        "groupby": ["device_type"],
                        "time_range": "Last month",
                    },
                },
                "Total Events": {
                    "viz_type": "big_number_total",
                    "params": {
                        "metrics": [
                            {
                                "expressionType": "SQL",
                                "sqlExpression": "COUNT(*)",
                                "label": "Total Events",
                            }
                        ],
                        "time_range": "Last month",
                    },
                },
                "Conversion Rate": {
                    "viz_type": "big_number_total",
                    "params": {
                        "metrics": [
                            {
                                "expressionType": "SQL",
                                "sqlExpression": "AVG(is_converted)*100",
                                "label": "Conversion %",
                            }
                        ],
                        "time_range": "Last month",
                    },
                },
                "Events Table": {
                    "viz_type": "table",
                    "params": {
                        "metrics": [
                            {
                                "expressionType": "SQL",
                                "sqlExpression": "COUNT(*)",
                                "label": "count",
                            }
                        ],
                        "groupby": ["event_type", "country", "platform"],
                        "time_range": "Last week",
                        "row_limit": 500,
                    },
                },
            },
            # PostgreSQL sales
            "sales": {
                "Revenue Over Time": {
                    "viz_type": "echarts_timeseries_line",
                    "params": {
                        "metrics": [
                            {
                                "expressionType": "SQL",
                                "sqlExpression": "SUM(amount)",
                                "label": "Revenue",
                            }
                        ],
                        "time_grain_sqla": "P1D",
                        "time_range": "Last month",
                        "x_axis": "sale_date",
                    },
                },
                "Sales by Region": {
                    "viz_type": "echarts_bar",
                    "params": {
                        "metrics": [
                            {
                                "expressionType": "SQL",
                                "sqlExpression": "SUM(amount)",
                                "label": "Revenue",
                            },
                            {
                                "expressionType": "SQL",
                                "sqlExpression": "COUNT(*)",
                                "label": "Orders",
                            },
                        ],
                        "groupby": ["region_id"],
                        "time_range": "Last month",
                    },
                },
                "Payment Methods": {
                    "viz_type": "pie",
                    "params": {
                        "metrics": [
                            {
                                "expressionType": "SQL",
                                "sqlExpression": "COUNT(*)",
                                "label": "count",
                            }
                        ],
                        "groupby": ["payment_method"],
                        "time_range": "Last month",
                    },
                },
                "Order Status": {
                    "viz_type": "pie",
                    "params": {
                        "metrics": [
                            {
                                "expressionType": "SQL",
                                "sqlExpression": "COUNT(*)",
                                "label": "count",
                            }
                        ],
                        "groupby": ["status"],
                        "time_range": "Last month",
                    },
                },
                "Total Revenue": {
                    "viz_type": "big_number_total",
                    "params": {
                        "metrics": [
                            {
                                "expressionType": "SQL",
                                "sqlExpression": "SUM(amount)",
                                "label": "Total Revenue",
                            }
                        ],
                        "time_range": "Last month",
                    },
                },
                "Average Order Value": {
                    "viz_type": "big_number_total",
                    "params": {
                        "metrics": [
                            {
                                "expressionType": "SQL",
                                "sqlExpression": "AVG(amount)",
                                "label": "AOV",
                            }
                        ],
                        "time_range": "Last month",
                    },
                },
                "Sales Pivot": {
                    "viz_type": "pivot_table_v2",
                    "params": {
                        "metrics": [
                            {
                                "expressionType": "SQL",
                                "sqlExpression": "SUM(amount)",
                                "label": "Revenue",
                            }
                        ],
                        "groupbyRows": ["region_id"],
                        "groupbyColumns": ["payment_method"],
                        "time_range": "Last month",
                    },
                },
            },
            # MySQL orders
            "orders": {
                "Orders Over Time": {
                    "viz_type": "echarts_timeseries_line",
                    "params": {
                        "metrics": [
                            {
                                "expressionType": "SQL",
                                "sqlExpression": "COUNT(*)",
                                "label": "Orders",
                            }
                        ],
                        "time_grain_sqla": "P1D",
                        "time_range": "Last month",
                        "x_axis": "order_date",
                    },
                },
                "Order Status Distribution": {
                    "viz_type": "pie",
                    "params": {
                        "metrics": [
                            {
                                "expressionType": "SQL",
                                "sqlExpression": "COUNT(*)",
                                "label": "count",
                            }
                        ],
                        "groupby": ["status"],
                        "time_range": "Last month",
                    },
                },
                "Revenue by Payment": {
                    "viz_type": "echarts_bar",
                    "params": {
                        "metrics": [
                            {
                                "expressionType": "SQL",
                                "sqlExpression": "SUM(total_amount)",
                                "label": "Revenue",
                            }
                        ],
                        "groupby": ["payment_method"],
                        "time_range": "Last month",
                    },
                },
                "Total Orders": {
                    "viz_type": "big_number_total",
                    "params": {
                        "metrics": [
                            {
                                "expressionType": "SQL",
                                "sqlExpression": "COUNT(*)",
                                "label": "Total Orders",
                            }
                        ],
                        "time_range": "Last month",
                    },
                },
            },
            # Metrics table
            "metrics": {
                "Metrics Over Time": {
                    "viz_type": "echarts_timeseries_line",
                    "params": {
                        "metrics": [
                            {
                                "expressionType": "SQL",
                                "sqlExpression": "AVG(value)",
                                "label": "avg_value",
                            }
                        ],
                        "time_grain_sqla": "PT1H",
                        "time_range": "Last week",
                        "x_axis": "timestamp",
                    },
                },
                "Metrics by Service": {
                    "viz_type": "echarts_bar",
                    "params": {
                        "metrics": [
                            {
                                "expressionType": "SQL",
                                "sqlExpression": "AVG(value)",
                                "label": "avg_value",
                            }
                        ],
                        "groupby": ["service"],
                        "time_range": "Last week",
                    },
                },
                "Metrics by Host": {
                    "viz_type": "table",
                    "params": {
                        "metrics": [
                            {
                                "expressionType": "SQL",
                                "sqlExpression": "AVG(value)",
                                "label": "avg",
                            },
                            {
                                "expressionType": "SQL",
                                "sqlExpression": "MAX(value)",
                                "label": "max",
                            },
                        ],
                        "groupby": ["host", "metric_name"],
                        "time_range": "Last day",
                        "row_limit": 200,
                    },
                },
            },
            # Users
            "users": {
                "Users by Country": {
                    "viz_type": "echarts_bar",
                    "params": {
                        "metrics": [
                            {
                                "expressionType": "SQL",
                                "sqlExpression": "COUNT(*)",
                                "label": "count",
                            }
                        ],
                        "groupby": ["country"],
                        "row_limit": 20,
                    },
                },
                "Users by Segment": {
                    "viz_type": "pie",
                    "params": {
                        "metrics": [
                            {
                                "expressionType": "SQL",
                                "sqlExpression": "COUNT(*)",
                                "label": "count",
                            }
                        ],
                        "groupby": ["segment"],
                    },
                },
                "Total Users": {
                    "viz_type": "big_number_total",
                    "params": {
                        "metrics": [
                            {
                                "expressionType": "SQL",
                                "sqlExpression": "COUNT(*)",
                                "label": "Total Users",
                            }
                        ],
                    },
                },
                "Total LTV": {
                    "viz_type": "big_number_total",
                    "params": {
                        "metrics": [
                            {
                                "expressionType": "SQL",
                                "sqlExpression": "SUM(lifetime_value)",
                                "label": "Total LTV",
                            }
                        ],
                    },
                },
            },
            # Customers
            "customers": {
                "Customers by Segment": {
                    "viz_type": "pie",
                    "params": {
                        "metrics": [
                            {
                                "expressionType": "SQL",
                                "sqlExpression": "COUNT(*)",
                                "label": "count",
                            }
                        ],
                        "groupby": ["customer_segment"],
                    },
                },
                "Customer LTV": {
                    "viz_type": "echarts_bar",
                    "params": {
                        "metrics": [
                            {
                                "expressionType": "SQL",
                                "sqlExpression": "SUM(total_spent)",
                                "label": "LTV",
                            }
                        ],
                        "groupby": ["customer_segment"],
                    },
                },
            },
        }

    def _get_default_charts(self, columns: list, metrics: list) -> dict:
        """Generate default charts when no specific config exists."""
        charts = {}

        # Basic count chart
        charts["Total Count"] = {
            "viz_type": "big_number_total",
            "params": {
                "metrics": [
                    {
                        "expressionType": "SQL",
                        "sqlExpression": "COUNT(*)",
                        "label": "count",
                    }
                ],
            },
        }

        # Table view
        charts["Data Table"] = {
            "viz_type": "table",
            "params": {
                "metrics": [
                    {
                        "expressionType": "SQL",
                        "sqlExpression": "COUNT(*)",
                        "label": "count",
                    }
                ],
                "groupby": columns[:3] if columns else [],
                "row_limit": 100,
            },
        }

        return charts

    def _setup_dashboards(self) -> int:
        """Create dashboards with charts."""
        logger.info("\n🎨 Setting up dashboards...")
        count = 0

        # Group charts by dataset/category
        dashboard_configs = [
            {
                "title": "Analytics Overview (ClickHouse)",
                "slug": "loadtest-analytics",
                "tables": ["events", "metrics", "user_sessions"],
            },
            {
                "title": "Sales Dashboard (PostgreSQL)",
                "slug": "loadtest-sales",
                "tables": ["sales", "users", "products"],
            },
            {
                "title": "E-Commerce Dashboard (MySQL)",
                "slug": "loadtest-ecommerce",
                "tables": ["orders", "order_items", "customers"],
            },
            {
                "title": "Executive Summary",
                "slug": "loadtest-executive",
                "tables": ["events", "sales", "orders"],
            },
        ]

        # Get all charts
        all_charts = self.client.get_charts()
        chart_by_name = {c["slice_name"]: c["id"] for c in all_charts}

        for config in dashboard_configs:
            title = str(config["title"])
            slug = str(config["slug"])
            result = self.client.create_dashboard(
                title=title, slug=slug, published=True
            )

            if not result or "id" not in result:
                logger.error(f"  ✗ Failed to create dashboard '{config['title']}'")
                continue

            dashboard_id = result["id"]
            self.created_dashboards.append(dashboard_id)
            logger.info(
                f"  ✓ Created dashboard '{config['title']}' (id={dashboard_id})"
            )
            count += 1

            # Find charts for this dashboard
            chart_ids = []
            for chart_name, chart_id in chart_by_name.items():
                for table in config["tables"]:
                    if chart_name.startswith(f"{table} -"):
                        chart_ids.append(chart_id)
                        break

            if chart_ids:
                # Add charts to dashboard
                self.client.add_charts_to_dashboard(dashboard_id, chart_ids[:20])
                logger.info(f"    Added {len(chart_ids[:20])} charts to dashboard")

        # Create additional dashboards for stress testing
        for i in range(1, 11):
            result = self.client.create_dashboard(
                title=f"Load Test Dashboard #{i}",
                slug=f"loadtest-stress-{i}",
                published=True,
            )
            if result and "id" in result:
                dashboard_id = result["id"]
                self.created_dashboards.append(dashboard_id)
                # Add random charts
                chart_ids = list(chart_by_name.values())[: min(8, len(chart_by_name))]
                if chart_ids:
                    self.client.add_charts_to_dashboard(dashboard_id, chart_ids)
                logger.info(f"  ✓ Created stress dashboard #{i} (id={dashboard_id})")
                count += 1

        return count


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Setup Superset fixtures for load testing"
    )
    parser.add_argument(
        "--url",
        default=os.getenv("SUPERSET_URL", "http://localhost:8088"),
        help="Superset URL",
    )
    parser.add_argument(
        "--username",
        default=os.getenv("SUPERSET_USERNAME", "admin"),
        help="Superset username",
    )
    parser.add_argument(
        "--password",
        default=os.getenv("SUPERSET_PASSWORD", "admin"),
        help="Superset password",
    )
    parser.add_argument(
        "--clickhouse",
        default=os.getenv(
            "CLICKHOUSE_URI", "clickhousedb://default:@clickhouse:8123/loadtest"
        ),
        help="ClickHouse SQLAlchemy URI",
    )
    parser.add_argument(
        "--postgres",
        default=os.getenv(
            "POSTGRES_URI",
            "postgresql+psycopg2://loadtest:loadtest@postgres:5432/loadtest",
        ),
        help="PostgreSQL SQLAlchemy URI",
    )
    parser.add_argument(
        "--mysql",
        default=os.getenv(
            "MYSQL_URI", "mysql+pymysql://loadtest:loadtest@mysql:3306/loadtest"
        ),
        help="MySQL SQLAlchemy URI",
    )

    args = parser.parse_args()

    logger.info(f"Superset URL: {args.url}")
    logger.info(f"Username: {args.username}")

    setup = SupersetFixtureSetup(
        superset_url=args.url,
        username=args.username,
        password=args.password,
        clickhouse_uri=args.clickhouse,
        postgres_uri=args.postgres,
        mysql_uri=args.mysql,
    )

    result = setup.run()

    if not result.get("success"):
        logger.error(f"Setup failed: {result.get('error')}")
        sys.exit(1)

    logger.info("Fixture setup completed successfully!")
    sys.exit(0)


if __name__ == "__main__":
    main()
