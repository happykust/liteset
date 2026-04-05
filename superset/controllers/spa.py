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
import os
from typing import Any

from litestar import Controller, get, post, Request
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
        "databaseview",
        "dataset",
        "savedquery",
        "savedqueryview",
        "csstemplate",
        "csstemplatemodelview",
        "annotationlayer",
        "tags",
        "rowlevelsecurity",
        "tablemodelview",
        "theme",
        "sqllab",
        "actionlog",
        "user_info",
        "users",
        "roles",
        "list_groups",
        "registrations",
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

# All 49 original FRONTEND_CONF_KEYS from Apache Superset's views/base.py,
# mapped to (settings_attr_name, default_value) for reading via getattr().
# Keys that have no direct settings field use (None, <hardcoded_default>)
# and are read from settings only if the attribute exists.
_FRONTEND_CONF_KEYS: tuple[str, ...] = (
    "SUPERSET_WEBSERVER_TIMEOUT",
    "SUPERSET_DASHBOARD_POSITION_DATA_LIMIT",
    "SUPERSET_DASHBOARD_PERIODICAL_REFRESH_LIMIT",
    "SUPERSET_DASHBOARD_PERIODICAL_REFRESH_WARNING_MESSAGE",
    "ENABLE_JAVASCRIPT_CONTROLS",
    "DEFAULT_SQLLAB_LIMIT",
    "DEFAULT_VIZ_TYPE",
    "SQL_MAX_ROW",
    "SUPERSET_WEBSERVER_DOMAINS",
    "SQLLAB_SAVE_WARNING_MESSAGE",
    "SQLLAB_DEFAULT_DBID",
    "DISPLAY_MAX_ROW",
    "GLOBAL_ASYNC_QUERIES_TRANSPORT",
    "GLOBAL_ASYNC_QUERIES_POLLING_DELAY",
    "SQL_VALIDATORS_BY_ENGINE",
    "SQLALCHEMY_DOCS_URL",
    "SQLALCHEMY_DISPLAY_TEXT",
    "GLOBAL_ASYNC_QUERIES_WEBSOCKET_URL",
    "DASHBOARD_AUTO_REFRESH_MODE",
    "DASHBOARD_AUTO_REFRESH_INTERVALS",
    "DASHBOARD_VIRTUALIZATION",
    "SCHEDULED_QUERIES",
    "EXCEL_EXTENSIONS",
    "CSV_EXTENSIONS",
    "COLUMNAR_EXTENSIONS",
    "ALLOWED_EXTENSIONS",
    "SAMPLES_ROW_LIMIT",
    "DEFAULT_TIME_FILTER",
    "HTML_SANITIZATION",
    "HTML_SANITIZATION_SCHEMA_EXTENSIONS",
    "WELCOME_PAGE_LAST_TAB",
    "VIZ_TYPE_DENYLIST",
    "ALERT_REPORTS_DEFAULT_CRON_VALUE",
    "ALERT_REPORTS_DEFAULT_RETENTION",
    "ALERT_REPORTS_DEFAULT_WORKING_TIMEOUT",
    "NATIVE_FILTER_DEFAULT_ROW_LIMIT",
    "SUPERSET_CLIENT_RETRY_ATTEMPTS",
    "SUPERSET_CLIENT_RETRY_DELAY",
    "SUPERSET_CLIENT_RETRY_BACKOFF_MULTIPLIER",
    "SUPERSET_CLIENT_RETRY_MAX_DELAY",
    "SUPERSET_CLIENT_RETRY_JITTER_MAX",
    "SUPERSET_CLIENT_RETRY_STATUS_CODES",
    "PREVENT_UNSAFE_DEFAULT_URLS_ON_DATASET",
    "JWT_ACCESS_CSRF_COOKIE_NAME",
    "SQLLAB_QUERY_RESULT_TIMEOUT",
    "SYNC_DB_PERMISSIONS_IN_ASYNC_MODE",
    "TABLE_VIZ_MAX_ROW_SERVER",
)

