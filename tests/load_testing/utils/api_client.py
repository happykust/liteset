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
Superset API client for load testing with authentication and session management.
"""

import json
import logging
import time
from typing import Any
from urllib.parse import urljoin

from locust import HttpUser

logger = logging.getLogger(__name__)


class SupersetAPIClient:
    """
    API client wrapper for Superset with authentication,
    CSRF token handling, and convenience methods.
    """

    def __init__(self, user: HttpUser, base_url: str):
        self.user = user
        self.client = user.client
        self.base_url = base_url
        self.csrf_token: str | None = None
        self.access_token: str | None = None
        self.refresh_token: str | None = None
        self.is_authenticated = False
        self._session_cookies: dict[str, str] = {}

    def _get_url(self, endpoint: str) -> str:
        """Build full URL for endpoint."""
        if endpoint.startswith("http"):
            return endpoint
        return urljoin(self.base_url, endpoint)

    def _get_headers(
        self, extra_headers: dict[str, str] | None = None
    ) -> dict[str, str]:
        """Get headers with CSRF token and authentication."""
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.csrf_token:
            headers["X-CSRFToken"] = self.csrf_token
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        if extra_headers:
            headers.update(extra_headers)
        return headers

    def login(self, username: str, password: str) -> bool:
        """
        Authenticate with Superset using form-based login.
        Returns True if successful.
        """
        # First, get the login page to obtain CSRF token
        with self.client.get(
            "/login/", name="/login [GET]", catch_response=True
        ) as response:
            if response.status_code != 200:
                response.failure(f"Failed to get login page: {response.status_code}")
                return False

        # Get CSRF token from security endpoint
        self._fetch_csrf_token()

        # Perform login
        login_data = {
            "username": username,
            "password": password,
        }

        headers = self._get_headers()
        headers["Content-Type"] = "application/x-www-form-urlencoded"

        with self.client.post(
            "/login/",
            data=login_data,
            headers={"X-CSRFToken": self.csrf_token} if self.csrf_token else {},
            name="/login [POST]",
            catch_response=True,
            allow_redirects=True,
        ) as response:
            if response.status_code in (200, 302):
                self.is_authenticated = True
                # Refresh CSRF token after login
                self._fetch_csrf_token()
                response.success()
                return True
            else:
                response.failure(f"Login failed: {response.status_code}")
                return False

    def login_api(self, username: str, password: str) -> bool:
        """
        Authenticate using the REST API (JWT-based).
        Returns True if successful.
        """
        login_payload = {
            "username": username,
            "password": password,
            "provider": "db",
            "refresh": True,
        }

        with self.client.post(
            "/api/v1/security/login",
            json=login_payload,
            name="/api/v1/security/login",
            catch_response=True,
        ) as response:
            if response.status_code == 200:
                data = response.json()
                self.access_token = data.get("access_token")
                self.refresh_token = data.get("refresh_token")
                self.is_authenticated = True
                # Fetch CSRF token for API calls
                self._fetch_csrf_token()
                response.success()
                return True
            else:
                response.failure(f"API login failed: {response.status_code}")
                return False

    def _fetch_csrf_token(self) -> str | None:
        """Fetch CSRF token from security endpoint."""
        with self.client.get(
            "/api/v1/security/csrf_token/",
            headers=self._get_headers(),
            name="/api/v1/security/csrf_token",
            catch_response=True,
        ) as response:
            if response.status_code == 200:
                data = response.json()
                self.csrf_token = data.get("result")
                response.success()
                return self.csrf_token
            else:
                response.failure(f"Failed to get CSRF token: {response.status_code}")
                return None

    def refresh_csrf(self) -> str | None:
        """Refresh CSRF token."""
        return self._fetch_csrf_token()

    def get(
        self,
        endpoint: str,
        name: str | None = None,
        params: dict | None = None,
        **kwargs,
    ) -> Any:
        """Make GET request to API endpoint."""
        url = self._get_url(endpoint)
        request_name = name or endpoint

        with self.client.get(
            url,
            headers=self._get_headers(),
            params=params,
            name=request_name,
            catch_response=True,
            **kwargs,
        ) as response:
            return self._handle_response(response, request_name)

    def post(
        self,
        endpoint: str,
        name: str | None = None,
        data: dict | None = None,
        json_data: dict | None = None,
        **kwargs,
    ) -> Any:
        """Make POST request to API endpoint."""
        url = self._get_url(endpoint)
        request_name = name or endpoint

        with self.client.post(
            url,
            headers=self._get_headers(),
            data=data,
            json=json_data,
            name=request_name,
            catch_response=True,
            **kwargs,
        ) as response:
            return self._handle_response(response, request_name)

    def put(
        self,
        endpoint: str,
        name: str | None = None,
        data: dict | None = None,
        json_data: dict | None = None,
        **kwargs,
    ) -> Any:
        """Make PUT request to API endpoint."""
        url = self._get_url(endpoint)
        request_name = name or endpoint

        with self.client.put(
            url,
            headers=self._get_headers(),
            data=data,
            json=json_data,
            name=request_name,
            catch_response=True,
            **kwargs,
        ) as response:
            return self._handle_response(response, request_name)

    def delete(self, endpoint: str, name: str | None = None, **kwargs) -> Any:
        """Make DELETE request to API endpoint."""
        url = self._get_url(endpoint)
        request_name = name or endpoint

        with self.client.delete(
            url,
            headers=self._get_headers(),
            name=request_name,
            catch_response=True,
            **kwargs,
        ) as response:
            return self._handle_response(response, request_name)

    def _handle_response(self, response, request_name: str) -> Any:
        """Handle API response with error checking."""
        try:
            if response.status_code == 401:
                # Token expired, try to refresh
                if self.refresh_token:
                    self._refresh_access_token()
                response.failure(f"Unauthorized: {request_name}")
                return None

            if response.status_code == 403:
                response.failure(f"Forbidden: {request_name}")
                return None

            if response.status_code == 404:
                response.failure(f"Not found: {request_name}")
                return None

            if response.status_code >= 500:
                response.failure(f"Server error {response.status_code}: {request_name}")
                return None

            if response.status_code >= 400:
                response.failure(f"Client error {response.status_code}: {request_name}")
                return None

            # Success
            response.success()

            # Try to parse JSON response
            if response.headers.get("Content-Type", "").startswith("application/json"):
                return response.json()
            return response.text

        except json.JSONDecodeError:
            return response.text
        except Exception as e:
            logger.error(f"Error handling response for {request_name}: {e}")
            response.failure(str(e))
            return None

    def _refresh_access_token(self) -> bool:
        """Refresh JWT access token."""
        if not self.refresh_token:
            return False

        with self.client.post(
            "/api/v1/security/refresh",
            headers={"Authorization": f"Bearer {self.refresh_token}"},
            name="/api/v1/security/refresh",
            catch_response=True,
        ) as response:
            if response.status_code == 200:
                data = response.json()
                self.access_token = data.get("access_token")
                response.success()
                return True
            else:
                response.failure("Failed to refresh token")
                return False

    # Convenience methods for common API operations

    def get_dashboards(
        self, page: int = 0, page_size: int = 25, filters: list | None = None
    ) -> dict | None:
        """Get list of dashboards."""
        params = {
            "q": json.dumps(
                {"page": page, "page_size": page_size, "filters": filters or []}
            )
        }
        return self.get(
            "/api/v1/dashboard/", name="GET /api/v1/dashboard/", params=params
        )

    def get_dashboard(self, id_or_slug: int | str) -> dict | None:
        """Get single dashboard by ID or slug."""
        return self.get(
            f"/api/v1/dashboard/{id_or_slug}", name="GET /api/v1/dashboard/<id>"
        )

    def get_dashboard_charts(self, dashboard_id: int) -> dict | None:
        """Get charts for a dashboard."""
        return self.get(
            f"/api/v1/dashboard/{dashboard_id}/charts",
            name="GET /api/v1/dashboard/<id>/charts",
        )

    def get_charts(
        self, page: int = 0, page_size: int = 25, filters: list | None = None
    ) -> dict | None:
        """Get list of charts."""
        params = {
            "q": json.dumps(
                {"page": page, "page_size": page_size, "filters": filters or []}
            )
        }
        return self.get("/api/v1/chart/", name="GET /api/v1/chart/", params=params)

    def get_chart(self, chart_id: int) -> dict | None:
        """Get single chart by ID."""
        return self.get(f"/api/v1/chart/{chart_id}", name="GET /api/v1/chart/<id>")

    def get_chart_data(self, query_context: dict) -> dict | None:
        """Execute chart data query."""
        return self.post(
            "/api/v1/chart/data",
            name="POST /api/v1/chart/data",
            json_data=query_context,
        )

    def get_chart_data_cached(
        self, query_context: dict, force_cached: bool = True
    ) -> dict | None:
        """Get chart data with cache preference."""
        if force_cached:
            query_context["result_format"] = "json"
            query_context["result_type"] = "full"
            # Don't force refresh
            for query in query_context.get("queries", []):
                query["force"] = False
        return self.get_chart_data(query_context)

    def get_datasets(
        self, page: int = 0, page_size: int = 25, filters: list | None = None
    ) -> dict | None:
        """Get list of datasets."""
        params = {
            "q": json.dumps(
                {"page": page, "page_size": page_size, "filters": filters or []}
            )
        }
        return self.get("/api/v1/dataset/", name="GET /api/v1/dataset/", params=params)

    def get_dataset(self, dataset_id: int) -> dict | None:
        """Get single dataset by ID."""
        return self.get(
            f"/api/v1/dataset/{dataset_id}", name="GET /api/v1/dataset/<id>"
        )

    def get_databases(self, page: int = 0, page_size: int = 25) -> dict | None:
        """Get list of databases."""
        params = {"q": json.dumps({"page": page, "page_size": page_size})}
        return self.get(
            "/api/v1/database/", name="GET /api/v1/database/", params=params
        )

    def get_database_schemas(self, database_id: int) -> dict | None:
        """Get schemas for a database."""
        return self.get(
            f"/api/v1/database/{database_id}/schemas/",
            name="GET /api/v1/database/<id>/schemas",
        )

    def get_database_tables(
        self, database_id: int, schema: str, force_refresh: bool = False
    ) -> dict | None:
        """Get tables for a database schema."""
        params = {"q": json.dumps({"schema_name": schema, "force": force_refresh})}
        return self.get(
            f"/api/v1/database/{database_id}/tables/",
            name="GET /api/v1/database/<id>/tables",
            params=params,
        )

    def execute_sql(
        self,
        database_id: int,
        sql: str,
        schema: str | None = None,
        run_async: bool = False,
        select_as_cta: bool = False,
        ctas_method: str = "TABLE",
        tmp_table_name: str | None = None,
    ) -> dict | None:
        """Execute SQL query via SQL Lab."""
        payload = {
            "database_id": database_id,
            "sql": sql,
            "schema": schema,
            "runAsync": run_async,
            "select_as_cta": select_as_cta,
            "ctas_method": ctas_method,
        }
        if tmp_table_name:
            payload["tmp_table_name"] = tmp_table_name

        return self.post(
            "/api/v1/sqllab/execute/",
            name="POST /api/v1/sqllab/execute",
            json_data=payload,
        )

    def get_sql_results(self, key: str) -> dict | None:
        """Get SQL query results by key."""
        params = {"q": json.dumps({"key": key})}
        return self.get(
            "/api/v1/sqllab/results/", name="GET /api/v1/sqllab/results", params=params
        )

    def poll_async_query(
        self, query_id: str, max_attempts: int = 60, poll_interval: float = 1.0
    ) -> dict | None:
        """Poll for async query completion."""
        for _ in range(max_attempts):
            result = self.get(
                f"/api/v1/query/{query_id}", name="GET /api/v1/query/<id> [poll]"
            )
            if result and result.get("result", {}).get("status") in (
                "success",
                "failed",
                "stopped",
            ):
                return result
            time.sleep(poll_interval)
        return None

    def get_explore_form_data(self, key: str) -> dict | None:
        """Get explore form data by key."""
        return self.get(
            f"/api/v1/explore/form_data/{key}",
            name="GET /api/v1/explore/form_data/<key>",
        )

    def save_explore_form_data(
        self,
        datasource_id: int,
        datasource_type: str,
        form_data: dict,
        chart_id: int | None = None,
    ) -> dict | None:
        """Save explore form data."""
        payload = {
            "datasource_id": datasource_id,
            "datasource_type": datasource_type,
            "form_data": json.dumps(form_data),
        }
        if chart_id:
            payload["chart_id"] = chart_id

        return self.post(
            "/api/v1/explore/form_data",
            name="POST /api/v1/explore/form_data",
            json_data=payload,
        )

    def get_tags(self, page: int = 0, page_size: int = 25) -> dict | None:
        """Get list of tags."""
        params = {"q": json.dumps({"page": page, "page_size": page_size})}
        return self.get("/api/v1/tag/", name="GET /api/v1/tag/", params=params)

    def get_queries(self, page: int = 0, page_size: int = 25) -> dict | None:
        """Get list of queries."""
        params = {"q": json.dumps({"page": page, "page_size": page_size})}
        return self.get("/api/v1/query/", name="GET /api/v1/query/", params=params)

    def get_saved_queries(self, page: int = 0, page_size: int = 25) -> dict | None:
        """Get list of saved queries."""
        params = {"q": json.dumps({"page": page, "page_size": page_size})}
        return self.get(
            "/api/v1/saved_query/", name="GET /api/v1/saved_query/", params=params
        )

    def add_favorite(self, class_name: str, obj_id: int) -> dict | None:
        """Add item to favorites."""
        endpoint_map = {
            "Dashboard": "/api/v1/dashboard",
            "Slice": "/api/v1/chart",
        }
        base = endpoint_map.get(class_name, "/api/v1/dashboard")
        return self.post(
            f"{base}/{obj_id}/favorites/", name=f"POST {base}/<id>/favorites"
        )

    def remove_favorite(self, class_name: str, obj_id: int) -> dict | None:
        """Remove item from favorites."""
        endpoint_map = {
            "Dashboard": "/api/v1/dashboard",
            "Slice": "/api/v1/chart",
        }
        base = endpoint_map.get(class_name, "/api/v1/dashboard")
        return self.delete(
            f"{base}/{obj_id}/favorites/", name=f"DELETE {base}/<id>/favorites"
        )
