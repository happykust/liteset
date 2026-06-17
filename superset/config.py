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
from importlib.resources import files
from pathlib import Path
from typing import Any

from celery.schedules import crontab
from pandas._libs.parsers import STR_NA_VALUES
from pydantic import field_validator, model_validator, SecretStr
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from superset.advanced_data_type.plugins.internet_address import internet_address
from superset.advanced_data_type.plugins.internet_port import internet_port
from superset.tasks.types import ExecutorType

SecretKeyStr = SecretStr

BASE_DIR = str(files("superset"))
if "SUPERSET_HOME" in os.environ:
    DATA_DIR = os.environ["SUPERSET_HOME"]
else:
    DATA_DIR = os.path.expanduser("~/.superset")

# superset_test_config.py does ``from superset.config import *`` then
# ``**FEATURE_FLAGS``
FEATURE_FLAGS: dict[str, bool] = {}

import logging as _logging  # noqa: E402

logger = _logging.getLogger(__name__)

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
    "DEBUG": "debug",
    "TESTING": "testing",
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
    "REDIS_URL": "redis_url",
    "MAPBOX_API_KEY": "mapbox_api_key",
    "DEFAULT_RELATIVE_START_TIME": "default_relative_start_time",
    "DEFAULT_RELATIVE_END_TIME": "default_relative_end_time",
    "VIZ_TYPE_DENYLIST": "viz_type_denylist",
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
    "AUTH_TYPE": "auth_type",
    "AUTH_USERNAME_CI": "auth_username_ci",
    "AUTH_USER_REGISTRATION": "auth_user_registration",
    "AUTH_USER_REGISTRATION_ROLE": "auth_user_registration_role",
    "AUTH_ROLES_MAPPING": "auth_roles_mapping",
    "AUTH_ROLES_SYNC_AT_LOGIN": "auth_roles_sync_at_login",
    "AUTH_LDAP_SERVER": "auth_ldap_server",
    "AUTH_LDAP_SEARCH": "auth_ldap_search",
    "AUTH_LDAP_SEARCH_FILTER": "auth_ldap_search_filter",
    "AUTH_LDAP_APPEND_DOMAIN": "auth_ldap_append_domain",
    "AUTH_LDAP_USERNAME_FORMAT": "auth_ldap_username_format",
    "AUTH_LDAP_BIND_USER": "auth_ldap_bind_user",
    "AUTH_LDAP_BIND_PASSWORD": "auth_ldap_bind_password",
    "AUTH_LDAP_USE_TLS": "auth_ldap_use_tls",
    "AUTH_LDAP_ALLOW_SELF_SIGNED": "auth_ldap_allow_self_signed",
    "AUTH_LDAP_TLS_DEMAND": "auth_ldap_tls_demand",
    "AUTH_LDAP_TLS_CACERTDIR": "auth_ldap_tls_cacertdir",
    "AUTH_LDAP_TLS_CACERTFILE": "auth_ldap_tls_cacertfile",
    "AUTH_LDAP_TLS_CERTFILE": "auth_ldap_tls_certfile",
    "AUTH_LDAP_TLS_KEYFILE": "auth_ldap_tls_keyfile",
    "AUTH_LDAP_UID_FIELD": "auth_ldap_uid_field",
    "AUTH_LDAP_GROUP_FIELD": "auth_ldap_group_field",
    "AUTH_LDAP_FIRSTNAME_FIELD": "auth_ldap_firstname_field",
    "AUTH_LDAP_LASTNAME_FIELD": "auth_ldap_lastname_field",
    "AUTH_LDAP_EMAIL_FIELD": "auth_ldap_email_field",
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
    "SUPERSET_WEBSERVER_TIMEOUT": "superset_webserver_timeout",
    "SUPERSET_WEBSERVER_DOMAINS": "superset_webserver_domains",
    "SUPERSET_DASHBOARD_POSITION_DATA_LIMIT": "superset_dashboard_position_data_limit",
    "SUPERSET_DASHBOARD_PERIODICAL_REFRESH_LIMIT": (
        "superset_dashboard_periodical_refresh_limit"
    ),
    "SUPERSET_DASHBOARD_PERIODICAL_REFRESH_WARNING_MESSAGE": (
        "superset_dashboard_periodical_refresh_warning_message"
    ),
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
    "SUPERSET_CLIENT_RETRY_BACKOFF_MULTIPLIER": (
        "superset_client_retry_backoff_multiplier"
    ),
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
    "GET_FEATURE_FLAGS_FUNC": "get_feature_flags_func",
    "IS_FEATURE_ENABLED_FUNC": "is_feature_enabled_func",
    "AUTH_ROLE_PUBLIC": "auth_role_public",
    "AUTH_ROLE_ADMIN": "auth_role_admin",
    "GUEST_ROLE_NAME": "guest_role_name",
    "GUEST_TOKEN_JWT_SECRET": "guest_token_jwt_secret",
    "GUEST_TOKEN_JWT_ALGO": "guest_token_jwt_algo",
    "GUEST_TOKEN_JWT_EXP_SECONDS": "guest_token_jwt_exp_seconds",
    "GUEST_TOKEN_HEADER_NAME": "guest_token_header_name",
    "GUEST_TOKEN_VALIDATOR_HOOK": "guest_token_validator_hook",
    "SQLLAB_CTAS_NO_LIMIT": "sqllab_ctas_no_limit",
    "SQLLAB_DEFAULT_DBID": "sqllab_default_dbid",
    "STATS_LOGGER": "stats_logger",
    "EVENT_LOGGER": "event_logger",
    "SUPERSET_LOG_VIEW": "superset_log_view",
    "SUPERSET_SECURITY_VIEW_MENU": "superset_security_view_menu",
    "ALEMBIC_SKIP_LOG_CONFIG": "alembic_skip_log_config",
    "VERSION_SHA_LENGTH": "version_sha_length",
    "FILTER_SELECT_ROW_LIMIT": "filter_select_row_limit",
    "SQLALCHEMY_TRACK_MODIFICATIONS": "sqlalchemy_track_modifications",
    "SQLALCHEMY_ENGINE_OPTIONS": "sqlalchemy_engine_options",
    "SQLALCHEMY_CUSTOM_PASSWORD_STORE": "sqlalchemy_custom_password_store",
    "SQLALCHEMY_ENCRYPTED_FIELD_TYPE_ADAPTER": (
        "sqlalchemy_encrypted_field_type_adapter"
    ),
    "SQLGLOT_DIALECTS_EXTENSIONS": "sqlglot_dialects_extensions",
    "QUERY_SEARCH_LIMIT": "query_search_limit",
    "WTF_CSRF_EXEMPT_LIST": "wtf_csrf_exempt_list",
    "FLASK_USE_RELOAD": "flask_use_reload",
    "PROFILING": "profiling",
    "SHOW_STACKTRACE": "show_stacktrace",
    "ENABLE_PROXY_FIX": "enable_proxy_fix",
    "PROXY_FIX_CONFIG": "proxy_fix_config",
    "RATELIMIT_ENABLED": "ratelimit_enabled",
    "RATELIMIT_APPLICATION": "ratelimit_application",
    "AUTH_RATE_LIMITED": "auth_rate_limited",
    "AUTH_RATE_LIMIT": "auth_rate_limit",
    "FAB_API_SWAGGER_UI": "fab_api_swagger_ui",
    "BABEL_DEFAULT_LOCALE": "babel_default_locale",
    "BABEL_DEFAULT_FOLDER": "babel_default_folder",
    "SSH_TUNNEL_MANAGER_CLASS": "ssh_tunnel_manager_class",
    "SSH_TUNNEL_LOCAL_BIND_ADDRESS": "ssh_tunnel_local_bind_address",
    "SSH_TUNNEL_TIMEOUT_SEC": "ssh_tunnel_timeout_sec",
    "SSH_TUNNEL_PACKET_TIMEOUT_SEC": "ssh_tunnel_packet_timeout_sec",
    "CUSTOM_FONT_URLS": "custom_font_urls",
    "CACHE_WARMUP_EXECUTORS": "cache_warmup_executors",
    "THUMBNAIL_EXECUTORS": "thumbnail_executors",
    "THUMBNAIL_DASHBOARD_DIGEST_FUNC": "thumbnail_dashboard_digest_func",
    "THUMBNAIL_CHART_DIGEST_FUNC": "thumbnail_chart_digest_func",
    "THUMBNAIL_CACHE_CONFIG": "thumbnail_cache_config",
    "THUMBNAIL_ERROR_CACHE_TTL": "thumbnail_error_cache_ttl",
    "SCREENSHOT_LOCATE_WAIT": "screenshot_locate_wait",
    "SCREENSHOT_LOAD_WAIT": "screenshot_load_wait",
    "SCREENSHOT_SELENIUM_RETRIES": "screenshot_selenium_retries",
    "SCREENSHOT_SELENIUM_HEADSTART": "screenshot_selenium_headstart",
    "SCREENSHOT_SELENIUM_ANIMATION_WAIT": "screenshot_selenium_animation_wait",
    "SCREENSHOT_REPLACE_UNEXPECTED_ERRORS": "screenshot_replace_unexpected_errors",
    "SCREENSHOT_WAIT_FOR_ERROR_MODAL_VISIBLE": (
        "screenshot_wait_for_error_modal_visible"
    ),
    "SCREENSHOT_WAIT_FOR_ERROR_MODAL_INVISIBLE": (
        "screenshot_wait_for_error_modal_invisible"
    ),
    "SCREENSHOT_PLAYWRIGHT_WAIT_EVENT": "screenshot_playwright_wait_event",
    "SCREENSHOT_PLAYWRIGHT_DEFAULT_TIMEOUT": "screenshot_playwright_default_timeout",
    "SCREENSHOT_TILED_ENABLED": "screenshot_tiled_enabled",
    "SCREENSHOT_TILED_CHART_THRESHOLD": "screenshot_tiled_chart_threshold",
    "SCREENSHOT_TILED_HEIGHT_THRESHOLD": "screenshot_tiled_height_threshold",
    "SCREENSHOT_TILED_VIEWPORT_HEIGHT": "screenshot_tiled_viewport_height",
    "UPLOAD_FOLDER": "upload_folder",
    "UPLOAD_CHUNK_SIZE": "upload_chunk_size",
    "FILTER_STATE_CACHE_CONFIG": "filter_state_cache_config",
    "EXPLORE_FORM_DATA_CACHE_CONFIG": "explore_form_data_cache_config",
    "STORE_CACHE_KEYS_IN_METADATA_DB": "store_cache_keys_in_metadata_db",
    "ENABLE_CORS": "enable_cors",
    "CORS_OPTIONS": "cors_options",
    "TIME_GRAIN_DENYLIST": "time_grain_denylist",
    "TIME_GRAIN_ADDONS": "time_grain_addons",
    "TIME_GRAIN_ADDON_EXPRESSIONS": "time_grain_addon_expressions",
    "TIME_GRAIN_JOIN_COLUMN_PRODUCERS": "time_grain_join_column_producers",
    "DEFAULT_MODULE_DS_MAP": "default_module_ds_map",
    "ADDITIONAL_MODULE_DS_MAP": "additional_module_ds_map",
    "ADDITIONAL_MIDDLEWARE": "additional_middleware",
    "LOGGING_CONFIGURATOR": "logging_configurator",
    "LOG_FORMAT": "log_format",
    # LOG_LEVEL is an int in upstream config (e.g. logging.INFO = 20).
    # Map it to log_level (str) — the field_validator below accepts both
    # int and str and converts int via logging.getLevelName().
    "LOG_LEVEL": "log_level",
    "ENABLE_TIME_ROTATE": "enable_time_rotate",
    "TIME_ROTATE_LOG_LEVEL": "time_rotate_log_level",
    "FILENAME": "log_filename",
    "ROLLOVER": "rollover",
    "INTERVAL": "log_interval",
    "BACKUP_COUNT": "backup_count",
    "QUERY_LOGGER": "query_logger",
    "SUPERSET_META_DB_LIMIT": "superset_meta_db_limit",
    "SQLLAB_SCHEDULE_WARNING_MESSAGE": "sqllab_schedule_warning_message",
    "SQLLAB_PAYLOAD_MAX_MB": "sqllab_payload_max_mb",
    "SQLLAB_TIMEOUT": "sqllab_timeout",
    "SQLLAB_VALIDATION_TIMEOUT": "sqllab_validation_timeout",
    "SQLLAB_ASYNC_TIME_LIMIT_SEC": "sqllab_async_time_limit_sec",
    "SQLLAB_QUERY_COST_ESTIMATE_TIMEOUT": "sqllab_query_cost_estimate_timeout",
    "QUERY_COST_FORMATTERS_BY_ENGINE": "query_cost_formatters_by_engine",
    "SQLLAB_CTAS_SCHEMA_NAME_FUNC": "sqllab_ctas_schema_name_func",
    "CELERY_BEAT_SCHEDULER_EXPIRES": "celery_beat_scheduler_expires",
    "CELERY_CONFIG": "celery_config",
    # When True, commit the scoped sync session in task_postrun.
    "SQLALCHEMY_COMMIT_ON_TEARDOWN": "sqlalchemy_commit_on_teardown",
    "CELERY_ALWAYS_EAGER": "celery_always_eager",
    "DEFAULT_HTTP_HEADERS": "default_http_headers",
    "OVERRIDE_HTTP_HEADERS": "override_http_headers",
    "HTTP_HEADERS": "http_headers",
    "DEFAULT_DB_ID": "default_db_id",
    "RESULTS_BACKEND": "results_backend",
    "RESULTS_BACKEND_USE_MSGPACK": "results_backend_use_msgpack",
    "CSV_TO_HIVE_UPLOAD_S3_BUCKET": "csv_to_hive_upload_s3_bucket",
    "CSV_TO_HIVE_UPLOAD_DIRECTORY": "csv_to_hive_upload_directory",
    "CSV_TO_HIVE_UPLOAD_DIRECTORY_FUNC": "csv_to_hive_upload_directory_func",
    "UPLOADED_CSV_HIVE_NAMESPACE": "uploaded_csv_hive_namespace",
    "ALLOWED_USER_CSV_SCHEMA_FUNC": "allowed_user_csv_schema_func",
    "CSV_DEFAULT_NA_NAMES": "csv_default_na_names",
    "JINJA_CONTEXT_ADDONS": "jinja_context_addons",
    "CUSTOM_TEMPLATE_PROCESSORS": "custom_template_processors",
    "ROBOT_PERMISSION_ROLES": "robot_permission_roles",
    "FLASK_APP_MUTATOR": "flask_app_mutator",
    "ENABLE_CHUNK_ENCODING": "enable_chunk_encoding",
    "SILENCE_FAB": "silence_fab",
    "FAB_ADD_SECURITY_VIEWS": "fab_add_security_views",
    "FAB_ADD_SECURITY_API": "fab_add_security_api",
    "FAB_ADD_SECURITY_PERMISSION_VIEW": "fab_add_security_permission_view",
    "FAB_ADD_SECURITY_VIEW_MENU_VIEW": "fab_add_security_view_menu_view",
    "FAB_ADD_SECURITY_PERMISSION_VIEWS_VIEW": "fab_add_security_permission_views_view",
    "FAB_PASSWORD_COMPLEXITY_ENABLED": "fab_password_complexity_enabled",
    "FAB_PASSWORD_COMPLEXITY_VALIDATOR": "fab_password_complexity_validator",
    "FAB_PASSWORD_HASH_METHOD": "fab_password_hash_method",
    "FAB_PASSWORD_HASH_SALT_LENGTH": "fab_password_hash_salt_length",
    "TROUBLESHOOTING_LINK": "troubleshooting_link",
    "PERMISSION_INSTRUCTIONS_LINK": "permission_instructions_link",
    "BLUEPRINTS": "blueprints",
    "TRACKING_URL_TRANSFORMER": "tracking_url_transformer",
    "DB_POLL_INTERVAL_SECONDS": "db_poll_interval_seconds",
    "PRESTO_POLL_INTERVAL": "presto_poll_interval",
    "ALLOWED_EXTRA_AUTHENTICATIONS": "allowed_extra_authentications",
    "DASHBOARD_TEMPLATE_ID": "dashboard_template_id",
    "ENGINE_CONTEXT_MANAGER": "engine_context_manager",
    "DB_CONNECTION_MUTATOR": "db_connection_mutator",
    "DB_SQLA_URI_VALIDATOR": "db_sqla_uri_validator",
    "DISALLOWED_SQL_FUNCTIONS": "disallowed_sql_functions",
    "SQL_QUERY_MUTATOR": "sql_query_mutator",
    "MUTATE_AFTER_SPLIT": "mutate_after_split",
    "MUTATE_ALERT_QUERY": "mutate_alert_query",
    "EMAIL_HEADER_MUTATOR": "email_header_mutator",
    "EXCLUDE_USERS_FROM_LISTS": "exclude_users_from_lists",
    "DBS_AVAILABLE_DENYLIST": "dbs_available_denylist",
    "MACHINE_AUTH_PROVIDER_CLASS": "machine_auth_provider_class",
    "ALERT_REPORTS_CRON_WINDOW_SIZE": "alert_reports_cron_window_size",
    "ALERT_REPORTS_WORKING_TIME_OUT_KILL": "alert_reports_working_time_out_kill",
    "ALERT_REPORTS_EXECUTORS": "alert_reports_executors",
    "ALERT_REPORTS_WORKING_TIME_OUT_LAG": ("alert_reports_working_time_out_lag"),
    "ALERT_REPORTS_WORKING_SOFT_TIME_OUT_LAG": (
        "alert_reports_working_soft_time_out_lag"
    ),
    "ALERT_REPORTS_QUERY_EXECUTION_MAX_TRIES": (
        "alert_reports_query_execution_max_tries"
    ),
    "ALERT_REPORTS_MIN_CUSTOM_SCREENSHOT_WIDTH": (
        "alert_reports_min_custom_screenshot_width"
    ),
    "ALERT_REPORTS_MAX_CUSTOM_SCREENSHOT_WIDTH": (
        "alert_reports_max_custom_screenshot_width"
    ),
    "ALERT_MINIMUM_INTERVAL": "alert_minimum_interval",
    "REPORT_MINIMUM_INTERVAL": "report_minimum_interval",
    "SLACK_PROXY": "slack_proxy",
    "SLACK_CACHE_TIMEOUT": "slack_cache_timeout",
    "SLACK_API_RATE_LIMIT_RETRY_COUNT": "slack_api_rate_limit_retry_count",
    "WEBDRIVER_TYPE": "webdriver_type",
    "WEBDRIVER_WINDOW": "webdriver_window",
    "WEBDRIVER_AUTH_FUNC": "webdriver_auth_func",
    "WEBDRIVER_CONFIGURATION": "webdriver_configuration",
    "WEBDRIVER_OPTION_ARGS": "webdriver_option_args",
    "WEBDRIVER_BASEURL": "webdriver_baseurl",
    "WEBDRIVER_BASEURL_USER_FRIENDLY": "webdriver_baseurl_user_friendly",
    "EMAIL_PAGE_RENDER_WAIT": "email_page_render_wait",
    "PREFERRED_DATABASES": "preferred_databases",
    "TEST_DATABASE_CONNECTION_TIMEOUT": "test_database_connection_timeout",
    "DATABASE_OAUTH2_CLIENTS": "database_oauth2_clients",
    "DATABASE_OAUTH2_JWT_ALGORITHM": "database_oauth2_jwt_algorithm",
    "DATABASE_OAUTH2_TIMEOUT": "database_oauth2_timeout",
    "DATABASE_OAUTH2_REDIRECT_URI": "database_oauth2_redirect_uri",
    "CONTENT_SECURITY_POLICY_WARNING": "content_security_policy_warning",
    "TALISMAN_ENABLED": "talisman_enabled",
    "TALISMAN_CONFIG": "talisman_config",
    "TALISMAN_DEV_CONFIG": "talisman_dev_config",
    "SESSION_SERVER_SIDE": "session_server_side",
    "SEND_FILE_MAX_AGE_DEFAULT": "send_file_max_age_default",
    "PREVENT_UNSAFE_DB_CONNECTIONS": "prevent_unsafe_db_connections",
    "DATASET_IMPORT_ALLOWED_DATA_URLS": "dataset_import_allowed_data_urls",
    "SSL_CERT_PATH": "ssl_cert_path",
    "SQLA_TABLE_MUTATOR": "sqla_table_mutator",
    "GLOBAL_ASYNC_QUERY_MANAGER_CLASS": "global_async_query_manager_class",
    "GLOBAL_ASYNC_QUERIES_REDIS_STREAM_PREFIX": (
        "global_async_queries_redis_stream_prefix"
    ),
    "GLOBAL_ASYNC_QUERIES_REDIS_STREAM_LIMIT": (
        "global_async_queries_redis_stream_limit"
    ),
    "GLOBAL_ASYNC_QUERIES_REDIS_STREAM_LIMIT_FIREHOSE": (
        "global_async_queries_redis_stream_limit_firehose"
    ),
    "GLOBAL_ASYNC_QUERIES_REGISTER_REQUEST_HANDLERS": (
        "global_async_queries_register_request_handlers"
    ),
    "GLOBAL_ASYNC_QUERIES_JWT_COOKIE_NAME": ("global_async_queries_jwt_cookie_name"),
    "GLOBAL_ASYNC_QUERIES_JWT_COOKIE_SECURE": (
        "global_async_queries_jwt_cookie_secure"
    ),
    "GLOBAL_ASYNC_QUERIES_JWT_COOKIE_SAMESITE": (
        "global_async_queries_jwt_cookie_samesite"
    ),
    "GLOBAL_ASYNC_QUERIES_JWT_COOKIE_DOMAIN": "global_async_queries_jwt_cookie_domain",
    "GLOBAL_ASYNC_QUERIES_JWT_SECRET": "global_async_queries_jwt_secret",
    "GLOBAL_ASYNC_QUERIES_CACHE_BACKEND": "global_async_queries_cache_backend",
    "GUEST_TOKEN_JWT_AUDIENCE": "guest_token_jwt_audience",
    "DATASET_HEALTH_CHECK": "dataset_health_check",
    "ZIPPED_FILE_MAX_SIZE": "zipped_file_max_size",
    "ZIP_FILE_MAX_COMPRESS_RATIO": "zip_file_max_compress_ratio",
    "EXTRA_RELATED_QUERY_FILTERS": "extra_related_query_filters",
    "EXTRA_DYNAMIC_QUERY_FILTERS": "extra_dynamic_query_filters",
    "CATALOGS_SIMPLIFIED_MIGRATION": "catalogs_simplified_migration",
    "USER_AGENT_FUNC": "user_agent_func",
}