# Mapping: FRONTEND_CONF_KEY -> (settings_attribute, default_value).
# Superset config uses UPPER_CASE; Liteset settings uses lower_snake_case.
# For keys that don't have a 1:1 settings field the attribute name is the
# lowercased config key itself so that getattr() safely returns the default.
_CONF_KEY_DEFAULTS: dict[str, tuple[str, Any]] = {
    "SUPERSET_WEBSERVER_TIMEOUT": ("superset_webserver_timeout", 60),
    "SUPERSET_DASHBOARD_POSITION_DATA_LIMIT": (
        "superset_dashboard_position_data_limit",
        65535,
    ),
    "SUPERSET_DASHBOARD_PERIODICAL_REFRESH_LIMIT": (
        "superset_dashboard_periodical_refresh_limit",
        0,
    ),
    "SUPERSET_DASHBOARD_PERIODICAL_REFRESH_WARNING_MESSAGE": (
        "superset_dashboard_periodical_refresh_warning_message",
        None,
    ),
    "ENABLE_JAVASCRIPT_CONTROLS": ("enable_javascript_controls", False),
    "DEFAULT_SQLLAB_LIMIT": ("default_sqllab_limit", 1000),
    "DEFAULT_VIZ_TYPE": ("default_viz_type", "table"),
    "SQL_MAX_ROW": ("sql_max_row", 100000),
    "SUPERSET_WEBSERVER_DOMAINS": ("superset_webserver_domains", None),
    "SQLLAB_SAVE_WARNING_MESSAGE": ("sqllab_save_warning_message", None),
    "SQLLAB_DEFAULT_DBID": ("sqllab_default_dbid", None),
    "DISPLAY_MAX_ROW": ("display_max_row", 10000),
    "GLOBAL_ASYNC_QUERIES_TRANSPORT": (
        "global_async_queries_transport",
        "polling",
    ),
    "GLOBAL_ASYNC_QUERIES_POLLING_DELAY": (
        "global_async_queries_polling_delay",
        500,
    ),
    "SQL_VALIDATORS_BY_ENGINE": ("sql_validators_by_engine", {}),
    "SQLALCHEMY_DOCS_URL": (
        "sqlalchemy_docs_url",
        "https://docs.sqlalchemy.org/en/latest/",
    ),
    "SQLALCHEMY_DISPLAY_TEXT": (
        "sqlalchemy_display_text",
        "Change your database",
    ),
    "GLOBAL_ASYNC_QUERIES_WEBSOCKET_URL": (
        "global_async_queries_websocket_url",
        "ws://127.0.0.1:8080/",
    ),
    "DASHBOARD_AUTO_REFRESH_MODE": ("dashboard_auto_refresh_mode", "force"),
    "DASHBOARD_AUTO_REFRESH_INTERVALS": (
        "dashboard_auto_refresh_intervals",
        [
            [0, "Don't refresh"],
            [10, "10 seconds"],
            [30, "30 seconds"],
            [60, "1 minute"],
            [300, "5 minutes"],
            [1800, "30 minutes"],
            [3600, "1 hour"],
            [21600, "6 hours"],
            [43200, "12 hours"],
            [86400, "24 hours"],
        ],
    ),
    "DASHBOARD_VIRTUALIZATION": ("dashboard_virtualization", True),
    "SCHEDULED_QUERIES": ("scheduled_queries", {}),
    "EXCEL_EXTENSIONS": ("excel_extensions", {"xlsx", "xls"}),
    "CSV_EXTENSIONS": ("csv_extensions", {"csv", "tsv", "txt"}),
    "COLUMNAR_EXTENSIONS": ("columnar_extensions", {"parquet", "zip"}),
    "ALLOWED_EXTENSIONS": (
        "allowed_extensions",
        {"xlsx", "xls", "csv", "tsv", "txt", "parquet", "zip"},
    ),
    "SAMPLES_ROW_LIMIT": ("samples_row_limit", 1000),
    "DEFAULT_TIME_FILTER": ("default_time_filter", "No filter"),
    "HTML_SANITIZATION": ("html_sanitization", True),
    "HTML_SANITIZATION_SCHEMA_EXTENSIONS": (
        "html_sanitization_schema_extensions",
        {},
    ),
    "WELCOME_PAGE_LAST_TAB": ("welcome_page_last_tab", "all"),
    "VIZ_TYPE_DENYLIST": ("viz_type_denylist", []),
    "ALERT_REPORTS_DEFAULT_CRON_VALUE": (
        "alert_reports_default_cron_value",
        "0 0 * * *",
    ),
    "ALERT_REPORTS_DEFAULT_RETENTION": (
        "alert_reports_default_retention",
        90,
    ),
    "ALERT_REPORTS_DEFAULT_WORKING_TIMEOUT": (
        "alert_reports_default_working_timeout",
        3600,
    ),
    "NATIVE_FILTER_DEFAULT_ROW_LIMIT": (
        "native_filter_default_row_limit",
        1000,
    ),
    "SUPERSET_CLIENT_RETRY_ATTEMPTS": (
        "superset_client_retry_attempts",
        3,
    ),
    "SUPERSET_CLIENT_RETRY_DELAY": ("superset_client_retry_delay", 1000),
    "SUPERSET_CLIENT_RETRY_BACKOFF_MULTIPLIER": (
        "superset_client_retry_backoff_multiplier",
        2,
    ),
    "SUPERSET_CLIENT_RETRY_MAX_DELAY": (
        "superset_client_retry_max_delay",
        10000,
    ),
    "SUPERSET_CLIENT_RETRY_JITTER_MAX": (
        "superset_client_retry_jitter_max",
        1000,
    ),
    "SUPERSET_CLIENT_RETRY_STATUS_CODES": (
        "superset_client_retry_status_codes",
        [502, 503, 504],
    ),
    "PREVENT_UNSAFE_DEFAULT_URLS_ON_DATASET": (
        "prevent_unsafe_default_urls_on_dataset",
        True,
    ),
    "JWT_ACCESS_CSRF_COOKIE_NAME": (
        "jwt_access_csrf_cookie_name",
        "access_csrf_token",
    ),
    "SQLLAB_QUERY_RESULT_TIMEOUT": ("sqllab_query_result_timeout", 0),
    "SYNC_DB_PERMISSIONS_IN_ASYNC_MODE": (
        "sync_db_permissions_in_async_mode",
        False,
    ),
    "TABLE_VIZ_MAX_ROW_SERVER": ("table_viz_max_row_server", 500000),
}

