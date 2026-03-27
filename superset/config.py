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

from pydantic import field_validator, SecretStr
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
}


_superset_config_cache: dict[str, dict[str, Any]] = {}


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
    sqlalchemy_database_uri: str = "sqlite+aiosqlite:///superset.db"
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

    # Session cookie max age (seconds), applied to FlaskSessionDecoder
    session_max_age: int = 86400

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

    # Feature flags dict (mirrors Superset's FEATURE_FLAGS)
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
