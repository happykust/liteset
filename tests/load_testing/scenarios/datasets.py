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
Dataset scenarios for load testing.
"""

import json
import logging
from typing import Any, TYPE_CHECKING

from ..utils.helpers import random_choice, random_string
from ..utils.metrics import get_metrics_collector, MetricsTimer

if TYPE_CHECKING:
    from ..utils.api_client import SupersetAPIClient

logger = logging.getLogger(__name__)


class DatasetScenarios:
    """Dataset-related load testing scenarios."""

    def __init__(self, client: "SupersetAPIClient"):
        self.client = client
        self.metrics = get_metrics_collector()
        self._dataset_cache: list[dict] = []

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

    def list_datasets(
        self, page: int = 0, page_size: int = 25, filters: list | None = None
    ) -> dict | None:
        """
        Scenario: List datasets with pagination
        """
        with MetricsTimer("dataset.list"):
            result = self.client.get_datasets(
                page=page, page_size=page_size, filters=filters
            )
            if result and "result" in result:
                self._dataset_cache = result["result"]
            return result

    def get_dataset(self, dataset_id: int | None = None) -> dict | None:
        """
        Scenario: Get single dataset details
        """
        if dataset_id is None:
            dataset = self._get_random_dataset()
            dataset_id = dataset.get("id") if dataset else None

        if dataset_id is None:
            return None

        with MetricsTimer("dataset.get"):
            return self.client.get_dataset(dataset_id)

    def get_dataset_samples(
        self, dataset_id: int | None = None, force: bool = False
    ) -> dict | None:
        """
        Scenario: Get sample data from dataset
        """
        if dataset_id is None:
            dataset = self._get_random_dataset()
            dataset_id = dataset.get("id") if dataset else None

        if dataset_id is None:
            return None

        payload = {"datasource": {"id": dataset_id, "type": "table"}, "force": force}

        with MetricsTimer("dataset.samples"):
            return self.client.post(
                "/api/v1/datasource/samples",
                name="POST /api/v1/datasource/samples",
                json_data=payload,
            )

    def get_dataset_columns(self, dataset_id: int | None = None) -> dict | None:
        """
        Scenario: Get dataset columns metadata
        """
        if dataset_id is None:
            dataset = self._get_random_dataset()
            dataset_id = dataset.get("id") if dataset else None

        if dataset_id is None:
            return None

        with MetricsTimer("dataset.columns"):
            return self.client.get(
                f"/api/v1/dataset/{dataset_id}/columns",
                name="GET /api/v1/dataset/<id>/columns",
            )

    def get_dataset_metrics(self, dataset_id: int | None = None) -> dict | None:
        """
        Scenario: Get dataset metrics
        """
        if dataset_id is None:
            dataset = self._get_random_dataset()
            dataset_id = dataset.get("id") if dataset else None

        if dataset_id is None:
            return None

        with MetricsTimer("dataset.metrics"):
            return self.client.get(
                f"/api/v1/dataset/{dataset_id}/metrics",
                name="GET /api/v1/dataset/<id>/metrics",
            )

    def get_related_objects(self, dataset_id: int | None = None) -> dict | None:
        """
        Scenario: Get objects related to dataset (charts, dashboards)
        """
        if dataset_id is None:
            dataset = self._get_random_dataset()
            dataset_id = dataset.get("id") if dataset else None

        if dataset_id is None:
            return None

        with MetricsTimer("dataset.related_objects"):
            return self.client.get(
                f"/api/v1/dataset/{dataset_id}/related_objects",
                name="GET /api/v1/dataset/<id>/related_objects",
            )

    def get_related_owners(
        self, page: int = 0, page_size: int = 25, filter_str: str | None = None
    ) -> dict | None:
        """
        Scenario: Get related owners for dataset form
        """
        params = {
            "q": json.dumps(
                {"page": page, "page_size": page_size, "filter": filter_str or ""}
            )
        }

        with MetricsTimer("dataset.related_owners"):
            return self.client.get(
                "/api/v1/dataset/related/owners",
                name="GET /api/v1/dataset/related/owners",
                params=params,
            )

    def get_related_databases(self, page: int = 0, page_size: int = 25) -> dict | None:
        """
        Scenario: Get related databases for dataset form
        """
        params = {
            "q": json.dumps(
                {
                    "page": page,
                    "page_size": page_size,
                }
            )
        }

        with MetricsTimer("dataset.related_databases"):
            return self.client.get(
                "/api/v1/dataset/related/database",
                name="GET /api/v1/dataset/related/database",
                params=params,
            )

    def refresh_dataset(self, dataset_id: int | None = None) -> dict | None:
        """
        Scenario: Refresh dataset schema from database
        """
        if dataset_id is None:
            dataset = self._get_random_dataset()
            dataset_id = dataset.get("id") if dataset else None

        if dataset_id is None:
            return None

        with MetricsTimer("dataset.refresh"):
            return self.client.put(
                f"/api/v1/dataset/{dataset_id}/refresh",
                name="PUT /api/v1/dataset/<id>/refresh",
                json_data={},
            )

    def create_dataset(
        self,
        database_id: int,
        table_name: str,
        schema: str | None = None,
        owners: list[int] | None = None,
    ) -> dict | None:
        """
        Scenario: Create new dataset from table
        """
        payload = {
            "database": database_id,
            "table_name": table_name,
            "schema": schema,
            "owners": owners or [],
        }

        with MetricsTimer("dataset.create"):
            return self.client.post(
                "/api/v1/dataset/", name="POST /api/v1/dataset", json_data=payload
            )

    def create_virtual_dataset(
        self,
        database_id: int,
        sql: str,
        table_name: str | None = None,
        schema: str | None = None,
    ) -> dict | None:
        """
        Scenario: Create virtual dataset from SQL
        """
        if table_name is None:
            table_name = f"virtual_dataset_{random_string(8)}"

        payload = {
            "database": database_id,
            "table_name": table_name,
            "schema": schema,
            "sql": sql,
        }

        with MetricsTimer("dataset.create_virtual"):
            return self.client.post(
                "/api/v1/dataset/",
                name="POST /api/v1/dataset [virtual]",
                json_data=payload,
            )

    def update_dataset(self, dataset_id: int, updates: dict[str, Any]) -> dict | None:
        """
        Scenario: Update dataset properties
        """
        with MetricsTimer("dataset.update"):
            return self.client.put(
                f"/api/v1/dataset/{dataset_id}",
                name="PUT /api/v1/dataset/<id>",
                json_data=updates,
            )

    def delete_dataset(self, dataset_id: int) -> dict | None:
        """
        Scenario: Delete dataset
        """
        with MetricsTimer("dataset.delete"):
            return self.client.delete(
                f"/api/v1/dataset/{dataset_id}", name="DELETE /api/v1/dataset/<id>"
            )

    def export_datasets(self, dataset_ids: list[int]) -> bytes | None:
        """
        Scenario: Export datasets
        """
        params = {"q": json.dumps(dataset_ids)}

        with MetricsTimer("dataset.export"):
            return self.client.get(
                "/api/v1/dataset/export/",
                name="GET /api/v1/dataset/export",
                params=params,
            )

    def search_datasets(self, query: str) -> dict | None:
        """
        Scenario: Search datasets by name
        """
        filters = [{"col": "table_name", "opr": "ct", "value": query}]

        with MetricsTimer("dataset.search"):
            return self.list_datasets(filters=filters)

    def filter_by_database(self, database_id: int) -> dict | None:
        """
        Scenario: Filter datasets by database
        """
        filters = [{"col": "database", "opr": "rel_o_m", "value": database_id}]

        with MetricsTimer("dataset.filter_by_database"):
            return self.list_datasets(filters=filters)

    def get_distinct_schemas(self) -> dict | None:
        """
        Scenario: Get distinct schema values
        """
        with MetricsTimer("dataset.distinct_schemas"):
            return self.client.get(
                "/api/v1/dataset/distinct/schema",
                name="GET /api/v1/dataset/distinct/schema",
            )