# Static menu items.  The React frontend splits items whose top-level
# ``name`` is "Security", "Data", or "Manage" into the settings dropdown
# automatically (see Menu.tsx MenuWrapper).  We only need to emit them
# in ``menu_data.menu``; the ``settings`` key is kept as an empty list
# because the frontend populates it client-side.
_MENU_ITEMS: list[dict[str, Any]] = [
    {
        "name": "",
        "label": "Dashboards",
        "icon": "fa-dashboard",
        "url": "/dashboard/list/",
        "childs": [],
    },
    {
        "name": "",
        "label": "Charts",
        "icon": "fa-bar-chart",
        "url": "/chart/list/",
        "childs": [],
    },
    {
        "name": "",
        "label": "Datasets",
        "icon": "fa-table",
        "url": "/tablemodelview/list/",
        "childs": [],
    },
    {
        "name": "SQL Lab",
        "label": "SQL",
        "icon": "fa-flask",
        "childs": [
            {
                "name": "SQL Editor",
                "label": "SQL Lab",
                "icon": "fa-flask",
                "url": "/sqllab/",
            },
            {
                "name": "Saved Queries",
                "label": "Saved Queries",
                "icon": "fa-save",
                "url": "/savedqueryview/list/",
            },
            {
                "name": "Query Search",
                "label": "Query History",
                "icon": "fa-search",
                "url": "/sqllab/history/",
            },
        ],
    },
    {
        "name": "Data",
        "label": "Data",
        "icon": "fa-database",
        "childs": [
            {
                "name": "Databases",
                "label": "Database Connections",
                "icon": "fa-database",
                "url": "/databaseview/list/",
            },
        ],
    },
    {
        "name": "Security",
        "label": "Security",
        "icon": "",
        "childs": [
            {
                "name": "List Roles",
                "label": "List Roles",
                "icon": "",
                "url": "/roles/",
            },
            {
                "name": "List Users",
                "label": "List Users",
                "icon": "",
                "url": "/users/",
            },
            {
                "name": "Row Level Security",
                "label": "Row Level Security",
                "icon": "fa-lock",
                "url": "/rowlevelsecurity/list/",
            },
            {
                "name": "Action Log",
                "label": "Action Log",
                "icon": "fa-list-ol",
                "url": "/actionlog/list",
            },
        ],
    },
    {
        "name": "Manage",
        "label": "Manage",
        "icon": "",
        "childs": [
            {
                "name": "Alerts & Report",
                "label": "Alerts & Reports",
                "icon": "fa-exclamation-triangle",
                "url": "/alert/list/",
            },
            {
                "name": "Annotation Layers",
                "label": "Annotation Layers",
                "icon": "fa-comment",
                "url": "/annotationlayer/list/",
            },
            {
                "name": "CSS Templates",
                "label": "CSS Templates",
                "icon": "fa-css3",
                "url": "/csstemplatemodelview/list/",
            },
            {
                "name": "Tags",
                "label": "Tags",
                "icon": "",
                "url": "/superset/tags/",
            },
        ],
    },
]


