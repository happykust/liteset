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

import json

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
        feature_flags = getattr(settings, "feature_flags", {})
        bootstrap = {
            "common": {
                "application_root": "/",
                "static_assets_prefix": (
                    settings.static_assets_prefix
                ),
                "flash_messages": [],
                "conf": {
                    "SUPERSET_WEBSERVER_TIMEOUT": 60,
                    "ENABLE_JAVASCRIPT_CONTROLS": False,
                    "DEFAULT_SQLLAB_LIMIT": 1000,
                    "SQL_MAX_ROW": 100000,
                    "SUPERSET_DASHBOARD_POSITION_DATA_LIMIT": (
                        500000
                    ),
                    "DISPLAY_MAX_ROW": 10000,
                    "GLOBAL_ASYNC_QUERIES_TRANSPORT": (
                        "polling"
                    ),
                    "GLOBAL_ASYNC_QUERIES_POLLING_DELAY": (
                        500
                    ),
                    "SQLALCHEMY_DOCS_URL": (
                        "https://docs.sqlalchemy.org/en/latest/"
                    ),
                    "SQLALCHEMY_DISPLAY_TEXT": (
                        "Change your database"
                    ),
                    "JWT_ACCESS_CSRF_COOKIE_NAME": (
                        "access_csrf_token"
                    ),
                    "JWT_ACCESS_CSRF_FIELD_NAME": (
                        "csrf_token"
                    ),
                    "RETRY_REQUESTS_TOTAL": 3,
                    "RETRY_REQUESTS_BACKOFF_FACTOR": 0.2,
                    "RETRY_REQUESTS_STATUS_FORCELIST": [
                        429,
                        500,
                        502,
                        503,
                        504,
                    ],
                },
                "locale": "en",
                "feature_flags": feature_flags,
                "language_pack": {
                    "domain": "superset",
                    "locale_data": {
                        "superset": {
                            "": {
                                "domain": "superset",
                                "lang": "en",
                                "plural_forms": "",
                            }
                        }
                    },
                },
                "extra_categorical_color_schemes": [],
                "extra_sequential_color_schemes": [],
                "d3_format": None,
                "d3_time_format": None,
                "currencies": ["USD", "EUR", "GBP"],
                "menu_data": {
                    "menu": [],
                    "brand": {
                        "path": "/superset/welcome/",
                        "icon": "",
                        "alt": "Superset",
                        "tooltip": "",
                        "text": "Superset",
                    },
                    "navbar_right": {
                        "show_watermark": False,
                        "languages": {
                            "en": {
                                "flag": "us",
                                "name": "English",
                                "url": "/lang/en",
                            },
                        },
                        "show_language_picker": False,
                        "user_is_anonymous": False,
                        "user_info_url": "/users/userinfo/",
                        "user_login_url": "/login/",
                        "user_logout_url": "/logout/",
                        "locale": "en",
                    },
                },
            },
            "user": {
                "username": "admin",
                "firstName": "Admin",
                "lastName": "User",
                "isActive": True,
                "isAnonymous": False,
            },
        }
        return Template(
            template_name="spa.html",
            context={
                "bootstrap_data": json.dumps(bootstrap),
                "entry": "spa",
                "title": "Superset",
                "assets_prefix": (
                    settings.static_assets_prefix
                ),
                "standalone_mode": False,
                "favicons": [
                    {
                        "href": (
                            "/static/assets/images/favicon.png"
                        )
                    }
                ],
                "csrf_token": "",
            },
        )
