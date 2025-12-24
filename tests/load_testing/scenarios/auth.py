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
Authentication scenarios for load testing.
"""

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..utils.api_client import SupersetAPIClient

logger = logging.getLogger(__name__)


class AuthScenarios:
    """Authentication-related load testing scenarios."""

    def __init__(self, client: "SupersetAPIClient"):
        self.client = client

    def login_form(self, username: str, password: str) -> bool:
        """
        Scenario: Form-based login
        Simulates user logging in via the web interface.
        """
        return self.client.login(username, password)

    def login_api(self, username: str, password: str) -> bool:
        """
        Scenario: API-based login (JWT)
        Simulates programmatic login via REST API.
        """
        return self.client.login_api(username, password)

    def get_csrf_token(self) -> str | None:
        """
        Scenario: Fetch CSRF token
        Required before any state-changing operations.
        """
        return self.client.refresh_csrf()

    def refresh_session(self) -> bool:
        """
        Scenario: Refresh session/token
        Simulates keeping session alive.
        """
        # Refresh CSRF token
        csrf = self.client.refresh_csrf()
        if not csrf:
            return False

        # Optionally refresh JWT if using API auth
        if self.client.refresh_token:
            return self.client._refresh_access_token()

        return True

    def get_current_user(self) -> dict | None:
        """
        Scenario: Get current user info
        Fetches current user's profile.
        """
        return self.client.get("/api/v1/me/", name="GET /api/v1/me")

    def get_user_roles(self) -> dict | None:
        """
        Scenario: Get user roles
        Fetches roles and permissions for current user.
        """
        return self.client.get("/api/v1/me/roles/", name="GET /api/v1/me/roles")

    def healthcheck(self) -> bool:
        """
        Scenario: Health check
        Verifies system is responding.
        """
        result = self.client.get("/health", name="GET /health")
        return result is not None

    def get_available_permissions(self) -> dict | None:
        """
        Scenario: Get available permissions
        Fetches list of all permissions in the system.
        """
        return self.client.get(
            "/api/v1/security/permissions/", name="GET /api/v1/security/permissions"
        )


class GuestTokenScenarios:
    """Guest token scenarios for embedded dashboards."""

    def __init__(self, client: "SupersetAPIClient"):
        self.client = client

    def create_guest_token(
        self,
        dashboard_id: int,
        username: str = "guest_user",
        first_name: str = "Guest",
        last_name: str = "User",
        rls_rules: list | None = None,
    ) -> dict | None:
        """
        Scenario: Create guest token for embedded dashboard
        Used for embedding dashboards in external applications.
        """
        payload = {
            "user": {
                "username": username,
                "first_name": first_name,
                "last_name": last_name,
            },
            "resources": [{"type": "dashboard", "id": str(dashboard_id)}],
            "rls": rls_rules or [],
        }

        return self.client.post(
            "/api/v1/security/guest_token/",
            name="POST /api/v1/security/guest_token",
            json_data=payload,
        )

    def create_guest_token_with_rls(
        self, dashboard_id: int, rls_clause: str, dataset_id: int | None = None
    ) -> dict | None:
        """
        Scenario: Create guest token with Row Level Security
        Tests RLS enforcement in embedded context.
        """
        rls_rules: list[dict[str, str | int]] = [{"clause": rls_clause}]
        if dataset_id:
            rls_rules[0]["dataset"] = dataset_id

        return self.create_guest_token(dashboard_id=dashboard_id, rls_rules=rls_rules)