def _get_csrf_token(
    settings: Any,
    session_id: str = "",
) -> str:
    """Generate a CSRF token for the SPA hidden input."""
    from superset.middleware.csrf import generate_csrf_token

    secret = ""
    if settings:
        sk = getattr(settings, "secret_key", "")
        if hasattr(sk, "get_secret_value"):
            sk = sk.get_secret_value()
        secret = str(sk)
    return generate_csrf_token(secret, session_id=session_id)


def _get_conf_value(settings: Any, key: str) -> Any:
    """Read a FRONTEND_CONF_KEY value from settings, converting sets to lists."""
    attr, default = _CONF_KEY_DEFAULTS.get(key, (key.lower(), None))
    value = getattr(settings, attr, default)
    # The frontend cannot serialise Python sets, convert to sorted lists.
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    return value


def _get_environment_tag(settings: Any) -> dict[str, str]:
    """Resolve environment tag from ENVIRONMENT_TAG_CONFIG logic.

    Mirrors the original ``get_environment_tag()`` from views/base.py.
    """
    env_tag_config: dict[str, Any] = getattr(
        settings,
        "environment_tag_config",
        {
            "variable": "SUPERSET_ENV",
            "values": {
                "debug": {"color": "error", "text": "flask-debug"},
                "development": {"color": "error", "text": "Development"},
                "production": {"color": "", "text": ""},
            },
        },
    )
    values = env_tag_config.get("values", {})
    env_var = env_tag_config.get("variable", "SUPERSET_ENV")
    env_name = os.environ.get(env_var)

    debug = getattr(settings, "debug", False)
    if not env_name or env_name not in values:
        env_name = "debug" if debug else None

    tag = values.get(env_name) if env_name else None
    return tag or {"text": "", "color": ""}


# Mapping of menu child item names to required feature flags.
# Mirrors the ``menu_cond`` lambdas from the original Superset initialization.
_MENU_ITEM_FEATURE_FLAGS: dict[str, str] = {
    "CSS Templates": "CSS_TEMPLATES",
    "Tags": "TAGGING_SYSTEM",
}


