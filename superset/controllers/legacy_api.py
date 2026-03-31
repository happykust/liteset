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
"""Legacy API controller -- deprecated endpoint stubs returning 410 Gone."""

from __future__ import annotations

from litestar import Controller, get
from litestar.response import Response


class LegacyApiController(Controller):
    path = "/api/v1"
    tags = ["Legacy"]

    @get("/query/", opt={"exclude_from_auth": False}, status_code=410)
    async def deprecated_query(self) -> Response[dict[str, str]]:
        """GET /api/v1/query/ -- deprecated query polling."""
        return Response(
            content={"message": "Deprecated. Use /api/v1/sqllab/ instead."},
            status_code=410,
            headers={
                "Deprecation": "true",
                "X-Deprecated-Endpoint": "/api/v1/query/",
            },
        )

    @get("/form_data/", opt={"exclude_from_auth": False}, status_code=410)
    async def deprecated_form_data(self) -> Response[dict[str, str]]:
        """GET /api/v1/form_data/ -- deprecated form_data endpoint."""
        return Response(
            content={
                "message": "Deprecated. Use /api/v1/explore/form_data/ instead."
            },
            status_code=410,
            headers={
                "Deprecation": "true",
                "X-Deprecated-Endpoint": "/api/v1/form_data/",
            },
        )

    @get("/time_range/", opt={"exclude_from_auth": False}, status_code=410)
    async def deprecated_time_range(self) -> Response[dict[str, str]]:
        """GET /api/v1/time_range/ -- deprecated time range endpoint."""
        return Response(
            content={
                "message": "Deprecated. Use chart data queries instead."
            },
            status_code=410,
            headers={
                "Deprecation": "true",
                "X-Deprecated-Endpoint": "/api/v1/time_range/",
            },
        )
