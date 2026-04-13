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
"""OpenAPI spec controller.

Port of Flask-AppBuilder's ``OpenApi`` class
(``flask_appbuilder/api/manager.py``).

The original endpoint ``GET /api/<version>/_openapi`` returns the
OpenAPI JSON spec for all API views that belong to a certain version.

In Litestar the OpenAPI schema is generated automatically from the
registered route handlers.  This controller serves the same schema
at the FAB-compatible path ``/api/{version}/_openapi`` so that
existing tooling and the Swagger UI integration continue to work.
"""

from __future__ import annotations

import logging
from typing import Any

from litestar import Controller, get, Request
from litestar.response import Response

from superset.guards.rbac import require_authentication

logger = logging.getLogger(__name__)


class OpenApiController(Controller):
    """OpenAPI spec — ``GET /api/{version:str}/_openapi``.

    Port of FAB's ``OpenApi.get`` from
    ``flask_appbuilder/api/manager.py``.

    The original implementation iterates all registered ``BaseApi``
    views, calls ``add_api_spec`` for each matching version, then
    returns the assembled ``APISpec`` dict.

    In Litestar the framework builds a single OpenAPI schema from all
    registered route handlers.  This controller returns that schema
    at the FAB-compatible path for backward compatibility.  Only
    ``v1`` is recognized as a valid version (matching the original
    Superset setup); other versions return 404.
    """

    path = "/api"
    tags = ["OpenAPI"]

    @get(
        "/{version:str}/_openapi",
        guards=[require_authentication],
    )
    async def get_openapi_spec(
        self,
        request: Request[Any, Any, Any],
        version: str,
    ) -> Response[Any]:
        """Get the OpenAPI spec for a specific API version.

        Mirrors FAB's ``GET /api/<version>/_openapi``.

        Parameters
        ----------
        version : str
            API version string (e.g. ``"v1"``).

        Returns
        -------
        200
            The OpenAPI spec as JSON.
        404
            If the requested version is not found.
        """
        # Only v1 is supported — matches original Superset
        if version != "v1":
            return Response(
                content={"message": "Not found", "severity": "warning"},
                status_code=404,
                media_type="application/json",
            )

        # Litestar generates the OpenAPI schema from the app's route
        # handlers.  Access it from the app instance.
        app = request.app
        schema = app.openapi_schema
        return Response(
            content=schema.to_schema(),
            status_code=200,
            media_type="application/json",
        )