def _build_menu_data(user: Any, settings: Any) -> dict[str, Any]:
    """Build the complete menu_data dict for the bootstrap payload.

    Includes brand, navbar_right, environment_tag, and the menu items
    filtered by the user's permissions.  The ``settings`` key is left
    empty because the React frontend extracts Security/Data/Manage
    items from ``menu`` into ``settings`` client-side.
    """
    from superset.middleware.auth import UnauthenticatedUser

    is_anon = isinstance(user, UnauthenticatedUser)

    # Determine if user is admin (has "Admin" role)
    is_admin = False
    user_roles = getattr(user, "roles", [])
    admin_role_name = getattr(settings, "auth_role_admin", "Admin")
    for role in user_roles:
        if getattr(role, "name", "") == admin_role_name:
            is_admin = True
            break

    # Feature flags for conditional menu items
    feature_flags: dict[str, bool] = getattr(settings, "feature_flags", {})

    # Filter menu items by permissions and feature flags.
    # Admins see everything; other users need ``menu_access`` on the item.
    user_perms: set[str] = getattr(user, "permissions", set())
    filtered_menu: list[dict[str, Any]] = []
    for item in _MENU_ITEMS:
        item_name = item.get("name", "")
        # Top-level items with no name (Dashboards, Charts, Datasets)
        # are always visible.
        if not item_name:
            filtered_menu.append(item)
            continue
        # Admins see all menu categories.
        if is_admin:
            # Deep-copy item to avoid mutating the module-level constant
            # when filtering child items by feature flags.
            import copy

            item_copy = copy.deepcopy(item)
            if "childs" in item_copy:
                item_copy["childs"] = [
                    child
                    for child in item_copy["childs"]
                    if child.get("name", "") not in _MENU_ITEM_FEATURE_FLAGS
                    or feature_flags.get(
                        _MENU_ITEM_FEATURE_FLAGS[child["name"]], False
                    )
                ]
            filtered_menu.append(item_copy)
            continue
        # Non-admin users need menu_access permission for the category.
        perm_key = f"menu_access_{item_name}"
        if perm_key in user_perms:
            import copy

            item_copy = copy.deepcopy(item)
            if "childs" in item_copy:
                item_copy["childs"] = [
                    child
                    for child in item_copy["childs"]
                    if child.get("name", "") not in _MENU_ITEM_FEATURE_FLAGS
                    or feature_flags.get(
                        _MENU_ITEM_FEATURE_FLAGS[child["name"]], False
                    )
                ]
            filtered_menu.append(item_copy)

    # Brand configuration
    logo_target = getattr(settings, "logo_target_path", None)
    brand_path = logo_target or "/superset/welcome/"

    app_icon = getattr(
        settings,
        "app_icon",
        "/static/assets/images/superset-logo-horiz.png",
    )
    app_name = getattr(settings, "app_name", "Superset")
    logo_tooltip = getattr(settings, "logo_tooltip", "")

    logo_right_text = getattr(settings, "logo_right_text", "")
    if callable(logo_right_text):
        logo_right_text = logo_right_text()

    # Navbar right
    hide_user_info = getattr(settings, "feature_flags", {}).get(
        "MENU_HIDE_USER_INFO",
        False,
    )
    version_string = getattr(settings, "version_string", "")
    version_sha = getattr(settings, "version_sha", "")
    build_number = getattr(settings, "build_number", None)
    bug_report_url = getattr(settings, "bug_report_url", None)
    bug_report_icon = getattr(settings, "bug_report_icon", None)
    bug_report_text = getattr(settings, "bug_report_text", "Report a bug")
    documentation_url = getattr(settings, "documentation_url", None)
    documentation_icon = getattr(settings, "documentation_icon", None)
    documentation_text = getattr(
        settings,
        "documentation_text",
        "Documentation",
    )

    # Languages
    languages: dict[str, dict[str, str]] = getattr(
        settings,
        "languages",
        {
            "en": {"flag": "us", "name": "English", "url": "/lang/en"},
        },
    )

    return {
        "menu": filtered_menu,
        "brand": {
            "path": brand_path,
            "icon": app_icon,
            "alt": app_name,
            "tooltip": logo_tooltip,
            "text": logo_right_text,
        },
        "environment_tag": _get_environment_tag(settings),
        "navbar_right": {
            "show_watermark": "superset-logo-horiz" not in app_icon,
            "bug_report_url": bug_report_url,
            "bug_report_icon": bug_report_icon,
            "bug_report_text": bug_report_text,
            "documentation_url": documentation_url,
            "documentation_icon": documentation_icon,
            "documentation_text": documentation_text,
            "version_string": version_string,
            "version_sha": version_sha,
            "build_number": build_number,
            "languages": languages,
            "show_language_picker": len(languages) > 1,
            "user_is_anonymous": is_anon,
            "user_info_url": None if hide_user_info else "/users/userinfo/",
            "user_login_url": "/login/",
            "user_logout_url": "/logout/",
            "locale": "en",
        },
        # React builds settings from menu (Security/Data/Manage categories).
        "settings": [],
    }


