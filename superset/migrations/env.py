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
"""Alembic environment configuration for Superset.

Uses psycopg2 (sync driver) for migrations. Runtime uses asyncpg.
Dual-driver strategy:
- Runtime:    postgresql+asyncpg://
- Migrations: postgresql+psycopg2://
"""
from __future__ import annotations

import logging
import os

from alembic import context
from sqlalchemy import create_engine, pool

logger = logging.getLogger("alembic.env")

# Import superset model metadata for autogenerate support
import superset.models.annotations  # noqa: F401
import superset.models.cache  # noqa: F401
import superset.models.connectors  # noqa: F401
import superset.models.core  # noqa: F401
import superset.models.dashboard  # noqa: F401
import superset.models.dynamic_plugins  # noqa: F401
import superset.models.embedded_dashboard  # noqa: F401
import superset.models.key_value  # noqa: F401
import superset.models.reports  # noqa: F401
import superset.models.security  # noqa: F401
import superset.models.slice  # noqa: F401
import superset.models.sql_lab  # noqa: F401
import superset.models.tags  # noqa: F401
import superset.models.user  # noqa: F401
from superset.models.helpers import Base

target_metadata = Base.metadata

_ASYNC_TO_SYNC_DRIVERS = {
    "postgresql+asyncpg://": "postgresql+psycopg2://",
    "mysql+asyncmy://": "mysql+pymysql://",
    "sqlite+aiosqlite://": "sqlite://",
}


def _get_sync_url() -> str:
    url = os.environ.get(
        "LITESET_SQLALCHEMY_DATABASE_URI",
        context.config.get_main_option("sqlalchemy.url", ""),
    )
    for async_prefix, sync_prefix in _ASYNC_TO_SYNC_DRIVERS.items():
        if url.startswith(async_prefix):
            url = url.replace(async_prefix, sync_prefix, 1)
            break
    return url


def run_migrations_offline() -> None:
    url = _get_sync_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = _get_sync_url()
    connectable = create_engine(url, poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
