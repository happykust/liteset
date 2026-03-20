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
"""SPA HTML shell controller.

Renders the HTML page that bootstraps the React frontend.
Uses a catch-all route with prefix guard instead of hardcoded routes.
"""
from __future__ import annotations

from litestar import Controller, get
from litestar.datastructures import State
from litestar.exceptions import NotFoundException
from litestar.response import Template

SPA_ROUTE_PREFIXES: set[str] = {
    "explore",
    "dashboard",
    "superset",
    "chart",
    "alert",
    "report",
    "database",
    "dataset",
    "savedquery",
    "csstemplate",
    "annotationlayer",
    "rowlevelsecurity",
    "users",
    "roles",
    "logmodelview",
}


class SPAController(Controller):
    path = "/"

    @get(
        "/{path:path}",
        opt={"exclude_from_auth": True},
    )
    async def spa_page(self, state: State, path: str = "") -> Template:
        # Extract the first path segment to match against known prefixes
        first_segment = path.strip("/").split("/")[0] if path.strip("/") else ""

        if first_segment not in SPA_ROUTE_PREFIXES:
            raise NotFoundException(detail=f"Unknown route: /{path}")

        settings = state.settings
        return Template(
            template_name="spa.html",
            context={
                "bootstrap_data": "{}",
                "entry": "spa",
                "title": "Superset",
                "assets_prefix": settings.static_assets_prefix,
                "standalone_mode": False,
                "favicons": [{"href": "/static/assets/images/favicon.png"}],
                "csrf_token": "",
            },
        )
