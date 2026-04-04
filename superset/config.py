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
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Any

from pydantic import field_validator, model_validator, SecretStr
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

# Minimum 16 characters for secret key (validated via field_validator below).
# SecretStr masks the value in repr/logs to prevent accidental exposure.
SecretKeyStr = SecretStr

_SYNC_TO_ASYNC_DRIVERS = {
    "postgresql://": "postgresql+asyncpg://",
    "postgresql+psycopg2://": "postgresql+asyncpg://",
    "postgresql+pg8000://": "postgresql+asyncpg://",
    "mysql://": "mysql+asyncmy://",
    "mysql+pymysql://": "mysql+asyncmy://",
    "mysql+mysqldb://": "mysql+asyncmy://",
    "sqlite://": "sqlite+aiosqlite://",
}

_SUPERSET_TO_LITESET: dict[str, str] = {
    "SECRET_KEY": "secret_key",
    "SQLALCHEMY_DATABASE_URI": "sqlalchemy_database_uri",
    "SQLALCHEMY_EXAMPLES_URI": "sqlalchemy_examples_uri",
    "CORS_ALLOW_ORIGINS": "cors_allow_origins",
    "GLOBAL_ASYNC_QUERIES": "global_async_queries",
    "STATIC_ASSETS_PREFIX": "static_assets_prefix",
    # Phase 4: query processing and SqlLab config
    "ROW_LIMIT": "row_limit",
    "SAMPLES_ROW_LIMIT": "samples_row_limit",
    "CACHE_DEFAULT_TIMEOUT": "cache_default_timeout",
    "DATA_CACHE_CONFIG": "data_cache_config",
    "SQL_MAX_ROW": "sql_max_row",
    "DISPLAY_MAX_ROW": "display_max_row",
    "DEFAULT_SQLLAB_LIMIT": "default_sqllab_limit",
    "CSV_EXPORT": "csv_export",
    "EXCEL_EXPORT": "excel_export",
    "PERMANENT_SESSION_LIFETIME": "session_max_age",
    "CACHE_CONFIG": "cache_config",
    "QUERY_CACHE_CONFIG": "query_cache_config",
    "FEATURE_FLAGS": "feature_flags",
    "MAPBOX_API_KEY": "mapbox_api_key",
    "DEFAULT_RELATIVE_START_TIME": "default_relative_start_time",
    "DEFAULT_RELATIVE_END_TIME": "default_relative_end_time",
    "VIZ_TYPE_DENYLIST": "viz_type_denylist",
    # UI / Branding
    "APP_NAME": "app_name",
    "APP_ICON": "app_icon",
    "LOGO_TARGET_PATH": "logo_target_path",
    "LOGO_TOOLTIP": "logo_tooltip",
    "LOGO_RIGHT_TEXT": "logo_right_text",
    "FAVICONS": "favicons",
    "VERSION_STRING": "version_string",
    "VERSION_SHA": "version_sha",
    "BUILD_NUMBER": "build_number",
    "BUG_REPORT_URL": "bug_report_url",
    "BUG_REPORT_TEXT": "bug_report_text",
    "BUG_REPORT_ICON": "bug_report_icon",
    "DOCUMENTATION_URL": "documentation_url",
    "DOCUMENTATION_TEXT": "documentation_text",
    "DOCUMENTATION_ICON": "documentation_icon",
    # Auth / Security
    "AUTH_TYPE": "auth_type",
    "AUTH_USER_REGISTRATION": "auth_user_registration",
    "AUTH_USER_REGISTRATION_ROLE": "auth_user_registration_role",
    "PUBLIC_ROLE_LIKE": "public_role_like",
    "API_LOGIN_ALLOW_MULTIPLE_PROVIDERS": "api_login_allow_multiple_providers",
    "OAUTH_PROVIDERS": "oauth_providers",
    "RECAPTCHA_PUBLIC_KEY": "recaptcha_public_key",
    "CUSTOM_SECURITY_MANAGER": "custom_security_manager",
    "SESSION_COOKIE_HTTPONLY": "session_cookie_httponly",
    "SESSION_COOKIE_SECURE": "session_cookie_secure",
    "SESSION_COOKIE_SAMESITE": "session_cookie_samesite",
    "JWT_ACCESS_TOKEN_EXPIRES": "jwt_access_token_expires",
    "WTF_CSRF_ENABLED": "wtf_csrf_enabled",
    "WTF_CSRF_TIME_LIMIT": "wtf_csrf_time_limit",
    # Frontend Bootstrap
    "LANGUAGES": "languages",
    "THEME_DEFAULT": "theme_default",
    "THEME_DARK": "theme_dark",
    "ENABLE_UI_THEME_ADMINISTRATION": "enable_ui_theme_administration",
    "D3_FORMAT": "d3_format",
    "D3_TIME_FORMAT": "d3_time_format",
    "CURRENCIES": "currencies",
    "DECKGL_BASE_MAP": "deckgl_base_map",
    "EXTRA_CATEGORICAL_COLOR_SCHEMES": "extra_categorical_color_schemes",
    "EXTRA_SEQUENTIAL_COLOR_SCHEMES": "extra_sequential_color_schemes",
    "APPLICATION_ROOT": "application_root",
    "HAS_GSHEETS_INSTALLED": "has_gsheets_installed",
    "WELCOME_PAGE_LAST_TAB": "welcome_page_last_tab",
    "ENVIRONMENT_TAG_CONFIG": "environment_tag_config",
    "COMMON_BOOTSTRAP_OVERRIDES_FUNC": "common_bootstrap_overrides_func",
    # FRONTEND_CONF_KEYS
    "SUPERSET_WEBSERVER_TIMEOUT": "superset_webserver_timeout",
    "SUPERSET_WEBSERVER_DOMAINS": "superset_webserver_domains",
    "SUPERSET_DASHBOARD_POSITION_DATA_LIMIT": "superset_dashboard_position_data_limit",
    "SUPERSET_DASHBOARD_PERIODICAL_REFRESH_LIMIT": "superset_dashboard_periodical_refresh_limit",
    "SUPERSET_DASHBOARD_PERIODICAL_REFRESH_WARNING_MESSAGE": "superset_dashboard_periodical_refresh_warning_message",
    "DASHBOARD_AUTO_REFRESH_MODE": "dashboard_auto_refresh_mode",
    "DASHBOARD_AUTO_REFRESH_INTERVALS": "dashboard_auto_refresh_intervals",
    "SQLLAB_SAVE_WARNING_MESSAGE": "sqllab_save_warning_message",
    "SQLLAB_QUERY_RESULT_TIMEOUT": "sqllab_query_result_timeout",
    "DEFAULT_VIZ_TYPE": "default_viz_type",
    "DEFAULT_TIME_FILTER": "default_time_filter",
    "SCHEDULED_QUERIES": "scheduled_queries",
    "EXCEL_EXTENSIONS": "excel_extensions",
    "CSV_EXTENSIONS": "csv_extensions",
    "COLUMNAR_EXTENSIONS": "columnar_extensions",
    "ALLOWED_EXTENSIONS": "allowed_extensions",
    "HTML_SANITIZATION": "html_sanitization",
    "HTML_SANITIZATION_SCHEMA_EXTENSIONS": "html_sanitization_schema_extensions",
    "ALERT_REPORTS_DEFAULT_CRON_VALUE": "alert_reports_default_cron_value",
    "ALERT_REPORTS_DEFAULT_RETENTION": "alert_reports_default_retention",
    "ALERT_REPORTS_DEFAULT_WORKING_TIMEOUT": "alert_reports_default_working_timeout",
    "NATIVE_FILTER_DEFAULT_ROW_LIMIT": "native_filter_default_row_limit",
    "SUPERSET_CLIENT_RETRY_ATTEMPTS": "superset_client_retry_attempts",
    "SUPERSET_CLIENT_RETRY_DELAY": "superset_client_retry_delay",
    "SUPERSET_CLIENT_RETRY_BACKOFF_MULTIPLIER": "superset_client_retry_backoff_multiplier",
    "SUPERSET_CLIENT_RETRY_MAX_DELAY": "superset_client_retry_max_delay",
    "SUPERSET_CLIENT_RETRY_JITTER_MAX": "superset_client_retry_jitter_max",
    "SUPERSET_CLIENT_RETRY_STATUS_CODES": "superset_client_retry_status_codes",
    "PREVENT_UNSAFE_DEFAULT_URLS_ON_DATASET": "prevent_unsafe_default_urls_on_dataset",
    "JWT_ACCESS_CSRF_COOKIE_NAME": "jwt_access_csrf_cookie_name",
    "SYNC_DB_PERMISSIONS_IN_ASYNC_MODE": "sync_db_permissions_in_async_mode",
    "TABLE_VIZ_MAX_ROW_SERVER": "table_viz_max_row_server",
    "ENABLE_JAVASCRIPT_CONTROLS": "enable_javascript_controls",
    "SQLALCHEMY_DOCS_URL": "sqlalchemy_docs_url",
    "SQLALCHEMY_DISPLAY_TEXT": "sqlalchemy_display_text",
    "GLOBAL_ASYNC_QUERIES_TRANSPORT": "global_async_queries_transport",
    "GLOBAL_ASYNC_QUERIES_POLLING_DELAY": "global_async_queries_polling_delay",
    "GLOBAL_ASYNC_QUERIES_WEBSOCKET_URL": "global_async_queries_websocket_url",
    "SQL_VALIDATORS_BY_ENGINE": "sql_validators_by_engine",
    # Email / SMTP / Slack / Infra
    "SMTP_HOST": "smtp_host",
    "SMTP_PORT": "smtp_port",
    "SMTP_USER": "smtp_user",
    "SMTP_PASSWORD": "smtp_password",
    "SMTP_MAIL_FROM": "smtp_mail_from",
    "SMTP_STARTTLS": "smtp_starttls",
    "SMTP_SSL": "smtp_ssl",
    "SMTP_SSL_SERVER_AUTH": "smtp_ssl_server_auth",
    "EMAIL_REPORTS_SUBJECT_PREFIX": "email_reports_subject_prefix",
    "EMAIL_REPORTS_CTA": "email_reports_cta",
    "SLACK_API_TOKEN": "slack_api_token",
    "ADVANCED_DATA_TYPES": "advanced_data_types",
    "MAX_WS_PER_USER": "max_ws_per_user",
    "ALERT_REPORTS_NOTIFICATION_DRY_RUN": "alert_reports_notification_dry_run",
    # Feature flag functions
    "GET_FEATURE_FLAGS_FUNC": "get_feature_flags_func",
    "IS_FEATURE_ENABLED_FUNC": "is_feature_enabled_func",
}


