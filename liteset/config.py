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
from pydantic.functional_validators import AfterValidator
from pydantic_settings import BaseSettings, SettingsConfigDict
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

    @field_validator("sqlalchemy_database_uri")
    @classmethod
    def convert_to_async_driver(cls, v: str) -> str:
        for sync_prefix, async_prefix in _SYNC_TO_ASYNC_DRIVERS.items():
            if v.startswith(sync_prefix):
                return v.replace(sync_prefix, async_prefix, 1)
        return v

    @classmethod
    def from_superset_config(
        cls, config_path: str | None = None
    ) -> LitesetSettings:
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

        field_map = {
            "SECRET_KEY": "secret_key",
            "SQLALCHEMY_DATABASE_URI": "sqlalchemy_database_uri",
            "CORS_ALLOW_ORIGINS": "cors_allow_origins",
            "GLOBAL_ASYNC_QUERIES": "global_async_queries",
            "STATIC_ASSETS_PREFIX": "static_assets_prefix",
        }
        kwargs: dict[str, Any] = {}
        for superset_key, liteset_key in field_map.items():
            value = getattr(module, superset_key, None)
            if value is not None:
                kwargs[liteset_key] = value

        if "secret_key" not in kwargs:
            raise ValueError("SECRET_KEY not found in superset config file")

        return cls(**kwargs)