def _build_user_data(user: Any) -> dict[str, Any]:
    """Build the ``user`` section of bootstrap_data.

    Mirrors ``bootstrap_user_data()`` from the original views/utils.py,
    including the ``roles`` dict and ``permissions`` for datasource/database
    access.  Works with both CachedUser and UnauthenticatedUser.
    """
    from superset.middleware.auth import UnauthenticatedUser

    is_anon = isinstance(user, UnauthenticatedUser)

    if is_anon:
        # Anonymous user — provide minimal payload so React doesn't crash.
        roles: dict[str, list[list[str]]] = {}
        user_roles = getattr(user, "roles", [])
        user_perms: set[str] = getattr(user, "permissions", set())
        for role in user_roles:
            role_name = getattr(role, "name", "Public")
            roles[role_name] = [p.rsplit("_", 1) for p in user_perms if "_" in p]
        permissions = _extract_data_permissions(user_perms)
        return {
            "username": "",
            "firstName": "",
            "lastName": "",
            "isActive": False,
            "isAnonymous": True,
            "roles": roles,
            "permissions": permissions,
        }

    # Authenticated user — full payload.
    created_on = getattr(user, "created_on", None)
    created_on_str = ""
    if created_on is not None:
        if hasattr(created_on, "isoformat"):
            created_on_str = created_on.isoformat()
        else:
            created_on_str = str(created_on)

    # Build roles dict: {role_name: [[action, resource], ...]}
    user_perms = getattr(user, "permissions", set())
    user_roles = getattr(user, "roles", [])
    roles = {}
    for role in user_roles:
        role_name = getattr(role, "name", "")
        # CachedUser stores permissions as flat "action_resource" strings
        # on the user level, not per-role.  We attach all permissions to
        # every role for compatibility with the frontend contract
        # (UserRoles = Record<string, [string, string][]>).
        roles[role_name] = [
            _split_permission(p) for p in sorted(user_perms) if "_" in p
        ]

    permissions = _extract_data_permissions(user_perms)

    return {
        "username": getattr(user, "username", ""),
        "firstName": getattr(user, "first_name", ""),
        "lastName": getattr(user, "last_name", ""),
        "userId": getattr(user, "id", None),
        "email": getattr(user, "email", ""),
        "isActive": bool(getattr(user, "active", False)),
        "isAnonymous": False,
        "createdOn": created_on_str,
        "loginCount": getattr(user, "login_count", 0),
        "roles": roles,
        "permissions": permissions,
    }


def _split_permission(perm_str: str) -> list[str]:
    """Split 'action_resource' into [action, resource].

    Uses the first underscore as separator.  If the string has no
    underscore returns [perm_str, ''].
    """
    parts = perm_str.split("_", 1)
    if len(parts) == 2:
        return parts
    return [perm_str, ""]


def _extract_data_permissions(
    perms: set[str],
) -> dict[str, list[str]]:
    """Extract database_access and datasource_access permission values.

    The original Superset ``get_permissions()`` collects all permission
    tuples where action is ``database_access`` or ``datasource_access``
    and returns the resource part as a list.

    CachedUser stores permissions as flat ``"action_resource"`` strings
    (e.g. ``"database_access_[examples].(id:1)"``).  We use prefix
    matching to extract the resource portion.
    """
    db_access: list[str] = []
    ds_access: list[str] = []
    for perm in sorted(perms):
        if not isinstance(perm, str):
            continue
        if perm.startswith("database_access_"):
            db_access.append(perm[len("database_access_") :])
        elif perm.startswith("datasource_access_"):
            ds_access.append(perm[len("datasource_access_") :])
    return {
        "database_access": db_access,
        "datasource_access": ds_access,
    }


