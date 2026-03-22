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
"""Embedded dashboard controller — public embed access."""

from __future__ import annotations

from typing import Any

from litestar import Controller, get
from litestar.datastructures import State

from liteset.exceptions import LitesetNotFoundError


class EmbeddedDashboardController(Controller):
    path = "/api/v1/embedded_dashboard"
    tags = ["Embedded Dashboard"]

    @get(
        "/{uuid:str}",
        opt={"exclude_from_auth": True},
    )
    async def get_embedded(
        self,
        uuid: str,
        state: State,
    ) -> dict[str, Any]:
        """GET /api/v1/embedded_dashboard/{uuid} — get embedded dashboard config."""
        # Check EMBEDDED_SUPERSET feature flag
        feature_flags = getattr(state.settings, "feature_flags", {})
        if not feature_flags.get("EMBEDDED_SUPERSET", False):
            raise LitesetNotFoundError("Embedded dashboards are not enabled")

        # Query EmbeddedDashboard model by uuid
        # This requires session access — for now return UUID-based stub
        return {
            "result": {
                "uuid": uuid,
                "dashboard_id": None,
                "allowed_domains": [],
            },
        }
