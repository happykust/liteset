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
Standalone Superset API client for fixtures setup.
Does not depend on Locust.
"""

import json
import logging

import requests

logger = logging.getLogger(__name__)


class SupersetClient:
    """
    Standalone HTTP client for Superset API.
    Used for setting up fixtures outside of Locust.
    """

    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.session = requests.Session()
        self.csrf_token: str | None = None
        self.access_token: str | None = None

    def login(self) -> bool:
        """Login to Superset and get CSRF token."""
        # Try form-based login first
        try:
            # Get CSRF token from login page
            resp = self.session.get(f"{self.base_url}/login/")
            if resp.status_code == 200:
                # Extract CSRF token from page
                import re

                match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', resp.text)
                if match:
                    csrf = match.group(1)
                    # Submit login form
                    login_resp = self.session.post(
                        f"{self.base_url}/login/",
                        data={
                            "username": self.username,
                            "password": self.password,
                            "csrf_token": csrf,
                        },
                        allow_redirects=True,
                    )
                    if login_resp.status_code == 200 and "/login" not in login_resp.url:
                        logger.info("Form login successful")
                        self._refresh_csrf()
                        return True
        except Exception as e:
            logger.warning(f"Form login failed: {e}")

        # Try API login
        try:
            resp = self.session.post(
                f"{self.base_url}/api/v1/security/login",
                json={
                    "username": self.username,
                    "password": self.password,
                    "provider": "db",
                    "refresh": True,
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                self.access_token = data.get("access_token")
                if self.access_token:
                    self.session.headers["Authorization"] = (
                        f"Bearer {self.access_token}"
                    )
                    logger.info("API login successful")
                    self._refresh_csrf()
                    return True
        except Exception as e:
            logger.error(f"API login failed: {e}")

        return False

    def _refresh_csrf(self) -> None:
        """Get fresh CSRF token."""
        try:
            resp = self.session.get(f"{self.base_url}/api/v1/security/csrf_token/")
            if resp.status_code == 200:
                self.csrf_token = resp.json().get("result")
                if self.csrf_token:
                    self.session.headers["X-CSRFToken"] = self.csrf_token
        except Exception as e:
            logger.warning(f"Failed to get CSRF token: {e}")

    def get(self, endpoint: str, params: dict | None = None) -> dict | None:
        """GET request."""
        try:
            resp = self.session.get(f"{self.base_url}{endpoint}", params=params)
            if resp.status_code == 200:
                return resp.json()
            else:
                logger.error(
                    f"GET {endpoint} failed: {resp.status_code} - {resp.text[:200]}"
                )
        except Exception as e:
            logger.error(f"GET {endpoint} error: {e}")
        return None

    def post(self, endpoint: str, json_data: dict | None = None) -> dict | None:
        """POST request."""
        try:
            resp = self.session.post(
                f"{self.base_url}{endpoint}",
                json=json_data,
                headers={"Content-Type": "application/json"},
            )
            if resp.status_code in (200, 201):
                return resp.json()
            else:
                logger.error(
                    f"POST {endpoint} failed: {resp.status_code} - {resp.text[:500]}"
                )
        except Exception as e:
            logger.error(f"POST {endpoint} error: {e}")
        return None

    def put(self, endpoint: str, json_data: dict | None = None) -> dict | None:
        """PUT request."""
        try:
            resp = self.session.put(
                f"{self.base_url}{endpoint}",
                json=json_data,
                headers={"Content-Type": "application/json"},
            )
            if resp.status_code == 200:
                return resp.json()
            else:
                logger.error(
                    f"PUT {endpoint} failed: {resp.status_code} - {resp.text[:200]}"
                )
        except Exception as e:
            logger.error(f"PUT {endpoint} error: {e}")
        return None

    def delete(self, endpoint: str) -> bool:
        """DELETE request."""
        try:
            resp = self.session.delete(f"{self.base_url}{endpoint}")
            return resp.status_code in (200, 204)
        except Exception as e:
            logger.error(f"DELETE {endpoint} error: {e}")
        return False

    # Convenience methods

    def create_database(
        self, name: str, sqlalchemy_uri: str, extra: dict | None = None
    ) -> dict | None:
        """Create database connection."""
        payload = {
            "database_name": name,
            "sqlalchemy_uri": sqlalchemy_uri,
            "expose_in_sqllab": True,
            "allow_ctas": True,
            "allow_cvas": True,
            "allow_dml": True,
            "allow_run_async": True,
            "extra": json.dumps(extra or {}),
        }
        return self.post("/api/v1/database/", payload)

    def create_dataset(
        self, database_id: int, table_name: str, schema: str | None = None
    ) -> dict | None:
        """Create dataset from table."""
        payload = {
            "database": database_id,
            "table_name": table_name,
            "schema": schema or "",
        }
        return self.post("/api/v1/dataset/", payload)

    def create_chart(
        self,
        slice_name: str,
        viz_type: str,
        datasource_id: int,
        datasource_type: str = "table",
        params: dict | None = None,
    ) -> dict | None:
        """Create a chart."""
        payload = {
            "slice_name": slice_name,
            "viz_type": viz_type,
            "datasource_id": datasource_id,
            "datasource_type": datasource_type,
            "params": json.dumps(params or {}),
        }
        return self.post("/api/v1/chart/", payload)

    def create_dashboard(
        self, title: str, slug: str | None = None, published: bool = True
    ) -> dict | None:
        """Create a dashboard."""
        payload = {
            "dashboard_title": title,
            "slug": slug,
            "published": published,
        }
        return self.post("/api/v1/dashboard/", payload)

    def add_charts_to_dashboard(
        self, dashboard_id: int, chart_ids: list[int]
    ) -> dict | None:
        """Add charts to dashboard via position_json."""
        # Get current dashboard
        dashboard = self.get(f"/api/v1/dashboard/{dashboard_id}")
        if not dashboard:
            return None

        # Build position JSON with charts in grid
        positions = {
            "DASHBOARD_VERSION_KEY": "v2",
            "ROOT_ID": {"type": "ROOT", "id": "ROOT_ID", "children": ["GRID_ID"]},
            "GRID_ID": {
                "type": "GRID",
                "id": "GRID_ID",
                "children": [],
                "parents": ["ROOT_ID"],
            },
            "HEADER_ID": {
                "type": "HEADER",
                "id": "HEADER_ID",
                "meta": {"text": dashboard["result"]["dashboard_title"]},
            },
        }

        row_id = 0
        for i, chart_id in enumerate(chart_ids):
            row_key = f"ROW-{row_id}"
            chart_key = f"CHART-{chart_id}"

            # Create row if needed (4 charts per row)
            if i % 4 == 0:
                row_id += 1
                row_key = f"ROW-{row_id}"
                positions[row_key] = {
                    "type": "ROW",
                    "id": row_key,
                    "children": [],
                    "parents": ["ROOT_ID", "GRID_ID"],
                    "meta": {"background": "BACKGROUND_TRANSPARENT"},
                }
                positions["GRID_ID"]["children"].append(row_key)

            # Add chart to row
            positions[chart_key] = {
                "type": "CHART",
                "id": chart_key,
                "children": [],
                "parents": ["ROOT_ID", "GRID_ID", row_key],
                "meta": {
                    "width": 3,
                    "height": 50,
                    "chartId": chart_id,
                    "sliceName": f"Chart {chart_id}",
                },
            }
            positions[row_key]["children"].append(chart_key)  # type: ignore[index]

        return self.put(
            f"/api/v1/dashboard/{dashboard_id}",
            {"position_json": json.dumps(positions)},
        )

    def get_databases(self) -> list[dict]:
        """Get all databases."""
        result = self.get("/api/v1/database/", {"q": json.dumps({"page_size": 100})})
        return result.get("result", []) if result else []

    def get_datasets(self) -> list[dict]:
        """Get all datasets."""
        result = self.get("/api/v1/dataset/", {"q": json.dumps({"page_size": 500})})
        return result.get("result", []) if result else []

    def get_charts(self) -> list[dict]:
        """Get all charts."""
        result = self.get("/api/v1/chart/", {"q": json.dumps({"page_size": 500})})
        return result.get("result", []) if result else []

    def get_dashboards(self) -> list[dict]:
        """Get all dashboards."""
        result = self.get("/api/v1/dashboard/", {"q": json.dumps({"page_size": 100})})
        return result.get("result", []) if result else []

    def refresh_dataset(self, dataset_id: int) -> bool:
        """Refresh dataset columns."""
        result = self.put(f"/api/v1/dataset/{dataset_id}/refresh")
        return result is not None