def _build_theme_data(settings: Any) -> dict[str, Any]:
    """Build the theme section of bootstrap_data.

    Mirrors ``get_theme_bootstrap_data()`` from the original views/base.py.
    """
    default_theme = getattr(settings, "theme_default", {"algorithm": "default"})
    dark_theme = getattr(settings, "theme_dark", {"algorithm": "dark"})
    enable_ui_admin = getattr(
        settings,
        "enable_ui_theme_administration",
        False,
    )

    if callable(default_theme):
        default_theme = default_theme()
    if callable(dark_theme):
        dark_theme = dark_theme()

    return {
        "default": default_theme if isinstance(default_theme, dict) else {},
        "dark": dark_theme if isinstance(dark_theme, dict) else {},
        "enableUiThemeAdministration": enable_ui_admin,
    }


def _build_bootstrap_data(user: Any, settings: Any, **kw: Any) -> dict[str, Any]:
    """Build the complete bootstrap_data dict for the React SPA shell.

    Closely mirrors the original Apache Superset ``common_bootstrap_payload()``
    + ``bootstrap_user_data()`` + ``get_theme_bootstrap_data()``.
    """
    # --- conf: all FRONTEND_CONF_KEYS ---
    frontend_config: dict[str, Any] = {}
    for key in _FRONTEND_CONF_KEYS:
        frontend_config[key] = _get_conf_value(settings, key)

    # ALERT_REPORTS_NOTIFICATION_METHODS depends on SLACK_API_TOKEN
    slack_token = getattr(settings, "slack_api_token", None)
    if slack_token:
        frontend_config["ALERT_REPORTS_NOTIFICATION_METHODS"] = [
            "Email",
            "Slack",
            "SlackV2",
        ]
    else:
        frontend_config["ALERT_REPORTS_NOTIFICATION_METHODS"] = ["Email"]

    # HAS_GSHEETS_INSTALLED — default False in Liteset (no engine_spec registry)
    frontend_config["HAS_GSHEETS_INSTALLED"] = getattr(
        settings,
        "has_gsheets_installed",
        False,
    )

    # AUTH_TYPE and AUTH_USER_REGISTRATION
    auth_type = getattr(settings, "auth_type", 1)  # 1 = AUTH_DB
    auth_user_registration = getattr(
        settings,
        "auth_user_registration",
        False,
    )
    frontend_config["AUTH_TYPE"] = auth_type
    frontend_config["AUTH_USER_REGISTRATION"] = auth_user_registration

    if auth_user_registration:
        frontend_config["AUTH_USER_REGISTRATION_ROLE"] = getattr(
            settings,
            "auth_user_registration_role",
            "Public",
        )
        # RECAPTCHA only for non-OAuth registration
        # AUTH_OAUTH = 4 in FAB
        if auth_type != 4:
            frontend_config["RECAPTCHA_PUBLIC_KEY"] = getattr(
                settings,
                "recaptcha_public_key",
                "",
            )

    # OAuth providers
    if auth_type == 4:  # AUTH_OAUTH
        oauth_providers = getattr(settings, "oauth_providers", [])
        frontend_config["AUTH_PROVIDERS"] = [
            {"name": p.get("name", ""), "icon": p.get("icon", "")}
            for p in oauth_providers
            if isinstance(p, dict)
        ]

    # --- feature flags ---
    feature_flags = getattr(settings, "feature_flags", {})

    # --- d3 formats ---
    d3_format = getattr(settings, "d3_format", {})
    d3_time_format = getattr(settings, "d3_time_format", {})

    # --- currencies ---
    currencies = getattr(
        settings,
        "currencies",
        ["USD", "EUR", "GBP", "INR", "MXN", "JPY", "CNY"],
    )

    # --- deck.gl tiles ---
    deckgl_tiles = getattr(settings, "deckgl_base_map", None)

    # --- color schemes ---
    extra_cat_schemes = getattr(
        settings,
        "extra_categorical_color_schemes",
        [],
    )
    extra_seq_schemes = getattr(
        settings,
        "extra_sequential_color_schemes",
        [],
    )

    # --- menu_data ---
    menu_data = _build_menu_data(user, settings)

    # --- theme ---
    theme = _build_theme_data(settings)

    # --- common ---
    common: dict[str, Any] = {
        "application_root": getattr(settings, "application_root", "/"),
        "static_assets_prefix": getattr(settings, "static_assets_prefix", ""),
        "flash_messages": kw.get("flash_messages", []),
        "conf": frontend_config,
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
                    },
                },
            },
        },
        "extra_categorical_color_schemes": extra_cat_schemes,
        "extra_sequential_color_schemes": extra_seq_schemes,
        "d3_format": d3_format,
        "d3_time_format": d3_time_format,
        "currencies": currencies,
        "deckgl_tiles": deckgl_tiles,
        "menu_data": menu_data,
        "theme": theme,
    }

    # --- COMMON_BOOTSTRAP_OVERRIDES_FUNC ---
    overrides_func = getattr(
        settings,
        "common_bootstrap_overrides_func",
        None,
    )
    if callable(overrides_func):
        common.update(overrides_func(common))

    # --- user ---
    user_data = _build_user_data(user)

    result: dict[str, Any] = {
        "common": common,
        "user": user_data,
    }

    # Extra top-level keys (e.g. embedded dashboard info).
    result.update(kw.get("extra", {}))

    return result