_superset_config_cache: dict[str, dict[str, Any]] = {}

# Default feature flags — mirrors Apache Superset's DEFAULT_FEATURE_FLAGS.
# User-provided FEATURE_FLAGS (via superset_config.py or env) are merged
# on top, matching the original merge semantics.
_DEFAULT_FEATURE_FLAGS: dict[str, bool] = {
    "DRUID_JOINS": False,
    "DYNAMIC_PLUGINS": False,
    "ENABLE_TEMPLATE_PROCESSING": False,
    "ENABLE_JAVASCRIPT_CONTROLS": False,
    "PRESTO_EXPAND_DATA": False,
    "THUMBNAILS": False,
    "ENABLE_DASHBOARD_SCREENSHOT_ENDPOINTS": False,
    "ENABLE_DASHBOARD_DOWNLOAD_WEBDRIVER_SCREENSHOT": False,
    "TAGGING_SYSTEM": False,
    "SQLLAB_BACKEND_PERSISTENCE": True,
    "LISTVIEWS_DEFAULT_CARD_VIEW": False,
    "ESCAPE_MARKDOWN_HTML": False,
    "DASHBOARD_VIRTUALIZATION": True,
    "GLOBAL_ASYNC_QUERIES": False,
    "EMBEDDED_SUPERSET": False,
    "ALERT_REPORTS": False,
    "ALERT_REPORT_TABS": False,
    "ALERT_REPORT_SLACK_V2": False,
    "DASHBOARD_RBAC": False,
    "ENABLE_ADVANCED_DATA_TYPES": False,
    "ALERTS_ATTACH_REPORTS": True,
    "ALLOW_FULL_CSV_EXPORT": False,
    "ALLOW_ADHOC_SUBQUERY": False,
    "USE_ANALOGOUS_COLORS": False,
    "RLS_IN_SQLLAB": False,
    "OPTIMIZE_SQL": False,
    "IMPERSONATE_WITH_EMAIL_PREFIX": False,
    "CACHE_IMPERSONATION": False,
    "CACHE_QUERY_BY_USER": False,
    "EMBEDDABLE_CHARTS": True,
    "DRILL_TO_DETAIL": True,
    "DRILL_BY": True,
    "DATAPANEL_CLOSED_BY_DEFAULT": False,
    "FILTERBAR_CLOSED_BY_DEFAULT": False,
    "ESTIMATE_QUERY_COST": False,
    "SSH_TUNNELING": False,
    "AVOID_COLORS_COLLISION": True,
    "MENU_HIDE_USER_INFO": False,
    "ENABLE_SUPERSET_META_DB": False,
    "PLAYWRIGHT_REPORTS_AND_THUMBNAILS": False,
    "CHART_PLUGINS_EXPERIMENTAL": False,
    "SQLLAB_FORCE_RUN_ASYNC": False,
    "ENABLE_FACTORY_RESET_COMMAND": False,
    "SLACK_ENABLE_AVATARS": False,
    "CSS_TEMPLATES": True,
    "DATE_FORMAT_IN_EMAIL_SUBJECT": False,
    "DATASET_FOLDERS": False,
    "AG_GRID_TABLE_ENABLED": False,
    "TABLE_V2_TIME_COMPARISON_ENABLED": False,
    "DATE_RANGE_TIMESHIFTS_ENABLED": False,
}


