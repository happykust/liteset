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
API endpoints that must fall through to the legacy ASGI fallback mount.
"""

from __future__ import annotations

import json
import logging as _log
import os
import re
from pathlib import Path
from typing import Any

from litestar import Controller, get, post, Request
from litestar.datastructures import State
from litestar.response import Redirect, Response, Template
from sqlalchemy import select

from superset.commands.dashboard.create import CreateDashboardCommand
from superset.db.daos.dashboard import AsyncDashboardDAO
from superset.db.daos.log import AsyncLogDAO
from superset.guards.rbac import require_permission
from superset.i18n import get_locale

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
        "register",
        "registrations",
        "logmodelview",
    }
)

# Explicit route paths: each prefix gets both /{prefix} and /{prefix}/{path:path}.
# Un-matched paths (API, static, un-migrated endpoints) are NOT intercepted
# and fall through to the legacy ASGI fallback mount during Strangler Fig coexistence.
_SPA_PATHS: list[str] = (
    ["/"]
    + [f"/{prefix}/{{path:path}}" for prefix in SPA_ROUTE_PREFIXES]
    + [f"/{prefix}" for prefix in SPA_ROUTE_PREFIXES]
)

# All 47 original FRONTEND_CONF_KEYS from Apache Superset's views/base.py,
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
# NOTE: GUEST_TOKEN_HEADER_NAME is NOT in FRONTEND_CONF_KEYS.
# The frontend reads it only from the embedded entry point bootstrap payload.

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

# Menu items are now defined in ``superset.controllers.menu`` as
# ``MenuItem`` objects with proper condition lambdas and recursive
# permission filtering.  The ``_filter_menu_for_user`` function from
# that module is reused here for the SPA bootstrap payload.


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
    """Resolve environment tag from ENVIRONMENT_TAG_CONFIG logic."""
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


def _build_menu_data(user: Any, settings: Any) -> dict[str, Any]:
    """Build the complete menu_data dict for the bootstrap payload.

    Includes brand, navbar_right, environment_tag, and the menu items
    filtered by the user's permissions.  The ``settings`` key is left
    empty because the React frontend extracts Security/Data/Manage
    items from ``menu`` into ``settings`` client-side.

    Menu filtering is delegated to ``superset.controllers.menu`` which
    implements the upstream recursive ``Menu.get_data()`` logic with proper
    per-child permission checks and ``should_render()`` condition lambdas.
    """
    from superset.controllers.menu import _filter_menu_for_user
    from superset.middleware.auth import UnauthenticatedUser

    is_anon = isinstance(user, UnauthenticatedUser)

    # Reuse the canonical menu filtering from menu.py
    filtered_menu = _filter_menu_for_user(user, settings)

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
            "user_info_url": None if hide_user_info else "/user_info/",
            "user_login_url": "/login/",
            "user_logout_url": "/logout/",
            "locale": get_locale(),
        },
        # React builds settings from menu (Security/Data/Manage categories).
        "settings": [],
    }


def _build_user_data(user: Any) -> dict[str, Any]:
    """Build the ``user`` section of bootstrap_data.

    Mirrors ``bootstrap_user_data()`` from the original views/utils.py,
    including the ``roles`` dict and ``permissions`` for datasource/database
    access.  Works with both CachedUser and UnauthenticatedUser.

    Permissions are stored as ``set[tuple[str, str]]`` — (action, resource)
    tuples matching the upstream ``get_user_roles_permissions`` format.
    The frontend expects ``roles: {roleName: [[action, resource], ...]}``.
    """
    from superset.middleware.auth import UnauthenticatedUser
    from superset.security.guest import GuestUser

    is_anon = isinstance(user, UnauthenticatedUser)
    is_guest = isinstance(user, GuestUser)

    if is_anon:
        # Anonymous user — original returns {} then adds roles/permissions.
        # Keeping payload empty ensures the frontend's isUser() check
        # does not treat anonymous as a real user.
        payload: dict[str, Any] = {}
        user_roles = getattr(user, "roles", [])
        user_perms: set[tuple[str, str]] = getattr(user, "permissions", set())
        sorted_perms = sorted(user_perms)
        roles: dict[str, list[list[str]]] = {}
        for role in user_roles:
            role_name = getattr(role, "name", "Public")
            roles[role_name] = [list(p) for p in sorted_perms]
        permissions = _extract_data_permissions(user_perms)
        payload["roles"] = roles
        payload["permissions"] = permissions
        return payload

    if is_guest:
        # Guest user — limited payload (no userId, email, loginCount,
        # createdOn). Matches original bootstrap_user_data guest branch.
        guest_payload: dict[str, Any] = {
            "username": getattr(user, "username", "guest"),
            "firstName": getattr(user, "first_name", ""),
            "lastName": getattr(user, "last_name", ""),
            "isActive": bool(getattr(user, "is_active", True)),
            "isAnonymous": False,
        }
        user_perms = getattr(user, "permissions", set())
        sorted_perms = sorted(user_perms)
        user_roles = getattr(user, "roles", [])
        roles = {}
        for role in user_roles:
            role_name = getattr(role, "name", "")
            roles[role_name] = [list(p) for p in sorted_perms]
        guest_payload["roles"] = roles
        guest_payload["permissions"] = _extract_data_permissions(user_perms)
        return guest_payload

    # Authenticated regular user — full payload.
    created_on_str = ""
    if (created_on := getattr(user, "created_on", None)) is not None:
        if hasattr(created_on, "isoformat"):
            created_on_str = created_on.isoformat()
        else:
            created_on_str = str(created_on)

    # Build roles dict: {role_name: [[action, resource], ...]}
    #
    # Known simplification vs. the upstream get_user_roles_permissions:
    # The original returns per-role permissions (each role maps only
    # to its own permission set).  Here we attach the union of ALL
    # permissions to every role.  This is functionally equivalent because
    # the frontend's ``findPermission`` (src/utils/findPermission.ts)
    # uses ``Object.values(roles).some(...)`` which flattens all roles
    # anyway -- the per-role distinction is never consumed.  RBAC guard
    # checks on the backend use the flat ``user.permissions`` set
    # directly, so per-role granularity is not required there either.
    #
    # If per-role fidelity is ever needed, add a ``role_permissions``
    # field to CachedUser (dict[str, set[tuple[str, str]]]) and populate
    # it in ``_resolve_user_from_db`` via per-role DB queries.
    user_perms = getattr(user, "permissions", set())
    sorted_perms = sorted(user_perms)
    user_roles = getattr(user, "roles", [])
    roles = {}
    for role in user_roles:
        role_name = getattr(role, "name", "")
        roles[role_name] = [list(p) for p in sorted_perms]

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


def _extract_data_permissions(
    perms: set[tuple[str, str]],
) -> dict[str, list[str]]:
    """Extract database_access and datasource_access permission values.

    The original Superset ``get_permissions()`` collects all permission
    tuples where action is ``database_access`` or ``datasource_access``
    and returns the resource part as a list.

    Permissions are stored as ``(action, resource)`` tuples — e.g.
    ``("database_access", "[examples].(id:1)")``.
    """
    db_access: list[str] = []
    ds_access: list[str] = []
    for action, resource in sorted(perms):
        if action == "database_access":
            db_access.append(resource)
        elif action == "datasource_access":
            ds_access.append(resource)
    return {
        "database_access": db_access,
        "datasource_access": ds_access,
    }


def _build_theme_data(settings: Any) -> dict[str, Any]:
    """Build the theme section of bootstrap_data (config-based).

    When ``ENABLE_UI_THEME_ADMINISTRATION`` is True the caller should use
    ``_build_theme_data_async`` instead to load themes from DB.
    """
    from superset.commands.theme import _is_valid_theme

    logger = _log.getLogger(__name__)

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

    if not _is_valid_theme(default_theme):
        logger.warning(
            "Invalid default theme configuration: %s, using empty theme",
            default_theme,
        )
        default_theme = {}

    if not _is_valid_theme(dark_theme):
        logger.warning(
            "Invalid dark theme configuration: %s, using empty theme",
            dark_theme,
        )
        dark_theme = {}

    return {
        "default": default_theme if isinstance(default_theme, dict) else {},
        "dark": dark_theme if isinstance(dark_theme, dict) else {},
        "enableUiThemeAdministration": enable_ui_admin,
    }


def _parse_theme_json(model: Any, fallback: Any) -> Any:
    """Parse JSON from a Theme model, returning *fallback* on failure."""
    if model is None:
        return fallback
    try:
        loaded = json.loads(model.json_data)
        if isinstance(loaded, dict):
            return loaded
    except (json.JSONDecodeError, AttributeError):
        pass
    return fallback


async def _build_theme_data_async(
    settings: Any,
    session_factory: Any,
) -> dict[str, Any]:
    """Build the theme section with DB lookup when UI admin is enabled.

    When ``ENABLE_UI_THEME_ADMINISTRATION`` is True, loads themes from DB
    via ``AsyncThemeDAO`` (falling back to config if no DB row is found).
    """
    from superset.commands.theme import _is_valid_theme

    logger = _log.getLogger(__name__)

    enable_ui_admin = getattr(
        settings,
        "enable_ui_theme_administration",
        False,
    )

    if not enable_ui_admin:
        return _build_theme_data(settings)

    default_theme = getattr(settings, "theme_default", {"algorithm": "default"})
    dark_theme = getattr(settings, "theme_dark", {"algorithm": "dark"})

    if callable(default_theme):
        default_theme = default_theme()
    if callable(dark_theme):
        dark_theme = dark_theme()

    try:
        from superset.db.daos.theme import AsyncThemeDAO

        async with session_factory() as session:
            theme_dao = AsyncThemeDAO(session)
            default_theme = _parse_theme_json(
                await theme_dao.find_system_default(), default_theme
            )
            dark_theme = _parse_theme_json(
                await theme_dao.find_system_dark(), dark_theme
            )
    except Exception:
        logger.debug(
            "Failed to load themes from DB, using config values",
            exc_info=True,
        )

    if not _is_valid_theme(default_theme):
        logger.warning(
            "Invalid default theme configuration: %s, using empty theme",
            default_theme,
        )
        default_theme = {}

    if not _is_valid_theme(dark_theme):
        logger.warning(
            "Invalid dark theme configuration: %s, using empty theme",
            dark_theme,
        )
        dark_theme = {}

    return {
        "default": default_theme if isinstance(default_theme, dict) else {},
        "dark": dark_theme if isinstance(dark_theme, dict) else {},
        "enableUiThemeAdministration": enable_ui_admin,
    }


def _build_bootstrap_data(user: Any, settings: Any, **kw: Any) -> dict[str, Any]:
    """Build the complete bootstrap_data dict for the React SPA shell."""
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
        # AUTH_OAUTH = 4 upstream
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

    # --- locale (resolved per-request by LocaleMiddleware) ---
    language = get_locale()

    # --- common ---
    common: dict[str, Any] = {
        "application_root": getattr(settings, "application_root", "/"),
        "static_assets_prefix": getattr(settings, "static_assets_prefix", ""),
        "flash_messages": kw.get("flash_messages", []),
        "conf": frontend_config,
        "locale": language,
        "feature_flags": feature_flags,
        "language_pack": {
            "domain": "superset",
            "locale_data": {
                "superset": {
                    "": {
                        "domain": "superset",
                        "lang": language,
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
    }

    # --- COMMON_BOOTSTRAP_OVERRIDES_FUNC ---
    overrides_func = getattr(
        settings,
        "common_bootstrap_overrides_func",
        None,
    )
    if callable(overrides_func):
        common.update(overrides_func(common))

    # Theme is always written after the overrides func so it cannot be overridden.
    common["theme"] = theme

    # --- user ---
    user_data = _build_user_data(user)

    result: dict[str, Any] = {
        "common": common,
        "user": user_data,
    }

    # Extra top-level keys (e.g. embedded dashboard info).
    result.update(kw.get("extra", {}))

    return result


async def _render_welcome_dashboard(
    user: Any,
    user_id: int,
    session_factory: Any,
    state: Any,
    settings: Any,
    request: Any,
) -> Any:
    """Render the SPA shell for the user's configured welcome dashboard.

    Returns a :class:`~litestar.response.Template` (200) if the dashboard
    is found and accessible, a 404 :class:`~litestar.response.Response`
    when the dashboard is missing or the user is denied, or ``None`` when
    no ``welcome_dashboard_id`` is set (caller falls through to the generic
    welcome page).
    """
    from superset.exceptions import SupersetSecurityException
    from superset.models.user import UserAttribute

    # A genuine DB error propagates as 500, not downgraded to 404/welcome-page.
    async with session_factory() as _wa_session:
        welcome_dashboard_id = (
            await _wa_session.execute(
                select(UserAttribute.welcome_dashboard_id).filter_by(
                    user_id=user_id,
                )
            )
        ).scalar()

    if not welcome_dashboard_id:
        return None

    # Load the dashboard (mirrors Dashboard.get() in original) and run the
    # access check while the instance is still ATTACHED to its session:
    # ``raise_for_access`` pre-loads lazy relationships (owners/roles/slices)
    # through the instance's own async session, which is impossible on a
    # detached object — a closed-session check used to crash for every
    # non-admin and get swallowed into a 404 below.
    async with session_factory() as _dash_session:
        _welcome_dash = await AsyncDashboardDAO(_dash_session).get_by_id_or_slug(
            str(welcome_dashboard_id)
        )

        if _welcome_dash is None:
            return Response(content=b"Not Found", status_code=404)

        # Access check (mirrors dashboard.raise_for_access() + can_dashboard
        # perm check).
        try:
            from superset.dependencies import provide_security_manager

            _sec_mgr = await provide_security_manager(_dash_session, state)

            # @has_access returns 403 (not 404) for authenticated users
            # lacking the perm.
            # NOTE: ``user`` is keyword-only on AsyncSecurityManager.has_access;
            # a positional call raises TypeError which the broad except silently turns
            # into a 404 for everyone.
            if not await _sec_mgr.has_access("can_dashboard", "Superset", user=user):
                return Response(content=b"Forbidden", status_code=403)

            await _sec_mgr.raise_for_access(dashboard=_welcome_dash, user=user)
        except SupersetSecurityException:
            return Response(content=b"Not Found", status_code=404)
        except Exception:
            _log.getLogger(__name__).debug(
                "Access check failed for welcome dashboard %s",
                welcome_dashboard_id,
                exc_info=True,
            )
            return Response(content=b"Not Found", status_code=404)

    # ``standalone_mode`` is truthy unless the param is absent/"false"/"0".
    _standalone_param = request.query_params.get("standalone")
    _standalone_mode = bool(
        _standalone_param and _standalone_param != "false" and _standalone_param != "0"
    )
    _bootstrap = _build_bootstrap_data(user, settings)
    if getattr(settings, "enable_ui_theme_administration", False):
        try:
            _theme = await _build_theme_data_async(settings, session_factory)
            _bootstrap["common"]["theme"] = _theme
        except Exception:
            _log.getLogger(__name__).debug(
                "Async theme lookup failed (welcome dashboard)",
                exc_info=True,
            )
    return Template(
        template_name="spa.html",
        context={
            "bootstrap_data": json.dumps(_bootstrap),
            "entry": "spa",
            "title": _welcome_dash.dashboard_title or "Superset",
            "assets_prefix": getattr(settings, "static_assets_prefix", ""),
            "standalone_mode": _standalone_mode,
            "favicons": [{"href": "/static/assets/images/favicon.png"}],
            "csrf_token": _get_csrf_token(
                settings,
                session_id=request.cookies.get(
                    getattr(settings, "session_cookie_name", "session"), ""
                ),
            ),
        },
    )


class SPAController(Controller):
    path = "/"

    @get(
        ["/superset/language_pack/{lang:str}/", "/superset/language_pack/{lang:str}"],
        media_type="application/json",
        guards=[require_permission("can_language_pack", "Superset")],
    )
    async def language_pack(self, lang: str) -> Response[Any]:
        """GET /superset/language_pack/<lang>/ — serve a JSON translation pack.

        Returns the ``messages.json`` from ``superset/translations/<lang>/LC_MESSAGES/``
        or a 404. Only language codes matching ``^[a-z]{2,3}(_[A-Z]{2})?$`` are
        accepted (e.g. ``en``, ``pt_BR``) — invalid codes get a 400.
        """
        if not re.match(r"^[a-z]{2,3}(_[A-Z]{2})?$", lang):
            return Response(
                content=json.dumps({"error": "Invalid language code"}),
                status_code=400,
                media_type="application/json",
            )

        # Translations live next to the ``superset/`` package directory.
        _translations_dir = Path(__file__).resolve().parent.parent / "translations"
        messages_file = _translations_dir / lang / "LC_MESSAGES" / "messages.json"

        if messages_file.is_file():
            try:
                content = messages_file.read_bytes()
                return Response(
                    content=content,
                    status_code=200,
                    media_type="application/json",
                )
            except OSError:
                pass

        return Response(
            content=json.dumps({"error": "Language pack doesn't exist on the server"}),
            status_code=404,
            media_type="application/json",
        )

    @get(
        ["/lang/{locale:str}", "/lang/{locale:str}/"],
        opt={"exclude_from_auth": True},
    )
    async def set_language(
        self,
        locale: str,
        request: Request[Any, Any, Any],
        state: State,
    ) -> Response[Any]:
        """GET /lang/<locale> — switch the UI language.

        Unsupported locale → 404; otherwise persists the choice in the
        ``language`` cookie that :class:`LocaleMiddleware` reads and redirects back.
        """
        settings = getattr(state, "settings", None)
        languages = getattr(settings, "languages", None) or {}
        if locale not in languages:
            return Response(
                content=b"Locale not supported.",
                status_code=404,
                media_type="text/plain",
            )
        referrer = request.headers.get("Referer") or "/"
        response: Response[Any] = Response(
            content=b"",
            status_code=302,
            headers={"Location": referrer},
        )
        response.set_cookie("language", locale)
        return response

    @get(
        ["/dashboard/new/", "/dashboard/new"],
        guards=[require_permission("can_write", "Dashboard")],
    )
    async def new_dashboard(
        self,
        request: Request[Any, Any, Any],
        session: Any,
        security_manager: Any,
    ) -> Any:
        """GET /dashboard/new/ — create blank dashboard and redirect to edit mode.

        Creates a row with title ``[ untitled dashboard ]``, assigns the current
        user as owner, then 302-redirects to ``/superset/dashboard/{id}/?edit=true``.
        """
        user = getattr(request, "user", None)
        if not getattr(user, "is_authenticated", False):
            return Redirect(path="/login/")

        user_id = getattr(user, "id", None)
        dao = AsyncDashboardDAO(session)
        cmd = CreateDashboardCommand(
            dao=dao,
            data={"dashboard_title": "[ untitled dashboard ]"},
            user_id=user_id,
            security_manager=security_manager,
        )
        dashboard = await cmd.execute()
        return Redirect(path=f"/superset/dashboard/{dashboard.id}/?edit=true")

    @get(
        [
            "/register/activation/{activation_hash:str}/",
            "/register/activation/{activation_hash:str}",
        ],
        media_type="text/html",
        exclude_from_auth=True,
    )
    async def register_activation(
        self,
        activation_hash: str,
        request: Request[Any, Any, Any],
        state: State,
    ) -> Any:
        """GET /register/activation/<activation_hash>/ -- activate pending registration.

        1. Look up the pending ``RegisterUser`` row by ``registration_hash``.
        2. If not found: flash "Registration not found" (danger) and redirect to /.
        3. Create the ``ab_user`` row with the registration data (hashed_password
           is stored as-is).
        4. If user creation fails: flash generic error (danger) and redirect to /.
        5. On success: delete the ``RegisterUser`` row and render the SPA shell
           with ``username``, ``first_name``, ``last_name`` in bootstrap_data.
        """
        import urllib.parse

        from litestar.datastructures import Cookie

        from superset.models.security import RegisterUser, Role, User

        _logger = _log.getLogger(__name__)

        _error_message = "Not possible to register you at the moment, try again later"
        _false_error_message = "Registration not found"
        _logmsg_err_no_hash = "Attempt to activate user with false hash: %s"

        settings = state.settings
        session_factory = state.session_factory

        reg_data: dict[str, Any] | None = None

        # Step 1: find_register_user(activation_hash) -- mirrors sm.find_register_user
        try:
            async with session_factory() as session:
                stmt = select(RegisterUser).where(
                    RegisterUser.registration_hash == activation_hash
                )
                result = await session.execute(stmt)
                reg = result.scalars().one_or_none()
                if reg is not None:
                    reg_data = {
                        "username": reg.username,
                        "email": reg.email,
                        "first_name": reg.first_name,
                        "last_name": reg.last_name,
                        "password": reg.password,
                        "id": reg.id,
                    }
        except Exception:  # noqa: BLE001
            _logger.exception("Error looking up registration hash %s", activation_hash)
            raise  # DB error propagates as 500

        # Step 2: not found -> flash + redirect
        if reg_data is None:
            _logger.error(_logmsg_err_no_hash, activation_hash)
            flash_cookie = Cookie(
                key="_flash_danger",  # mirrors flash(msg, "danger")
                value=urllib.parse.quote(_false_error_message),
                max_age=60,
                path="/",
                httponly=True,
                samesite="lax",
            )
            redirect_resp = Redirect(path="/")
            redirect_resp.cookies.append(flash_cookie)
            return redirect_resp

        # Step 3: add_user -- mirrors sm.add_user(username, email, first_name,
        # last_name, role=sm.find_role(sm.auth_user_registration_role),
        # hashed_password=reg.password)
        auth_user_registration_role: str = getattr(
            settings, "auth_user_registration_role", "Public"
        )
        user_created = False
        try:
            async with session_factory() as session:
                # find_role (mirrors sm.find_role)
                role_stmt = select(Role).where(Role.name == auth_user_registration_role)
                role_result = await session.execute(role_stmt)
                role = role_result.scalars().one_or_none()

                # Create User row with hashed_password stored as-is (already hashed
                # by add_register_user).
                new_user = User()
                new_user.first_name = reg_data["first_name"]
                new_user.last_name = reg_data["last_name"]
                new_user.username = reg_data["username"]
                new_user.email = reg_data["email"]
                new_user.active = True  # type: ignore[assignment]
                new_user.roles = [role] if role is not None else []
                # password is already hashed (stored by add_register_user)
                new_user.password = reg_data["password"]
                session.add(new_user)
                # pre-init lazy-load collection to avoid SA lazy-load error
                new_user.groups = []
                await session.commit()
                user_created = True
        except Exception:  # noqa: BLE001
            _logger.exception(
                "Failed to create user from registration hash %s", activation_hash
            )

        # Step 4: add_user failed -> flash + redirect
        if not user_created:
            flash_cookie = Cookie(
                key="_flash_danger",  # mirrors flash(msg, "danger")
                value=urllib.parse.quote(_error_message),
                max_age=60,
                path="/",
                httponly=True,
                samesite="lax",
            )
            redirect_resp = Redirect(path="/")
            redirect_resp.cookies.append(flash_cookie)
            return redirect_resp

        # Step 5: del_register_user + render SPA with extra context
        # mirrors: sm.del_register_user(reg) + render_app_template({username, ...})
        try:
            async with session_factory() as session:
                del_stmt = select(RegisterUser).where(
                    RegisterUser.registration_hash == activation_hash
                )
                del_result = await session.execute(del_stmt)
                reg_to_delete = del_result.scalars().one_or_none()
                if reg_to_delete is not None:
                    await session.delete(reg_to_delete)
                    await session.commit()
        except Exception:  # noqa: BLE001
            _logger.exception(
                "Failed to delete registration after activation for hash %s",
                activation_hash,
            )

        # Build SPA shell with extra bootstrap context (username/first_name/last_name).
        # Mirrors render_app_template({"username": ..., "first_name": ...,
        # "last_name": ...}) which merges the dict into bootstrap_data.
        user = getattr(request, "user", None)
        bootstrap = _build_bootstrap_data(
            user,
            settings,
            extra={
                "username": reg_data["username"],
                "first_name": reg_data["first_name"],
                "last_name": reg_data["last_name"],
            },
        )

        import json

        return Template(
            template_name="spa.html",
            context={
                "bootstrap_data": json.dumps(bootstrap),
                "entry": "spa",
                "title": "Superset",
                "assets_prefix": getattr(settings, "static_assets_prefix", ""),
                "standalone_mode": False,
                "favicons": [{"href": "/static/assets/images/favicon.png"}],
                "csrf_token": _get_csrf_token(
                    settings,
                    session_id=request.cookies.get(
                        getattr(settings, "session_cookie_name", "session"),
                        "",
                    ),
                ),
            },
        )

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
        settings = state.settings

        # Check authentication
        user = getattr(request, "user", None)
        is_auth = getattr(user, "is_authenticated", False)
        has_perms = bool(getattr(user, "permissions", None))

        request_path = request.url.path

        if request_path in ("/", ""):
            return Redirect(path="/superset/welcome/")

        # /superset/welcome/ requires an authenticated user — Public role
        # permissions do NOT bypass this gate.
        if request_path.rstrip("/") == "/superset/welcome":
            if not is_auth:
                return Redirect(path="/login/")

            # If the user has a welcome_dashboard_id configured in UserAttribute,
            # render the dashboard SPA shell in-place at /superset/welcome/
            # (no redirect).
            _uid = getattr(user, "id", None)
            _sf = getattr(state, "session_factory", None)
            if _uid is not None and _sf is not None:
                _resp = await _render_welcome_dashboard(
                    user, _uid, _sf, state, settings, request
                )
                if _resp is not None:
                    return _resp

        # Other SPA paths: anonymous users without Public perms -> login.
        # Anonymous users with Public perms fall through and render SPA;
        # equivalent permission checks happen at API-call time via guards.
        elif not is_auth and not has_perms:
            return Redirect(path="/login/")

        bootstrap = _build_bootstrap_data(user, settings)

        enable_ui_admin = getattr(settings, "enable_ui_theme_administration", False)
        session_factory = getattr(state, "session_factory", None)
        if enable_ui_admin and session_factory is not None:
            try:
                theme_data = await _build_theme_data_async(settings, session_factory)
                bootstrap["common"]["theme"] = theme_data
            except Exception:
                _log.getLogger(__name__).debug(
                    "Async theme DB lookup failed, using config",
                    exc_info=True,
                )

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
        guards=[require_permission("can_log", "Superset")],
        opt={"exclude_from_csrf": True},
        status_code=200,
    )
    async def frontend_log(
        self,
        request: Request[Any, Any, Any],
        state: State,
    ) -> Response[None]:
        """POST /superset/log/ -- frontend event logging.

        The React frontend fires analytics events here.
        ``?explode=events`` sends a JSON array of event dicts in the ``events``
        form field. Returns an empty 200 body.
        """
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

            if events:
                session_factory = state.session_factory
                user = getattr(request, "user", None)
                user_id = getattr(user, "id", None)

                # Upstream writes action="log" (the view function name) for
                # ALL frontend events.  The per-event "event_name" (e.g.
                # "mount_dashboard") lives inside the json column, not in action.
                # recent_activity queries: action=="log" AND json contains event_name.
                referrer_raw = request.headers.get("Referer") or request.headers.get(
                    "referrer"
                )
                referrer = referrer_raw[:1000] if referrer_raw else None

                async with session_factory() as session:
                    log_dao = AsyncLogDAO(session)
                    for evt in events:
                        await log_dao.create_log(
                            {
                                "action": "log",
                                "json": json.dumps(evt),
                                "user_id": user_id,
                                "dashboard_id": evt.get("dashboard_id"),
                                "slice_id": evt.get("slice_id"),
                                "duration_ms": 0,
                                "referrer": referrer,
                            }
                        )
                    await session.commit()

                logger.debug(
                    "Logged %d frontend events",
                    len(events),
                )
        except Exception:  # noqa: BLE001
            logger.debug("Frontend log failed", exc_info=True)

        return Response(content=None, status_code=200)
