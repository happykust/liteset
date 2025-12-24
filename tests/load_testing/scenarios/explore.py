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
Explore scenarios for load testing.
Tests chart creation and data exploration workflows.
"""

import json
import logging
from typing import Any, TYPE_CHECKING

from ..utils.helpers import random_choice, random_string, random_viz_type
from ..utils.metrics import get_metrics_collector, MetricsTimer

if TYPE_CHECKING:
    from ..utils.api_client import SupersetAPIClient

logger = logging.getLogger(__name__)


class ExploreScenarios:
    """Explore-related load testing scenarios."""

    def __init__(self, client: "SupersetAPIClient"):
        self.client = client
        self.metrics = get_metrics_collector()
        self._dataset_cache: list[dict] = []
        self._form_data_keys: list[str] = []

    def _refresh_dataset_cache(self) -> None:
        """Refresh local cache of datasets."""
        result = self.client.get_datasets(page_size=100)
        if result and "result" in result:
            self._dataset_cache = result["result"]

    def _get_random_dataset(self) -> dict | None:
        """Get random dataset from cache."""
        if not self._dataset_cache:
            self._refresh_dataset_cache()
        if self._dataset_cache:
            return random_choice(self._dataset_cache)
        return None

    def get_form_data(self, key: str) -> dict | None:
        """
        Scenario: Get explore form data by key
        Retrieves saved chart configuration.
        """
        with MetricsTimer("explore.get_form_data"):
            return self.client.get_explore_form_data(key)

    def save_form_data(
        self,
        datasource_id: int | None = None,
        datasource_type: str = "table",
        form_data: dict | None = None,
        chart_id: int | None = None,
    ) -> dict | None:
        """
        Scenario: Save explore form data
        Persists chart configuration for sharing/embedding.
        """
        if datasource_id is None:
            dataset = self._get_random_dataset()
            datasource_id = dataset.get("id") if dataset else None

        if datasource_id is None:
            return None

        if form_data is None:
            form_data = self._generate_sample_form_data(datasource_id)

        with MetricsTimer("explore.save_form_data"):
            result = self.client.save_explore_form_data(
                datasource_id=datasource_id,
                datasource_type=datasource_type,
                form_data=form_data,
                chart_id=chart_id,
            )

            if result and "key" in result:
                self._form_data_keys.append(result["key"])

            return result

    def update_form_data(self, key: str, form_data: dict) -> dict | None:
        """
        Scenario: Update explore form data
        """
        payload = {"form_data": json.dumps(form_data)}

        with MetricsTimer("explore.update_form_data"):
            return self.client.put(
                f"/api/v1/explore/form_data/{key}",
                name="PUT /api/v1/explore/form_data/<key>",
                json_data=payload,
            )

    def delete_form_data(self, key: str) -> dict | None:
        """
        Scenario: Delete explore form data
        """
        with MetricsTimer("explore.delete_form_data"):
            return self.client.delete(
                f"/api/v1/explore/form_data/{key}",
                name="DELETE /api/v1/explore/form_data/<key>",
            )

    def create_permalink(
        self, datasource_id: int | None = None, form_data: dict | None = None
    ) -> dict | None:
        """
        Scenario: Create explore permalink
        Generates shareable link for current explore state.
        """
        if datasource_id is None:
            dataset = self._get_random_dataset()
            datasource_id = dataset.get("id") if dataset else None

        if datasource_id is None:
            return None

        if form_data is None:
            form_data = self._generate_sample_form_data(datasource_id)

        payload = {"formData": json.dumps(form_data), "urlParams": []}

        with MetricsTimer("explore.create_permalink"):
            return self.client.post(
                "/api/v1/explore/permalink",
                name="POST /api/v1/explore/permalink",
                json_data=payload,
            )

    def get_permalink(self, key: str) -> dict | None:
        """
        Scenario: Get explore permalink data
        """
        with MetricsTimer("explore.get_permalink"):
            return self.client.get(
                f"/api/v1/explore/permalink/{key}",
                name="GET /api/v1/explore/permalink/<key>",
            )

    def get_datasource(
        self, datasource_id: int | None = None, datasource_type: str = "table"
    ) -> dict | None:
        """
        Scenario: Get datasource details for explore
        """
        if datasource_id is None:
            dataset = self._get_random_dataset()
            datasource_id = dataset.get("id") if dataset else None

        if datasource_id is None:
            return None

        with MetricsTimer("explore.get_datasource"):
            return self.client.get(
                f"/api/v1/datasource/{datasource_type}/{datasource_id}",
                name="GET /api/v1/datasource/<type>/<id>",
            )

    def get_datasource_samples(
        self,
        datasource_id: int | None = None,
        datasource_type: str = "table",
        force: bool = False,
    ) -> dict | None:
        """
        Scenario: Get sample data from datasource
        """
        if datasource_id is None:
            dataset = self._get_random_dataset()
            datasource_id = dataset.get("id") if dataset else None

        if datasource_id is None:
            return None

        payload = {
            "datasource": {"id": datasource_id, "type": datasource_type},
            "force": force,
        }

        with MetricsTimer("explore.get_samples"):
            return self.client.post(
                "/api/v1/datasource/samples",
                name="POST /api/v1/datasource/samples",
                json_data=payload,
            )

    def explore_chart_workflow(self, datasource_id: int | None = None) -> dict:
        """
        Scenario: Full explore workflow
        Simulates creating a new chart from scratch.
        """
        results: dict[str, Any] = {
            "datasource": None,
            "samples": None,
            "form_data_saved": None,
            "chart_data": None,
        }

        if datasource_id is None:
            dataset = self._get_random_dataset()
            datasource_id = dataset.get("id") if dataset else None

        if datasource_id is None:
            return results

        # Step 1: Get datasource metadata
        results["datasource"] = self.get_datasource(datasource_id)

        # Step 2: Get sample data
        results["samples"] = self.get_datasource_samples(datasource_id)

        # Step 3: Build and save form data
        form_data = self._generate_sample_form_data(datasource_id)
        results["form_data_saved"] = self.save_form_data(
            datasource_id=datasource_id, form_data=form_data
        )

        # Step 4: Fetch chart data
        query_context = self._build_query_context_from_form_data(
            datasource_id, form_data
        )

        with MetricsTimer("explore.chart_data"):
            results["chart_data"] = self.client.get_chart_data(query_context)

        return results

    def switch_viz_type(
        self,
        datasource_id: int,
        current_form_data: dict,
        new_viz_type: str | None = None,
    ) -> dict | None:
        """
        Scenario: Switch visualization type
        Simulates user changing chart type in explore.
        """
        if new_viz_type is None:
            new_viz_type = random_viz_type()

        # Update form data with new viz type
        updated_form_data = current_form_data.copy()
        updated_form_data["viz_type"] = new_viz_type

        # Build query context and fetch data
        query_context = self._build_query_context_from_form_data(
            datasource_id, updated_form_data
        )

        with MetricsTimer("explore.switch_viz", {"viz_type": new_viz_type}):
            return self.client.get_chart_data(query_context)

    def add_filter(
        self,
        datasource_id: int,
        current_form_data: dict,
        filter_col: str,
        filter_op: str,
        filter_val: Any,
    ) -> dict | None:
        """
        Scenario: Add filter in explore
        Simulates user adding a filter to the chart.
        """
        updated_form_data = current_form_data.copy()

        # Add filter to adhoc_filters
        new_filter = {
            "clause": "WHERE",
            "comparator": filter_val,
            "expressionType": "SIMPLE",
            "operator": filter_op,
            "subject": filter_col,
        }

        if "adhoc_filters" not in updated_form_data:
            updated_form_data["adhoc_filters"] = []

        updated_form_data["adhoc_filters"].append(new_filter)

        query_context = self._build_query_context_from_form_data(
            datasource_id, updated_form_data
        )

        with MetricsTimer("explore.add_filter"):
            return self.client.get_chart_data(query_context)

    def change_time_range(
        self, datasource_id: int, current_form_data: dict, time_range: str = "Last week"
    ) -> dict | None:
        """
        Scenario: Change time range in explore
        """
        updated_form_data = current_form_data.copy()
        updated_form_data["time_range"] = time_range

        query_context = self._build_query_context_from_form_data(
            datasource_id, updated_form_data
        )

        with MetricsTimer("explore.change_time_range"):
            return self.client.get_chart_data(query_context)

    def add_metric(
        self,
        datasource_id: int,
        current_form_data: dict,
        metric_expression: str,
        metric_label: str | None = None,
    ) -> dict | None:
        """
        Scenario: Add metric in explore
        """
        if metric_label is None:
            metric_label = f"Metric {random_string(4)}"

        updated_form_data = current_form_data.copy()

        new_metric = {
            "expressionType": "SQL",
            "sqlExpression": metric_expression,
            "label": metric_label,
        }

        if "metrics" not in updated_form_data:
            updated_form_data["metrics"] = []

        updated_form_data["metrics"].append(new_metric)

        query_context = self._build_query_context_from_form_data(
            datasource_id, updated_form_data
        )

        with MetricsTimer("explore.add_metric"):
            return self.client.get_chart_data(query_context)

    def add_groupby(
        self, datasource_id: int, current_form_data: dict, groupby_column: str
    ) -> dict | None:
        """
        Scenario: Add GROUP BY dimension in explore
        """
        updated_form_data = current_form_data.copy()

        if "groupby" not in updated_form_data:
            updated_form_data["groupby"] = []

        updated_form_data["groupby"].append(groupby_column)

        query_context = self._build_query_context_from_form_data(
            datasource_id, updated_form_data
        )

        with MetricsTimer("explore.add_groupby"):
            return self.client.get_chart_data(query_context)

    def _generate_sample_form_data(self, datasource_id: int) -> dict:
        """Generate sample form data for explore."""
        return {
            "datasource": f"{datasource_id}__table",
            "viz_type": random_viz_type(),
            "time_range": "Last week",
            "metrics": [{"expressionType": "SQL", "sqlExpression": "COUNT(*)"}],
            "groupby": [],
            "adhoc_filters": [],
            "row_limit": 1000,
        }

    def _build_query_context_from_form_data(
        self, datasource_id: int, form_data: dict
    ) -> dict:
        """Build query context from explore form data."""
        return {
            "datasource": {"id": datasource_id, "type": "table"},
            "queries": [
                {
                    "metrics": form_data.get("metrics", []),
                    "groupby": form_data.get("groupby", []),
                    "filters": form_data.get("adhoc_filters", []),
                    "time_range": form_data.get("time_range", "Last week"),
                    "row_limit": form_data.get("row_limit", 1000),
                    "order_desc": True,
                }
            ],
            "result_format": "json",
            "result_type": "full",
        }