class SupersetConfigSettingsSource(PydanticBaseSettingsSource):
    """Read settings from superset_config.py as a Pydantic settings source.

    Priority: env vars > superset_config.py > defaults.
    Caches loaded values per config path to avoid re-executing the
    config file on every SupersetSettings() construction.
    """

    def __init__(self, settings_cls: type[BaseSettings]) -> None:
        super().__init__(settings_cls)
        self._values: dict[str, Any] = self._load()

    @staticmethod
    def _load() -> dict[str, Any]:
        path = os.environ.get("SUPERSET_CONFIG_PATH", "")
        if not path or not Path(path).exists():
            return {}
        if path in _superset_config_cache:
            return _superset_config_cache[path]
        spec = importlib.util.spec_from_file_location("superset_config", path)
        if spec is None or spec.loader is None:
            return {}
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            raise ImportError(
                f"Failed to load superset_config.py from {path}: {exc}"
            ) from exc
        values: dict[str, Any] = {}
        for sup_key, lit_key in _SUPERSET_TO_LITESET.items():
            val = getattr(module, sup_key, None)
            if val is not None:
                values[lit_key] = val
        _superset_config_cache[path] = values
        return values

    def get_field_value(
        self, field: FieldInfo, field_name: str
    ) -> tuple[Any, str, bool]:
        if field_name in self._values:
            return self._values[field_name], field_name, True
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        return self._values


class SupersetSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LITESET_",
        env_file=".env",
        extra="ignore",
    )

    secret_key: SecretKeyStr
    sqlalchemy_database_uri: str
    sqlalchemy_examples_uri: str = ""
    host: str = "0.0.0.0"  # noqa: S104
    port: int = 8088
    debug: bool = False
    static_assets_prefix: str = ""
    global_async_queries: bool = False
    cors_allow_origins: list[str] = []
    log_level: str = "INFO"
    production: bool = False
    # Query processing (used by AsyncQueryContextProcessor)
    row_limit: int = 50000
    samples_row_limit: int = 1000
    cache_default_timeout: int = 300
    csv_export: dict[str, Any] = {}
    excel_export: dict[str, Any] = {}
    data_cache_config: dict[str, Any] = {}
    cache_config: dict[str, Any] = {}
    query_cache_config: dict[str, Any] = {}
    enable_explore_json_csrf_protection: bool = False
    sqllab_ctas_no_limit: bool = False
    sql_max_row: int = 100000
    display_max_row: int = 10000
    sqllab_default_dbid: int | None = None
    default_sqllab_limit: int = 1000

    # Viz / explore_json settings
    mapbox_api_key: str = ""
    default_relative_start_time: str = "today"
    default_relative_end_time: str = "today"
    viz_type_denylist: list[str] = []

    # Session cookie max age (seconds), applied to FlaskSessionDecoder
    session_max_age: int = 2678400  # 31 days in seconds (matches original timedelta(days=31))

    # Redis (used for auth cache and general caching)
    redis_url: str = ""
    csrf_enabled: bool = True
    csrf_cookie_name: str = "csrf_access_token"
    csrf_header_name: str = "X-CSRFToken"
    session_cookie_name: str = "session"

    # Auth role names
    auth_role_public: str = "Public"
    auth_role_admin: str = "Admin"
    guest_role_name: str = "Guest"

    # Security headers
    content_security_policy: str = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; "
        "font-src 'self' data:; "
        "connect-src 'self' ws: wss:; "
        "worker-src 'self' blob:"
    )

    # Rate limiting
    rate_limit_per_minute: int = 100
    rate_limit_window_seconds: int = 60

    # User-provided feature flag overrides.  Merged on top of
    # _DEFAULT_FEATURE_FLAGS via model_validator below.
    feature_flags: dict[str, bool] = {}

    # DASHBOARD_RBAC feature flag
    dashboard_rbac: bool = False

    # Embedded dashboards (guest tokens)
    embedded_superset: bool = False
    guest_token_jwt_secret: str = ""
    guest_token_jwt_algo: str = "HS256"  # noqa: S105
    guest_token_jwt_exp_seconds: int = 3600
    guest_token_header_name: str = "Authorization"  # noqa: S105
    guest_token_validator_hook: Any | None = None

    # ── UI / Branding ──
    app_name: str = "Superset"
    app_icon: str = "/static/assets/images/superset-logo-horiz.png"
    logo_target_path: str | None = None
    logo_tooltip: str = ""
    logo_right_text: Any = ""  # Can be str or Callable
    favicons: list[dict[str, str]] = [
        {"href": "/static/assets/images/favicon.png"}
    ]

    # ── Version / Build ──
    version_string: str = ""
    version_sha: str = ""
    build_number: str | None = None

    # ── Help / Docs ──
    bug_report_url: str | None = None
    bug_report_text: str = "Report a bug"
    bug_report_icon: str | None = None
    documentation_url: str | None = None
    documentation_text: str = "Documentation"
    documentation_icon: str | None = None

    # ── Authentication ──
    auth_type: int = 1  # 1=AUTH_DB, 2=AUTH_LDAP, 3=AUTH_REMOTE_USER, 4=AUTH_OAUTH
    auth_user_registration: bool = False
    auth_user_registration_role: str = "Public"
    public_role_like: str | None = None
    api_login_allow_multiple_providers: bool = False
    oauth_providers: list[dict[str, Any]] = []
    recaptcha_public_key: str = ""
    custom_security_manager: Any | None = None

    # ── Session cookies ──
    session_cookie_httponly: bool = True
    session_cookie_secure: bool = False
    session_cookie_samesite: str | None = "Lax"

    # ── JWT ──
    jwt_access_token_expires: int = 900  # seconds (15 min)

    # ── CSRF ──
    wtf_csrf_enabled: bool = True
    wtf_csrf_time_limit: int = 604800  # 7 days in seconds

    # ── Internationalization ──
    languages: dict[str, dict[str, str]] = {
        "en": {"flag": "us", "name": "English", "url": "/lang/en"},
    }

    # ── Theme ──
    theme_default: Any = {"algorithm": "default"}  # Can be dict or Callable
    theme_dark: Any = {"algorithm": "dark"}  # Can be dict or Callable
    enable_ui_theme_administration: bool = True

    # ── D3 / Visualization ──
    d3_format: dict[str, Any] = {}
    d3_time_format: dict[str, Any] = {}
    currencies: list[str] = [
        "USD", "EUR", "GBP", "INR", "MXN", "JPY", "CNY",
    ]
    deckgl_base_map: Any | None = None
    extra_categorical_color_schemes: list[Any] = []
    extra_sequential_color_schemes: list[Any] = []

    # ── Misc frontend ──
    application_root: str = "/"
    has_gsheets_installed: bool = False
    welcome_page_last_tab: str = "all"
    environment_tag_config: dict[str, Any] = {
        "variable": "SUPERSET_ENV",
        "values": {
            "debug": {"color": "error", "text": "flask-debug"},
            "development": {"color": "error", "text": "Development"},
            "production": {"color": "", "text": ""},
        },
    }
    common_bootstrap_overrides_func: Any | None = None  # Callable or None

    # ── Webserver ──
    superset_webserver_timeout: int = 60
    superset_webserver_domains: list[str] | None = None

    # ── Dashboard ──
    superset_dashboard_position_data_limit: int = 65535
    superset_dashboard_periodical_refresh_limit: int = 0
    superset_dashboard_periodical_refresh_warning_message: str | None = None
    dashboard_auto_refresh_mode: str = "force"
    dashboard_auto_refresh_intervals: list[list[Any]] = [
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
    ]

    # ── SQL Lab extended ──
    sqllab_save_warning_message: str | None = None
    sqllab_query_result_timeout: int = 0
    default_viz_type: str = "table"
    default_time_filter: str = "No filter"
    scheduled_queries: dict[str, Any] = {}

    # ── File extensions ──
    excel_extensions: set[str] = {"xlsx", "xls"}
    csv_extensions: set[str] = {"csv", "tsv", "txt"}
    columnar_extensions: set[str] = {"parquet", "zip"}
    allowed_extensions: set[str] = {
        "xlsx", "xls", "csv", "tsv", "txt", "parquet", "zip",
    }

    # ── HTML ──
    html_sanitization: bool = True
    html_sanitization_schema_extensions: dict[str, Any] = {}

    # ── Alerts / Reports defaults ──
    alert_reports_default_cron_value: str = "0 0 * * *"
    alert_reports_default_retention: int = 90
    alert_reports_default_working_timeout: int = 3600

    # ── Native filters ──
    native_filter_default_row_limit: int = 1000

    # ── Client retry ──
    superset_client_retry_attempts: int = 3
    superset_client_retry_delay: int = 1000
    superset_client_retry_backoff_multiplier: int = 2
    superset_client_retry_max_delay: int = 10000
    superset_client_retry_jitter_max: int = 1000
    superset_client_retry_status_codes: list[int] = [502, 503, 504]

    # ── Misc FRONTEND_CONF ──
    prevent_unsafe_default_urls_on_dataset: bool = True
    jwt_access_csrf_cookie_name: str = "access_csrf_token"
    sync_db_permissions_in_async_mode: bool = False
    table_viz_max_row_server: int = 500000
    enable_javascript_controls: bool = False

    # ── SQLAlchemy docs ──
    sqlalchemy_docs_url: str = "https://docs.sqlalchemy.org/en/latest/"
    sqlalchemy_display_text: str = "Change your database"

    # ── Global Async Queries ──
    global_async_queries_transport: str = "polling"
    global_async_queries_polling_delay: int = 500
    global_async_queries_websocket_url: str = "ws://127.0.0.1:8080/"

    # ── SQL Validators ──
    sql_validators_by_engine: dict[str, str] = {}

    # ── SMTP / Email ──
    smtp_host: str = "localhost"
    smtp_port: int = 25
    smtp_user: str = "superset"
    smtp_password: str = "superset"
    smtp_mail_from: str = "superset@superset.com"
    smtp_starttls: bool = True
    smtp_ssl: bool = False
    smtp_ssl_server_auth: bool = False
    email_reports_subject_prefix: str = "[Report] "
    email_reports_cta: str = "Explore in Superset"

    # ── Slack ──
    slack_api_token: Any | None = None  # Can be str or Callable

    # ── Infrastructure ──
    advanced_data_types: dict[str, Any] = {}
    max_ws_per_user: int = 5

    # ── Alert/Report notification dry run ──
    alert_reports_notification_dry_run: bool = False

    # ── Feature flag functions (advanced) ──
    get_feature_flags_func: Any | None = None  # Callable[[dict], dict] | None
    is_feature_enabled_func: Any | None = None  # Callable[[str], bool] | None

    @field_validator("secret_key")
    @classmethod
    def validate_secret_key_length(cls, v: SecretStr) -> SecretStr:
        if len(v.get_secret_value()) < 16:
            raise ValueError("secret_key must be at least 16 characters long")
        return v

    @field_validator("sqlalchemy_database_uri")
    @classmethod
    def convert_to_async_driver(cls, v: str) -> str:
        for sync_prefix, async_prefix in _SYNC_TO_ASYNC_DRIVERS.items():
            if v.startswith(sync_prefix):
                return v.replace(sync_prefix, async_prefix, 1)
        return v

    @field_validator("session_max_age", mode="before")
    @classmethod
    def coerce_session_max_age(cls, v: Any) -> int:
        """Accept timedelta (original Superset format) or int (seconds)."""
        if hasattr(v, "total_seconds"):
            return int(v.total_seconds())
        return int(v)

    @model_validator(mode="after")
    def _merge_feature_flags(self) -> SupersetSettings:
        """Merge feature flags: defaults <- user config <- SUPERSET_FEATURE_* env vars.

        Mirrors the original Superset behaviour:
        1. Start with _DEFAULT_FEATURE_FLAGS
        2. Merge user-provided FEATURE_FLAGS (from superset_config.py)
        3. Merge SUPERSET_FEATURE_* env vars (highest priority)
        """
        import re

        merged = _DEFAULT_FEATURE_FLAGS.copy()
        merged.update(self.feature_flags)
        # Apply SUPERSET_FEATURE_* env vars (matches original config.py:647-653)
        for k, v in os.environ.items():
            if re.match(r"^SUPERSET_FEATURE_\w+", k):
                flag_name = k[len("SUPERSET_FEATURE_"):]
                merged[flag_name] = v.lower() in ("true", "1", "yes", "y", "on")
        self.feature_flags = merged
        return self

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            SupersetConfigSettingsSource(settings_cls),
            file_secret_settings,
        )

    @classmethod
    def from_superset_config(cls, config_path: str | None = None) -> SupersetSettings:
        """Load settings from superset_config.py.

        Deprecated: use SUPERSET_CONFIG_PATH env var.
        """
        path = config_path or os.environ.get("SUPERSET_CONFIG_PATH")
        if not path or not Path(path).exists():
            raise FileNotFoundError(
                f"Superset config not found at {path}. "
                "Set LITESET_SECRET_KEY env var or provide a valid config path."
            )

        spec = importlib.util.spec_from_file_location("superset_config", path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot load config from {path}")

        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            raise ImportError(
                f"Failed to load superset_config.py from {path}: {exc}"
            ) from exc

        kwargs: dict[str, Any] = {}
        for config_key, settings_field in _SUPERSET_TO_LITESET.items():
            value = getattr(module, config_key, None)
            if value is not None:
                kwargs[settings_field] = value

        if "secret_key" not in kwargs:
            raise ValueError("SECRET_KEY not found in superset config file")

        return cls(**kwargs)
