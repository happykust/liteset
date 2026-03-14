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
Uses explicit prefix-based routes to avoid intercepting un-migrated
API endpoints that must fall through to the Flask ASGI fallback mount.
"""

from __future__ import annotations

from litestar import Controller, get
from litestar.datastructures import State
from litestar.response import Template

SPA_ROUTE_PREFIXES: frozenset[str] = frozenset(
    {
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
)

# Explicit route paths: each prefix gets both /{prefix} and /{prefix}/{path:path}.
# Un-matched paths (API, static, un-migrated endpoints) are NOT intercepted
# and fall through to the Flask ASGI fallback mount during Strangler Fig coexistence.
_SPA_PATHS: list[str] = (
    ["/"]
    + [f"/{prefix}/{{path:path}}" for prefix in SPA_ROUTE_PREFIXES]
    + [f"/{prefix}" for prefix in SPA_ROUTE_PREFIXES]
)


class SPAController(Controller):
    path = "/"

    @get(
        _SPA_PATHS,
        exclude_from_auth=True,
    )
    async def spa_page(self, state: State, path: str = "") -> Template:
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
