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
"""Available domains controller — returns configured allowed domains."""

from __future__ import annotations

from litestar import Controller, get
from litestar.datastructures import State

from superset.guards.rbac import require_permission


class AvailableDomainsController(Controller):
    path = "/api/v1/available_domains"
    tags = ["Available Domains"]

    @get(
        "/",
        guards=[require_permission("can_read", "AvailableDomains")],
    )
    async def get_available_domains(self, state: State) -> dict[str, list[str]]:
        """GET /api/v1/available_domains/ — return allowed domains from config."""
        domains: list[str] = getattr(state.settings, "superset_webserver_domains", [])
        return {"result": domains}
