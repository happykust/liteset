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
"""
from __future__ import annotations

from litestar import Controller, get
from litestar.datastructures import State
from litestar.response import Template

SPA_ROUTES: list[str] = [
    "/superset/welcome/",
    "/explore/",
    "/dashboard/list/",
    "/dashboard/{pk:int}/",
    "/superset/sqllab/",
    "/chart/list/",
    "/superset/profile/{username:str}/",
    "/superset/dashboard/{slug:str}/",
    "/alert/list/",
    "/report/list/",
    "/database/list/",
    "/dataset/list/",
    "/savedquery/list/",
    "/csstemplate/list/",
    "/annotationlayer/list/",
    "/rowlevelsecurity/list/",
    "/superset/tags/",
    # FAB-style legacy routes (Flask-AppBuilder admin views)
    "/users/list/",
    "/users/add",
    "/users/{pk:int}/edit",
    "/roles/list/",
    "/roles/add",
    "/roles/{pk:int}/edit",
    "/logmodelview/list/",
]


class SPAController(Controller):
    path = "/"

    @get(
        SPA_ROUTES,
        opt={"exclude_from_auth": True},
    )
    async def spa_page(self, state: State) -> Template:
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
