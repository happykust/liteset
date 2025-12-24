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
Database scenarios for load testing.
"""

import json
import logging
from typing import Any, TYPE_CHECKING

from ..utils.helpers import random_choice, random_string
from ..utils.metrics import get_metrics_collector, MetricsTimer

if TYPE_CHECKING:
    from ..utils.api_client import SupersetAPIClient

logger = logging.getLogger(__name__)


class DatabaseScenarios:
    """Database-related load testing scenarios."""

    def __init__(self, client: "SupersetAPIClient"):
        self.client = client
        self.metrics = get_metrics_collector()
        self._database_cache: list[dict] = []
        self._schema_cache: dict[int, list[str]] = {}
        self._table_cache: dict[str, list[dict]] = {}

    def _refresh_database_cache(self) -> None:
        """Refresh local cache of databases."""
        result = self.client.get_databases(page_size=100)
        if result and "result" in result:
            self._database_cache = result["result"]

    def _get_random_database(self) -> dict | None:
        """Get random database from cache."""
        if not self._database_cache:
            self._refresh_database_cache()
        if self._database_cache:
            return random_choice(self._database_cache)
        return None

    def list_databases(self, page: int = 0, page_size: int = 25) -> dict | None:
        """
        Scenario: List databases with pagination
        """
        with MetricsTimer("database.list"):
            result = self.client.get_databases(page=page, page_size=page_size)
            if result and "result" in result:
                self._database_cache = result["result"]
            return result

    def get_database(self, database_id: int | None = None) -> dict | None:
        """
        Scenario: Get single database details
        """
        if database_id is None:
            db = self._get_random_database()
            database_id = db.get("id") if db else None

        if database_id is None:
            return None

        with MetricsTimer("database.get"):
            return self.client.get(
                f"/api/v1/database/{database_id}", name="GET /api/v1/database/<id>"
            )

    def get_schemas(self, database_id: int | None = None) -> dict | None:
        """
        Scenario: Get database schemas
        """
        if database_id is None:
            db = self._get_random_database()
            database_id = db.get("id") if db else None

        if database_id is None:
            return None

        with MetricsTimer("database.schemas"):
            result = self.client.get_database_schemas(database_id)
            if result and "result" in result:
                self._schema_cache[database_id] = result["result"]
            return result

    def get_tables(
        self,
        database_id: int | None = None,
        schema: str | None = None,
        force_refresh: bool = False,
    ) -> dict | None:
        """
        Scenario: Get tables in database schema
        """
        if database_id is None:
            db = self._get_random_database()
            database_id = db.get("id") if db else None

        if database_id is None:
            return None

        if schema is None:
            # Try to get a schema from cache
            schemas = self._schema_cache.get(database_id, [])
            if not schemas:
                self.get_schemas(database_id)
                schemas = self._schema_cache.get(database_id, [])
            if schemas:
                schema = random_choice(schemas)
            else:
                schema = "public"

        with MetricsTimer("database.tables"):
            result = self.client.get_database_tables(
                database_id, schema, force_refresh=force_refresh
            )
            if result and "result" in result:
                cache_key = f"{database_id}:{schema}"
                self._table_cache[cache_key] = result["result"]
            return result

    def get_table_metadata(
        self,
        database_id: int | None = None,
        table_name: str | None = None,
        schema: str | None = None,
    ) -> dict | None:
        """
        Scenario: Get table metadata (columns, indexes)
        """
        if database_id is None:
            db = self._get_random_database()
            database_id = db.get("id") if db else None

        if database_id is None:
            return None

        if schema is None:
            schema = "public"

        if table_name is None:
            # Get a table from cache
            cache_key = f"{database_id}:{schema}"
            tables = self._table_cache.get(cache_key, [])
            if not tables:
                self.get_tables(database_id, schema)
                tables = self._table_cache.get(cache_key, [])
            if tables:
                table = random_choice(tables)
                table_name = table.get("value") if isinstance(table, dict) else table
            else:
                return None

        params = {"q": json.dumps({"schema_name": schema, "table_name": table_name})}

        with MetricsTimer("database.table_metadata"):
            return self.client.get(
                f"/api/v1/database/{database_id}/table_metadata/",
                name="GET /api/v1/database/<id>/table_metadata",
                params=params,
            )

    def get_table_extra_metadata(
        self, database_id: int, table_name: str, schema: str | None = None
    ) -> dict | None:
        """
        Scenario: Get extra table metadata
        """
        params = {
            "q": json.dumps(
                {"schema_name": schema or "public", "table_name": table_name}
            )
        }

        with MetricsTimer("database.table_extra_metadata"):
            return self.client.get(
                f"/api/v1/database/{database_id}/table_metadata/extra/",
                name="GET /api/v1/database/<id>/table_metadata/extra",
                params=params,
            )

    def get_select_star(
        self, database_id: int, table_name: str, schema: str | None = None
    ) -> dict | None:
        """
        Scenario: Get SELECT * query for table
        """
        schema_param = schema or "public"

        with MetricsTimer("database.select_star"):
            return self.client.get(
                f"/api/v1/database/{database_id}/select_star/{table_name}/{schema_param}/",
                name="GET /api/v1/database/<id>/select_star/<table>/<schema>",
            )

    def get_catalogs(self, database_id: int | None = None) -> dict | None:
        """
        Scenario: Get database catalogs (for multi-catalog databases)
        """
        if database_id is None:
            db = self._get_random_database()
            database_id = db.get("id") if db else None

        if database_id is None:
            return None

        with MetricsTimer("database.catalogs"):
            return self.client.get(
                f"/api/v1/database/{database_id}/catalogs/",
                name="GET /api/v1/database/<id>/catalogs",
            )

    def get_function_names(self, database_id: int | None = None) -> dict | None:
        """
        Scenario: Get database function names (for SQL autocomplete)
        """
        if database_id is None:
            db = self._get_random_database()
            database_id = db.get("id") if db else None

        if database_id is None:
            return None

        with MetricsTimer("database.function_names"):
            return self.client.get(
                f"/api/v1/database/{database_id}/function_names/",
                name="GET /api/v1/database/<id>/function_names",
            )

    def test_connection(
        self, sqlalchemy_uri: str, database_name: str | None = None
    ) -> dict | None:
        """
        Scenario: Test database connection
        """
        if database_name is None:
            database_name = f"test_connection_{random_string(6)}"

        payload = {
            "database_name": database_name,
            "sqlalchemy_uri": sqlalchemy_uri,
            "impersonate_user": False,
        }

        with MetricsTimer("database.test_connection"):
            return self.client.post(
                "/api/v1/database/test_connection/",
                name="POST /api/v1/database/test_connection",
                json_data=payload,
            )

    def validate_parameters(
        self, engine: str, parameters: dict[str, Any]
    ) -> dict | None:
        """
        Scenario: Validate database connection parameters
        """
        payload = {
            "engine": engine,
            "parameters": parameters,
        }

        with MetricsTimer("database.validate_parameters"):
            return self.client.post(
                "/api/v1/database/validate_parameters/",
                name="POST /api/v1/database/validate_parameters",
                json_data=payload,
            )

    def get_available_engines(self) -> dict | None:
        """
        Scenario: Get available database engines
        """
        with MetricsTimer("database.available_engines"):
            return self.client.get(
                "/api/v1/database/available/", name="GET /api/v1/database/available"
            )

    def create_database(
        self,
        database_name: str,
        sqlalchemy_uri: str,
        expose_in_sqllab: bool = True,
        allow_run_async: bool = True,
    ) -> dict | None:
        """
        Scenario: Create new database connection
        """
        payload = {
            "database_name": database_name,
            "sqlalchemy_uri": sqlalchemy_uri,
            "expose_in_sqllab": expose_in_sqllab,
            "allow_run_async": allow_run_async,
        }

        with MetricsTimer("database.create"):
            return self.client.post(
                "/api/v1/database/", name="POST /api/v1/database", json_data=payload
            )

    def update_database(self, database_id: int, updates: dict[str, Any]) -> dict | None:
        """
        Scenario: Update database settings
        """
        with MetricsTimer("database.update"):
            return self.client.put(
                f"/api/v1/database/{database_id}",
                name="PUT /api/v1/database/<id>",
                json_data=updates,
            )

    def delete_database(self, database_id: int) -> dict | None:
        """
        Scenario: Delete database connection
        """
        with MetricsTimer("database.delete"):
            return self.client.delete(
                f"/api/v1/database/{database_id}", name="DELETE /api/v1/database/<id>"
            )

    def export_databases(self, database_ids: list[int]) -> bytes | None:
        """
        Scenario: Export database configurations
        """
        params = {"q": json.dumps(database_ids)}

        with MetricsTimer("database.export"):
            return self.client.get(
                "/api/v1/database/export/",
                name="GET /api/v1/database/export",
                params=params,
            )

    def get_related_objects(self, database_id: int | None = None) -> dict | None:
        """
        Scenario: Get objects related to database (datasets, charts)
        """
        if database_id is None:
            db = self._get_random_database()
            database_id = db.get("id") if db else None

        if database_id is None:
            return None

        with MetricsTimer("database.related_objects"):
            return self.client.get(
                f"/api/v1/database/{database_id}/related_objects/",
                name="GET /api/v1/database/<id>/related_objects",
            )

    def browse_schema(self, database_id: int | None = None) -> dict:
        """
        Scenario: Full schema browsing workflow
        Simulates user exploring database structure.
        """
        results: dict[str, Any] = {
            "database": None,
            "schemas": None,
            "tables": None,
            "table_metadata": None,
        }

        if database_id is None:
            db = self._get_random_database()
            database_id = db.get("id") if db else None

        if database_id is None:
            return results

        # Get database info
        results["database"] = self.get_database(database_id)

        # Get schemas
        results["schemas"] = self.get_schemas(database_id)

        # Get tables for first schema
        schemas = self._schema_cache.get(database_id, ["public"])
        if schemas:
            schema = schemas[0]
            results["tables"] = self.get_tables(database_id, schema)

            # Get metadata for first table
            cache_key = f"{database_id}:{schema}"
            tables = self._table_cache.get(cache_key, [])
            if tables:
                table = tables[0]
                table_name = table.get("value") if isinstance(table, dict) else table
                results["table_metadata"] = self.get_table_metadata(
                    database_id, table_name, schema
                )

        return results