class SPAController(Controller):
    path = "/"

    @get(
        _SPA_PATHS,
        media_type="text/html",
    )
    async def spa_page(
        self,
        request: Request[Any, Any, Any],
        state: State,
        path: str = "",
    ) -> Any:
        from litestar.response import Redirect

        settings = state.settings

        # Check authentication
        user = getattr(request, "user", None)
        is_auth = getattr(user, "is_authenticated", False)
        has_perms = bool(getattr(user, "permissions", None))

        # Unauthenticated without Public perms -> login
        if not is_auth and not has_perms:
            return Redirect(path="/login/")

        # Authenticated on / -> redirect to welcome
        request_path = request.url.path
        if is_auth and request_path in ("/", ""):
            return Redirect(
                path="/superset/welcome/",
            )

        bootstrap = _build_bootstrap_data(user, settings)
        return Template(
            template_name="spa.html",
            context={
                "bootstrap_data": json.dumps(bootstrap),
                "entry": "spa",
                "title": "Superset",
                "assets_prefix": getattr(
                    settings,
                    "static_assets_prefix",
                    "",
                ),
                "standalone_mode": False,
                "favicons": [
                    {
                        "href": ("/static/assets/images/favicon.png"),
                    },
                ],
                "csrf_token": _get_csrf_token(
                    settings,
                    session_id=request.cookies.get(
                        getattr(
                            settings,
                            "session_cookie_name",
                            "session",
                        ),
                        "",
                    ),
                ),
            },
        )

    @post(
        ["/superset/log/", "/superset/log"],
        exclude_from_auth=True,
        opt={"exclude_from_csrf": True},
        status_code=201,
    )
    async def frontend_log(
        self,
        request: Request[Any, Any, Any],
        state: State,
    ) -> dict[str, str]:
        """POST /superset/log/ -- frontend event logging.

        The React frontend fires analytics events here.
        ``?explode=events`` sends a JSON array of event
        dicts in the ``events`` form field.
        """
        import logging as _log

        from superset.models.core import Log

        logger = _log.getLogger("superset.frontend_log")
        try:
            form = await request.form()
            explode = request.query_params.get("explode")
            events: list[dict[str, Any]] = []

            if explode == "events":
                raw = form.get("events", "[]")
                events = json.loads(str(raw))
            else:
                events = [dict(form)]

            if not events:
                return {"status": "OK"}

            session_factory = state.session_factory
            user = getattr(request, "user", None)
            user_id = getattr(user, "id", None)

            async with session_factory() as session:
                for evt in events:
                    action = evt.get("action", evt.get("event_name", ""))
                    log_obj = Log(
                        action=action,
                        json=json.dumps(evt),
                        user_id=user_id,
                        dashboard_id=evt.get("dashboard_id"),
                        slice_id=evt.get("slice_id"),
                        duration_ms=evt.get("duration_ms", 0),
                    )
                    session.add(log_obj)
                await session.commit()

            logger.debug(
                "Logged %d frontend events",
                len(events),
            )
        except Exception:  # noqa: BLE001
            logger.debug("Frontend log failed", exc_info=True)

        return {"status": "OK"}
