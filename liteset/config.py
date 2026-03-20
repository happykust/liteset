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

from pydantic import field_validator
from pydantic.fields import FieldInfo
from pydantic.functional_validators import AfterValidator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict
from typing_extensions import Annotated


def _validate_secret_key(v: str) -> str:
    if len(v) < 16:
        raise ValueError("secret_key must be at least 16 characters long")
    return v


SecretKeyStr = Annotated[str, AfterValidator(_validate_secret_key)]

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
}


_superset_config_cache: dict[str, dict[str, Any]] = {}


class SupersetConfigSettingsSource(PydanticBaseSettingsSource):
    """Read settings from superset_config.py as a Pydantic settings source.

    Priority: env vars > superset_config.py > defaults.
    Caches loaded values per config path to avoid re-executing the
    config file on every LitesetSettings() construction.
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
        spec.loader.exec_module(module)
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


class LitesetSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LITESET_",
        env_file=".env",
        extra="ignore",
    )

    secret_key: SecretKeyStr
    sqlalchemy_database_uri: str = "sqlite+aiosqlite:///superset.db"
    host: str = "0.0.0.0"
    port: int = 8088
    debug: bool = False
    static_assets_prefix: str = ""
    global_async_queries: bool = False
    cors_allow_origins: list[str] = []
    log_level: str = "INFO"
    production: bool = False
    cache_redis_url: str = ""
    cache_default_ttl: int = 300

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
    def from_superset_config(
        cls, config_path: str | None = None
    ) -> LitesetSettings:
        """Load settings from superset_config.py (deprecated: use SUPERSET_CONFIG_PATH env var)."""
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
        spec.loader.exec_module(module)

        kwargs: dict[str, Any] = {}
        for superset_key, liteset_key in _SUPERSET_TO_LITESET.items():
            value = getattr(module, superset_key, None)
            if value is not None:
                kwargs[liteset_key] = value

        if "secret_key" not in kwargs:
            raise ValueError("SECRET_KEY not found in superset config file")

        return cls(**kwargs)