_superset_config_cache: dict[str, dict[str, Any]] = {}

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


def _parse_boolean_string(bool_str: str | None) -> bool:
    """Port of ``superset.utils.core.parse_boolean_string`` (1:1).

    Inlined here (rather than imported) because ``config.py`` is loaded very
    early — before ``superset.utils.core`` and its heavy dependencies — and we
    must avoid an import cycle at settings-construction time.
    """
    if bool_str is None:
        return False
    return bool_str.lower() in ("y", "Y", "yes", "True", "t", "true", "On", "on", "1")


def _cast_to_boolean(value: Any) -> bool | None:
    """Port of ``superset.utils.core.cast_to_boolean`` (1:1)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return False


# Legacy (unprefixed) environment variables honored by upstream Superset's
# ``config.py`` module body.  Pydantic-settings only reads ``LITESET_``-prefixed
# vars, so this map preserves drop-in compatibility for existing deployments
# (e.g. ``SUPERSET_SECRET_KEY`` injected via k8s/helm secrets).
#
# Each entry maps the env var name to the target settings field plus a parser
# matching the original inline expressions:
#   SECRET_KEY        = os.environ.get("SUPERSET_SECRET_KEY") or CHANGE_ME...
#   DEBUG             = parse_boolean_string(os.environ.get("FLASK_DEBUG"))
#   MAPBOX_API_KEY    = os.environ.get("MAPBOX_API_KEY", "")
#   TALISMAN_ENABLED  = cast_to_boolean(os.environ.get("TALISMAN_ENABLED", True))
#   RATELIMIT_ENABLED = os.environ.get("SUPERSET_ENV") == "production"
_LEGACY_ENV_VARS: dict[str, tuple[str, Any]] = {
    "SUPERSET_SECRET_KEY": ("secret_key", lambda v: v),
    "FLASK_DEBUG": ("debug", _parse_boolean_string),
    "MAPBOX_API_KEY": ("mapbox_api_key", lambda v: v),
    "TALISMAN_ENABLED": ("talisman_enabled", _cast_to_boolean),
    "SUPERSET_ENV": ("ratelimit_enabled", lambda v: v == "production"),
}


class SupersetConfigSettingsSource(PydanticBaseSettingsSource):
    """Read settings from superset_config.py as a Pydantic settings source.

    Resolution order:
      1. ``SUPERSET_CONFIG_PATH`` env var pointing at a file (``pex``-style), or
      2. an importable ``superset_config`` module on the ``PYTHONPATH``.

    Caches loaded values per config source to avoid re-executing the
    config file on every ``SupersetSettings()`` construction.
    """

    def __init__(self, settings_cls: type[BaseSettings]) -> None:
        super().__init__(settings_cls)
        self._values: dict[str, Any] = self._load()

    @staticmethod
    def _load_module_from_path(path: str) -> tuple[Any, str] | None:
        """Load a config module from an explicit file path.

        Returns ``(module, cache_key)`` on success, or ``None`` if the path
        does not exist or cannot be loaded as a module spec.  Raises
        ``ImportError`` if the module exists but its execution fails.
        """
        if not Path(path).exists():
            return None
        spec = importlib.util.spec_from_file_location("superset_config", path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            raise ImportError(
                f"Failed to load superset_config.py from {path}: {exc}"
            ) from exc
        return module, path

    @staticmethod
    def _load() -> dict[str, Any]:
        path = os.environ.get("SUPERSET_CONFIG_PATH", "")
        if path:
            # 1) Explicit file path (useful when the app runs via pex and the
            #    config module is not on the PYTHONPATH).
            cache_key = path
            if cache_key in _superset_config_cache:
                return _superset_config_cache[cache_key]
            result = SupersetConfigSettingsSource._load_module_from_path(path)
            if result is None:
                return {}
            module, cache_key = result
        elif importlib.util.find_spec("superset_config") is not None:
            # 2) ``superset_config`` importable on the PYTHONPATH.  This is the
            #    default mechanism for existing Superset installations (docker
            #    images and most pip/k8s deployments rely on it without setting
            #    SUPERSET_CONFIG_PATH).  Mirrors the ``find_spec`` branch of
            #    upstream config.py.
            cache_key = "superset_config"
            if cache_key in _superset_config_cache:
                return _superset_config_cache[cache_key]
            try:
                import superset_config

                module = superset_config
            except Exception as exc:
                raise ImportError(
                    f"Found but failed to import local superset_config: {exc}"
                ) from exc
        else:
            return {}

        values: dict[str, Any] = {}
        for sup_key, lit_key in _SUPERSET_TO_LITESET.items():
            val = getattr(module, sup_key, None)
            if val is not None:
                values[lit_key] = val
        _superset_config_cache[cache_key] = values
        return values

    def get_field_value(
        self, field: FieldInfo, field_name: str
    ) -> tuple[Any, str, bool]:
        if field_name in self._values:
            return self._values[field_name], field_name, True
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        return self._values


class LegacyEnvSettingsSource(PydanticBaseSettingsSource):
    """Honor upstream Superset's unprefixed environment variables.

    Apache Superset reads a handful of plain (unprefixed) env vars in its
    ``config.py`` module body — most importantly ``SUPERSET_SECRET_KEY``.
    Pydantic-settings only reads ``LITESET_``-prefixed vars, so this source
    preserves drop-in compatibility for existing deployments.

    Placed *below* :class:`SupersetConfigSettingsSource` so that
    ``superset_config.py`` overrides these env vars, matching the original
    precedence (the config module body seeds defaults from env, then
    ``from superset_config import *`` overrides them).
    """

    def __init__(self, settings_cls: type[BaseSettings]) -> None:
        super().__init__(settings_cls)
        self._values: dict[str, Any] = {}
        for env_name, (field_name, parser) in _LEGACY_ENV_VARS.items():
            if env_name in os.environ:
                self._values[field_name] = parser(os.environ[env_name])

    def get_field_value(
        self, field: FieldInfo, field_name: str
    ) -> tuple[Any, str, bool]:
        if field_name in self._values:
            return self._values[field_name], field_name, True
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        return self._values


_CELERY_BEAT_SCHEDULER_EXPIRES_SEC = 604800


class CeleryConfig:  # pylint: disable=too-few-public-methods
    broker_url = "sqla+sqlite:///celerydb.sqlite"
    imports = (
        "superset.tasks.sql_lab",
        "superset.tasks.scheduler",
        "superset.tasks.thumbnails",
        "superset.tasks.cache",
        "superset.tasks.slack",
    )
    result_backend = "db+sqlite:///celery_results.sqlite"
    worker_prefetch_multiplier = 1
    task_acks_late = False
    task_annotations = {
        "sql_lab.get_sql_results": {
            "rate_limit": "100/s",
        },
    }
    beat_schedule = {
        "reports.scheduler": {
            "task": "reports.scheduler",
            "schedule": crontab(minute="*", hour="*"),
            "options": {"expires": _CELERY_BEAT_SCHEDULER_EXPIRES_SEC},
        },
        "reports.prune_log": {
            "task": "reports.prune_log",
            "schedule": crontab(minute=0, hour=0),
        },
    }


class SupersetSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LITESET_",
        env_file=".env",
        extra="ignore",
    )

    secret_key: SecretKeyStr
    sqlalchemy_database_uri: str
    # Unconfigured installs get a dedicated SQLite examples DB, not the metadata DB.
    sqlalchemy_examples_uri: str = (
        "sqlite:///"
        + os.path.join(DATA_DIR, "examples.db")
        + "?check_same_thread=false"
    )
    host: str = "0.0.0.0"  # noqa: S104
    port: int = 8088
    debug: bool = False
    testing: bool = False
    static_assets_prefix: str = ""
    global_async_queries: bool = False
    cors_allow_origins: list[str] = []
    log_level: str = "INFO"
    production: bool = False
    row_limit: int = 50000
    samples_row_limit: int = 1000
    cache_default_timeout: int = 86400
    csv_export: dict[str, Any] = {"encoding": "utf-8-sig"}
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
    mapbox_api_key: str = ""
    default_relative_start_time: str = "today"
    default_relative_end_time: str = "today"
    viz_type_denylist: list[str] = []
    # Session cookie max age in seconds, applied to FlaskSessionDecoder.
    session_max_age: int = 2678400
    redis_url: str = ""
    csrf_enabled: bool = True
    csrf_cookie_name: str = "csrf_access_token"
    csrf_header_name: str = "X-CSRFToken"
    session_cookie_name: str = "session"
    auth_role_public: str = "Public"
    auth_role_admin: str = "Admin"
    guest_role_name: str = (
        "Public"  # matches Apache Superset 6.0.0 default; see audit 04-security-auth.md
    )

    content_security_policy: str = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "img-src 'self' data: blob: https://cdn.jsdelivr.net; "
        "font-src 'self' data:; "
        "connect-src 'self' ws: wss:; "
        "worker-src 'self' blob:"
    )

    rate_limit_per_minute: int = 100
    rate_limit_window_seconds: int = 60
    # Merged on top of _DEFAULT_FEATURE_FLAGS via model_validator.
    feature_flags: dict[str, Any] = {}
    dashboard_rbac: bool = False
    embedded_superset: bool = False
    guest_token_jwt_secret: str = "test-guest-secret-change-me"  # noqa: S105
    guest_token_jwt_algo: str = "HS256"  # noqa: S105
    guest_token_jwt_exp_seconds: int = (
        300  # matches Apache Superset 6.0.0 default; see audit 04-security-auth.md
    )
    guest_token_header_name: str = "X-GuestToken"  # noqa: S105
    guest_token_validator_hook: Any | None = None
    app_name: str = "Liteset"
    app_icon: str = "/static/assets/images/liteset-logo-horiz.png"
    logo_target_path: str | None = None
    logo_tooltip: str = "Liteset"
    logo_right_text: Any = ""
    favicons: list[dict[str, str]] = [
        {"href": "/static/assets/images/liteset-favicon.png"}
    ]  # noqa: E501
    version_string: str = ""
    version_sha: str = ""
    build_number: str | None = None
    bug_report_url: str | None = None
    bug_report_text: str = "Report a bug"
    bug_report_icon: str | None = None
    documentation_url: str | None = None
    documentation_text: str = "Documentation"
    documentation_icon: str | None = None
    auth_type: int = 1  # 1=AUTH_DB, 2=AUTH_LDAP, 3=AUTH_REMOTE_USER, 4=AUTH_OAUTH
    # Case-insensitive username lookup (upstream default True). When True a user
    # cannot self-register a case-variant duplicate via OAuth/LDAP.
    auth_username_ci: bool = True
    auth_user_registration: bool = False
    auth_user_registration_role: str = "Public"
    public_role_like: str | None = None
    api_login_allow_multiple_providers: bool = False
    oauth_providers: list[dict[str, Any]] = []
    recaptcha_public_key: str = ""
    custom_security_manager: Any | None = None
    auth_roles_mapping: dict[str, list[str]] = {}
    auth_roles_sync_at_login: bool = False
    # LDAP defaults match the ``setdefault`` calls in ``BaseSecurityManager.__init__``.
    auth_ldap_server: str = ""
    auth_ldap_search: str = ""
    auth_ldap_search_filter: str = ""
    auth_ldap_append_domain: str = ""
    auth_ldap_username_format: str = ""
    auth_ldap_bind_user: str = ""
    auth_ldap_bind_password: str = ""
    auth_ldap_use_tls: bool = False
    auth_ldap_allow_self_signed: bool = False
    auth_ldap_tls_demand: bool = False
    auth_ldap_tls_cacertdir: str = ""
    auth_ldap_tls_cacertfile: str = ""
    auth_ldap_tls_certfile: str = ""
    auth_ldap_tls_keyfile: str = ""
    auth_ldap_uid_field: str = "uid"
    auth_ldap_group_field: str = "memberOf"
    auth_ldap_firstname_field: str = "givenName"
    auth_ldap_lastname_field: str = "sn"
    auth_ldap_email_field: str = "mail"
    session_cookie_httponly: bool = True
    session_cookie_secure: bool = False
    session_cookie_samesite: str | None = "Lax"
    jwt_access_token_expires: int = 900
    wtf_csrf_enabled: bool = True
    wtf_csrf_time_limit: int = 604800
    languages: dict[str, dict[str, str]] = {
        "en": {"flag": "us", "name": "English", "url": "/lang/en"},
    }

    theme_default: Any = {"algorithm": "default"}
    theme_dark: Any = {"algorithm": "dark"}
    enable_ui_theme_administration: bool = True
    d3_format: dict[str, Any] = {}
    d3_time_format: dict[str, Any] = {}
    currencies: list[str] = [
        "USD",
        "EUR",
        "GBP",
        "INR",
        "MXN",
        "JPY",
        "CNY",
    ]
    deckgl_base_map: Any | None = None
    extra_categorical_color_schemes: list[Any] = []
    extra_sequential_color_schemes: list[Any] = []
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
    common_bootstrap_overrides_func: Any | None = None
    superset_webserver_timeout: int = 60
    superset_webserver_domains: list[str] | None = None
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

    sqllab_save_warning_message: str | None = None
    sqllab_query_result_timeout: int = 0
    default_viz_type: str = "table"
    default_time_filter: str = "No filter"
    scheduled_queries: dict[str, Any] = {}
    excel_extensions: set[str] = {"xlsx", "xls"}
    csv_extensions: set[str] = {"csv", "tsv", "txt"}
    columnar_extensions: set[str] = {"parquet", "zip"}
    allowed_extensions: set[str] = {
        "xlsx",
        "xls",
        "csv",
        "tsv",
        "txt",
        "parquet",
        "zip",
    }

    fab_password_hash_method: str = "scrypt"  # noqa: S105
    fab_password_hash_salt_length: int = 16
    html_sanitization: bool = True
    html_sanitization_schema_extensions: dict[str, Any] = {}
    alert_reports_default_cron_value: str = "0 0 * * *"
    alert_reports_default_retention: int = 90
    alert_reports_default_working_timeout: int = 3600
    native_filter_default_row_limit: int = 1000
    superset_client_retry_attempts: int = 3
    superset_client_retry_delay: int = 1000
    superset_client_retry_backoff_multiplier: int = 2
    superset_client_retry_max_delay: int = 10000
    superset_client_retry_jitter_max: int = 1000
    superset_client_retry_status_codes: list[int] = [502, 503, 504]
    prevent_unsafe_default_urls_on_dataset: bool = True
    jwt_access_csrf_cookie_name: str = "access_csrf_token"
    sync_db_permissions_in_async_mode: bool = False
    table_viz_max_row_server: int = 500000
    enable_javascript_controls: bool = False
    sqlalchemy_docs_url: str = "https://docs.sqlalchemy.org/en/latest/"
    sqlalchemy_display_text: str = "Change your database"
    global_async_queries_transport: str = "polling"
    global_async_queries_polling_delay: int = 500
    # Liteset folds the WebSocket relay INTO the main ASGI app (controller
    # ``AsyncQueryWebSocket`` at ``/ws/events``) — there is no separate Node
    # ``superset-websocket`` sidecar. Upstream's default
    # (``ws://127.0.0.1:8080/``) pointed at that now-removed sidecar and can
    # never work here, so the default points at the main app's ``/ws/events``
    # path on the canonical Superset port. Deployers behind a proxy / on a
    # different host or port must override this to their external app URL
    # + ``/ws/events`` (wss:// under TLS).
    global_async_queries_websocket_url: str = "ws://127.0.0.1:8088/ws/events"

    # Ships the presto/postgres validators on by default so "Validate SQL"
    # works out of the box for those engines.
    sql_validators_by_engine: dict[str, str] = {  # noqa: RUF012
        "presto": "PrestoDBSQLValidator",
        "postgresql": "PostgreSQLValidator",
    }

    smtp_host: str = "localhost"
    smtp_port: int = 25
    smtp_user: str = "superset"
    smtp_password: str = "superset"  # noqa: S105
    smtp_mail_from: str = "superset@superset.com"
    smtp_starttls: bool = True
    smtp_ssl: bool = False
    smtp_ssl_server_auth: bool = False
    email_reports_subject_prefix: str = "[Report] "
    email_reports_cta: str = "Explore in Superset"
    slack_api_token: Any | None = None
    advanced_data_types: dict[str, Any] = {
        "internet_address": internet_address,
        "port": internet_port,
    }
    max_ws_per_user: int = 5
    alert_reports_notification_dry_run: bool = False
    get_feature_flags_func: Any | None = None
    is_feature_enabled_func: Any | None = None

    stats_logger: Any = None
    event_logger: Any = None
    superset_log_view: bool = True
    superset_security_view_menu: bool = True
    alembic_skip_log_config: bool = False
    version_sha_length: int = 8
    filter_select_row_limit: int = 10000
    sqlalchemy_track_modifications: bool = False
    sqlalchemy_engine_options: dict[str, Any] = {}
    sqlalchemy_custom_password_store: Any | None = None
    sqlalchemy_encrypted_field_type_adapter: Any | None = None
    sqlglot_dialects_extensions: Any = {}
    query_search_limit: int = 1000
    wtf_csrf_exempt_list: list[str] = [
        "superset.charts.data.api.data",
        "superset.dashboards.api.cache_dashboard_screenshot",
        "superset.views.core.explore_json",
        "superset.views.core.log",
        "superset.views.datasource.views.samples",
    ]

    flask_use_reload: bool = True
    profiling: bool = False
    show_stacktrace: bool = False
    enable_proxy_fix: bool = False
    proxy_fix_config: dict[str, int] = {
        "x_for": 1,
        "x_proto": 1,
        "x_host": 1,
        "x_port": 1,
        "x_prefix": 1,
    }

    ratelimit_enabled: bool = False
    ratelimit_application: str = "50 per second"
    auth_rate_limited: bool = True
    auth_rate_limit: str = "5 per second"
    fab_api_swagger_ui: bool = True
    babel_default_locale: str = "en"
    babel_default_folder: str = "superset/translations"

    # ``superset.extensions.ssh.SSHManager`` path is preserved because it
    # actually exists in the new tree at ``superset/extensions/ssh.py:SSHManager``.
    ssh_tunnel_manager_class: str = "superset.extensions.ssh.SSHManager"
    ssh_tunnel_local_bind_address: str = "127.0.0.1"
    ssh_tunnel_timeout_sec: float = 10.0
    ssh_tunnel_packet_timeout_sec: float = 1.0
    custom_font_urls: list[str] = []
    cache_warmup_executors: list[Any] = [ExecutorType.OWNER]
    thumbnail_executors: list[Any] = [ExecutorType.CURRENT_USER]
    thumbnail_dashboard_digest_func: Any | None = None
    thumbnail_chart_digest_func: Any | None = None
    thumbnail_cache_config: dict[str, Any] = {
        "CACHE_TYPE": "NullCache",
        "CACHE_DEFAULT_TIMEOUT": 604800,
        "CACHE_NO_NULL_WARNING": True,
    }
    thumbnail_error_cache_ttl: int = 86400
    screenshot_locate_wait: int = 10
    screenshot_load_wait: int = 60
    screenshot_selenium_retries: int = 5
    screenshot_selenium_headstart: int = 3
    screenshot_selenium_animation_wait: int = 5
    screenshot_replace_unexpected_errors: bool = False
    screenshot_wait_for_error_modal_visible: int = 5
    screenshot_wait_for_error_modal_invisible: int = 5
    screenshot_playwright_wait_event: str = "domcontentloaded"
    screenshot_playwright_default_timeout: int = 60000  # milliseconds
    screenshot_tiled_enabled: bool = True
    screenshot_tiled_chart_threshold: int = 20
    screenshot_tiled_height_threshold: int = 5000
    screenshot_tiled_viewport_height: int = 2000
    upload_folder: str = "/static/uploads/"
    upload_chunk_size: int = 4096
    filter_state_cache_config: dict[str, Any] = {
        "CACHE_TYPE": "SupersetMetastoreCache",
        "CACHE_DEFAULT_TIMEOUT": 7776000,
        "REFRESH_TIMEOUT_ON_RETRIEVAL": True,
    }
    explore_form_data_cache_config: dict[str, Any] = {
        "CACHE_TYPE": "SupersetMetastoreCache",
        "CACHE_DEFAULT_TIMEOUT": 604800,
        "REFRESH_TIMEOUT_ON_RETRIEVAL": True,
    }
    store_cache_keys_in_metadata_db: bool = False
    enable_cors: bool = True
    cors_options: dict[str, Any] = {
        "origins": [
            "https://tile.openstreetmap.org",
            "https://tile.osm.ch",
        ],
    }

    time_grain_denylist: list[str] = []
    time_grain_addons: dict[str, str] = {}
    time_grain_addon_expressions: dict[str, dict[str, str]] = {}
    time_grain_join_column_producers: dict[str, Any] = {}
    default_module_ds_map: dict[str, list[str]] = {
        "superset.connectors.sqla.models": ["SqlaTable"],
    }
    additional_module_ds_map: dict[str, list[str]] = {}
    additional_middleware: list[Any] = []
    logging_configurator: Any | None = None
    log_format: str = "%(asctime)s:%(levelname)s:%(name)s:%(message)s"
    log_level_value: int = 20
    enable_time_rotate: bool = False
    time_rotate_log_level: int = 20
    log_filename: str = ""
    rollover: str = "midnight"
    log_interval: int = 1
    backup_count: int = 30
    query_logger: Any | None = None
    superset_meta_db_limit: int | None = 1000
    sqllab_schedule_warning_message: str | None = None
    sqllab_payload_max_mb: int | None = None
    sqllab_timeout: int = 30
    sqllab_validation_timeout: int = 10
    sqllab_async_time_limit_sec: int = 21600
    sqllab_query_cost_estimate_timeout: int = 10
    query_cost_formatters_by_engine: dict[str, Any] = {}
    sqllab_ctas_schema_name_func: Any | None = None
    celery_beat_scheduler_expires: int = 604800
    celery_config: Any | None = CeleryConfig
    # When True, commit the scoped sync session in task_postrun (mirrors
    # original SQLALCHEMY_COMMIT_ON_TEARDOWN config key).
    sqlalchemy_commit_on_teardown: bool = False
    celery_always_eager: bool = False
    default_http_headers: dict[str, Any] = {}
    override_http_headers: dict[str, Any] = {}
    http_headers: dict[str, Any] = {}
    default_db_id: int | None = None
    results_backend: Any | None = None
    results_backend_use_msgpack: bool = True
    csv_to_hive_upload_s3_bucket: str | None = None
    csv_to_hive_upload_directory: str = "EXTERNAL_HIVE_TABLES/"
    csv_to_hive_upload_directory_func: Any | None = None
    uploaded_csv_hive_namespace: str | None = None
    allowed_user_csv_schema_func: Any | None = None
    csv_default_na_names: list[str] = list(STR_NA_VALUES)
    jinja_context_addons: dict[str, Any] = {}
    custom_template_processors: dict[str, Any] = {}
    robot_permission_roles: list[str] = [
        "Public",
        "Gamma",
        "Alpha",
        "Admin",
        "sql_lab",
    ]

    flask_app_mutator: Any | None = None
    enable_chunk_encoding: bool = False
    silence_fab: bool = True
    fab_add_security_views: bool = True
    fab_add_security_api: bool = True
    fab_add_security_permission_view: bool = False
    fab_add_security_view_menu_view: bool = False
    fab_add_security_permission_views_view: bool = False
    # When True the password field on PUT /api/v1/me/ is validated against the
    # default complexity rules (≥2 uppercase, ≥1 special char, ≥2 digits,
    # ≥3 lowercase, ≥10 chars total) or a custom callable stored in
    # ``fab_password_complexity_validator``.  Corresponds to the
    # ``FAB_PASSWORD_COMPLEXITY_ENABLED`` / ``FAB_PASSWORD_COMPLEXITY_VALIDATOR``
    # config keys.
    fab_password_complexity_enabled: bool = False
    # Callable[[str], None] that raises on invalid passwords.  None → use the
    # built-in default_password_complexity() rules.
    fab_password_complexity_validator: Any | None = None
    troubleshooting_link: str = ""
    permission_instructions_link: str = ""
    blueprints: list[Any] = []
    tracking_url_transformer: Any | None = None
    db_poll_interval_seconds: dict[str, int] = {}
    presto_poll_interval: float = 1
    allowed_extra_authentications: dict[str, dict[str, Any]] = {}
    dashboard_template_id: int | None = None
    engine_context_manager: Any | None = None
    db_connection_mutator: Any | None = None
    db_sqla_uri_validator: Any | None = None
    disallowed_sql_functions: dict[str, set[str]] = {
        "postgresql": {
            "current_database",
            "current_schema",
            "current_user",
            "session_user",
            "current_setting",
            "version",
            "inet_client_addr",
            "inet_client_port",
            "inet_server_addr",
            "inet_server_port",
            "pg_read_file",
            "pg_ls_dir",
            "pg_read_binary_file",
            "database_to_xml",
            "database_to_xmlschema",
            "query_to_xml",
            "query_to_xmlschema",
            "table_to_xml",
            "table_to_xml_and_xmlschema",
            "query_to_xml_and_xmlschema",
            "table_to_xmlschema",
            "pg_sleep",
            "pg_terminate_backend",
        },
        "mysql": {
            "database",
            "schema",
            "current_user",
            "session_user",
            "system_user",
            "user",
            "version",
            "connection_id",
            "load_file",
            "sleep",
            "benchmark",
            "kill",
        },
        "sqlite": {
            "sqlite_version",
            "sqlite_source_id",
            "sqlite_offset",
            "sqlite_compileoption_used",
            "sqlite_compileoption_get",
            "load_extension",
        },
        "mssql": {
            "db_name",
            "suser_sname",
            "user_name",
            "host_name",
            "host_id",
            "suser_id",
            "system_user",
            "current_user",
            "original_login",
            "xp_cmdshell",
            "xp_regread",
            "xp_fileexist",
            "xp_dirtree",
            "serverproperty",
            "is_srvrolemember",
            "has_dbaccess",
            "fn_virtualfilestats",
            "fn_servershareddrives",
        },
        "clickhouse": {
            "currentUser",
            "currentDatabase",
            "hostName",
            "currentRoles",
            "version",
            "buildID",
            "url",
            "filesystemPath",
            "getOSInformation",
            "getMacro",
            "getSetting",
        },
    }

    sql_query_mutator: Any | None = None
    mutate_after_split: bool = False
    mutate_alert_query: bool = False
    email_header_mutator: Any | None = None
    exclude_users_from_lists: list[str] | None = None
    dbs_available_denylist: dict[str, set[str]] = {}
    machine_auth_provider_class: str = "superset.utils.machine_auth.MachineAuthProvider"
    alert_reports_cron_window_size: int = 59
    alert_reports_working_time_out_kill: bool = True
    alert_reports_executors: list[Any] = [ExecutorType.OWNER]
    alert_reports_working_time_out_lag: int = 10
    alert_reports_working_soft_time_out_lag: int = 1
    alert_reports_query_execution_max_tries: int = 1
    alert_reports_min_custom_screenshot_width: int = 600
    alert_reports_max_custom_screenshot_width: int = 2400
    alert_minimum_interval: int = 0
    report_minimum_interval: int = 0
    slack_proxy: str | None = None
    slack_cache_timeout: int = 86400
    slack_api_rate_limit_retry_count: int = 2
    webdriver_type: str = "firefox"
    webdriver_window: dict[str, Any] = {
        "dashboard": (1600, 2000),
        "slice": (3000, 1200),
        "pixel_density": 1,
    }
    webdriver_auth_func: Any | None = None
    webdriver_configuration: dict[str, Any] = {
        "options": {"capabilities": {}, "preferences": {}, "binary_location": ""},
        "service": {
            "log_output": "/dev/null",
            "service_args": [],
            "port": 0,
            "env": {},
        },
    }
    webdriver_option_args: list[str] = ["--headless"]
    webdriver_baseurl: str = "http://0.0.0.0:8080/"  # noqa: S104
    webdriver_baseurl_user_friendly: str = "http://0.0.0.0:8080/"  # noqa: S104
    email_page_render_wait: int = 30
    preferred_databases: list[str] = [
        "PostgreSQL",
        "Presto",
        "MySQL",
        "SQLite",
    ]
    test_database_connection_timeout: int = 30
    database_oauth2_clients: dict[str, dict[str, Any]] = {}
    database_oauth2_jwt_algorithm: str = "HS256"
    database_oauth2_timeout: int = 30
    # Optional explicit redirect URI override.  When unset, the default
    # is ``/api/v1/database/oauth2/`` (relative — engines requiring an
    # absolute URI must configure this).
    database_oauth2_redirect_uri: str = ""
    content_security_policy_warning: bool = True
    talisman_enabled: bool = True
    talisman_config: dict[str, Any] = {
        "content_security_policy": {
            "base-uri": ["'self'"],
            "default-src": ["'self'"],
            "img-src": [
                "'self'",
                "blob:",
                "data:",
                "https://apachesuperset.gateway.scarf.sh",
                "https://static.scarf.sh/",
                "ows.terrestris.de",
                "https://cdn.document360.io",
            ],
            "worker-src": ["'self'", "blob:"],
            "connect-src": [
                "'self'",
                "https://api.mapbox.com",
                "https://events.mapbox.com",
                "https://tile.openstreetmap.org",
                "https://tile.osm.ch",
            ],
            "object-src": "'none'",
            "style-src": ["'self'", "'unsafe-inline'"],
            "script-src": ["'self'", "'strict-dynamic'"],
        },
        "content_security_policy_nonce_in": ["script-src"],
        "force_https": False,
        "session_cookie_secure": False,
    }
    talisman_dev_config: dict[str, Any] = {
        "content_security_policy": {
            "base-uri": ["'self'"],
            "default-src": ["'self'"],
            "img-src": [
                "'self'",
                "blob:",
                "data:",
                "https://apachesuperset.gateway.scarf.sh",
                "https://static.scarf.sh/",
                "https://cdn.brandfolder.io",
                "ows.terrestris.de",
                "https://cdn.document360.io",
            ],
            "worker-src": ["'self'", "blob:"],
            "connect-src": [
                "'self'",
                "https://api.mapbox.com",
                "https://events.mapbox.com",
                "https://tile.openstreetmap.org",
                "https://tile.osm.ch",
            ],
            "object-src": "'none'",
            "style-src": ["'self'", "'unsafe-inline'"],
            "script-src": ["'self'", "'unsafe-inline'", "'unsafe-eval'"],
        },
        "content_security_policy_nonce_in": ["script-src"],
        "force_https": False,
        "session_cookie_secure": False,
    }

    session_server_side: bool = False
    send_file_max_age_default: int = 31536000
    prevent_unsafe_db_connections: bool = True
    dataset_import_allowed_data_urls: list[str] = [".*"]
    ssl_cert_path: str | None = None
    sqla_table_mutator: Any | None = None
    global_async_query_manager_class: str = (
        "superset.async_events.async_query_manager.AsyncQueryManager"
    )
    global_async_queries_redis_stream_prefix: str = "async-events-"
    global_async_queries_redis_stream_limit: int = 1000
    global_async_queries_redis_stream_limit_firehose: int = 1000000
    global_async_queries_register_request_handlers: bool = True
    global_async_queries_jwt_cookie_name: str = "async-token"
    global_async_queries_jwt_cookie_secure: bool = False
    global_async_queries_jwt_cookie_samesite: str | None = None
    global_async_queries_jwt_cookie_domain: str | None = None
    global_async_queries_jwt_secret: str = "test-secret-change-me"  # noqa: S105
    global_async_queries_cache_backend: dict[str, Any] = {
        "CACHE_TYPE": "RedisCache",
        "CACHE_REDIS_HOST": "localhost",
        "CACHE_REDIS_PORT": 6379,
        "CACHE_REDIS_USER": "",
        "CACHE_REDIS_PASSWORD": "",
        "CACHE_REDIS_DB": 0,
        "CACHE_DEFAULT_TIMEOUT": 300,
        "CACHE_REDIS_SENTINELS": [("localhost", 26379)],
        "CACHE_REDIS_SENTINEL_MASTER": "mymaster",
        "CACHE_REDIS_SENTINEL_PASSWORD": None,
        "CACHE_REDIS_SSL": False,
        "CACHE_REDIS_SSL_CERTFILE": None,
        "CACHE_REDIS_SSL_KEYFILE": None,
        "CACHE_REDIS_SSL_CERT_REQS": "required",
        "CACHE_REDIS_SSL_CA_CERTS": None,
    }

    guest_token_jwt_audience: Any | None = None
    dataset_health_check: Any | None = None
    zipped_file_max_size: int = 104857600
    zip_file_max_compress_ratio: float = 200.0
    extra_related_query_filters: dict[str, Any] = {}
    extra_dynamic_query_filters: dict[str, Any] = {}
    catalogs_simplified_migration: bool = False
    user_agent_func: Any | None = None

    @field_validator("log_level", mode="before")
    @classmethod
    def coerce_log_level(cls, v: Any) -> str:
        """Accept both ``int`` (e.g. ``logging.INFO = 20``) and ``str``.

        The original Superset config ships ``LOG_LEVEL = logging.INFO`` (an int).
        Pydantic-settings maps that to ``log_level`` via ``_SUPERSET_TO_LITESET``.
        This validator converts int (or a numeric string like "20") to the
        canonical level name so ``configure_logging`` can call
        ``settings.log_level.upper()`` safely.
        """
        import logging as _std_logging

        if isinstance(v, str):
            try:
                v = int(v)
            except ValueError:
                return v.upper()

        if isinstance(v, int):
            name = _std_logging.getLevelName(v)
            # getLevelName returns "Level N" for unknown ints; fall back to WARNING
            if name.startswith("Level "):
                return "WARNING"
            return name
        return str(v).upper()

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

    @field_validator(
        "celery_beat_scheduler_expires",
        "test_database_connection_timeout",
        "database_oauth2_timeout",
        mode="before",
    )
    @classmethod
    def coerce_timedelta_to_int(cls, v: Any) -> int:
        """Accept timedelta (original Superset format) or int (seconds).

        Upstream config.py defaults these to timedelta objects
        (e.g. ``timedelta(weeks=1)``, ``timedelta(seconds=30)``).
        Liteset stores them as ``int`` (total seconds).  This validator
        ensures that user superset_config.py files using the upstream
        timedelta type don't fail pydantic validation.
        """
        if hasattr(v, "total_seconds"):
            return int(v.total_seconds())
        return int(v)

    @model_validator(mode="after")
    def _resolve_version_info(self) -> SupersetSettings:
        """Populate version_string / version_sha from static/version_info.json.

        Only fills empty values so an explicit config override still wins.
        The file is generated on install (``{"GIT_SHA": ..., "version": ...}``).
        """
        if self.version_string and self.version_sha:
            return self
        import json as _json
        import os as _os

        path = _os.path.join(_os.path.dirname(__file__), "static", "version_info.json")
        try:
            with open(path) as f:
                data = _json.load(f)
        except Exception:  # noqa: BLE001 — file absent in some test contexts
            return self
        if not self.version_string:
            self.version_string = data.get("version") or ""
        if not self.version_sha:
            # Truncate to VERSION_SHA_LENGTH.
            self.version_sha = (data.get("GIT_SHA") or "")[: self.version_sha_length]
        return self

    @model_validator(mode="after")
    def _merge_feature_flags(self) -> SupersetSettings:
        """Merge feature flags: defaults <- SUPERSET_FEATURE_* env <- user config.

        Precedence (later wins):
        1. Start with _DEFAULT_FEATURE_FLAGS
        2. Merge SUPERSET_FEATURE_* env vars
        3. Merge user FEATURE_FLAGS last (``superset_config.py`` wins over env;
           feature_flag_manager does ``.update(FEATURE_FLAGS)`` last)
        """
        import re

        merged = _DEFAULT_FEATURE_FLAGS.copy()
        for k, v in os.environ.items():
            if re.match(r"^SUPERSET_FEATURE_\w+", k):
                flag_name = k[len("SUPERSET_FEATURE_") :]
                # parse_boolean_string semantics (incl. "t").
                merged[flag_name] = v.lower() in (
                    "y",
                    "yes",
                    "true",
                    "t",
                    "on",
                    "1",
                )
        merged.update(self.feature_flags)
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
        # Precedence (highest first):
        #   init kwargs > LITESET_* env > .env > superset_config.py
        #   > legacy unprefixed env (SUPERSET_SECRET_KEY, …) > file secrets.
        # superset_config.py sits above the legacy env source to match the
        # original behaviour (config file overrides env-seeded defaults).
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            SupersetConfigSettingsSource(settings_cls),
            LegacyEnvSettingsSource(settings_cls),
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
