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
from typing import Any

from alembic import context
from sqlalchemy import create_engine, pool

logger = logging.getLogger("alembic.env")

# Apply the logging configuration from alembic.ini, 1:1 with upstream
# ``if not ALEMBIC_SKIP_LOG_CONFIG: fileConfig(config.config_file_name)``. The
# settings read is guarded so migrations still run if config can't be resolved.
try:  # pragma: no cover — best-effort logging setup
    from logging.config import fileConfig

    from superset.config import SupersetSettings

    if (
        not SupersetSettings().alembic_skip_log_config  # type: ignore[call-arg]
        and context.config.config_file_name is not None
    ):
        fileConfig(context.config.config_file_name)
except Exception:  # noqa: BLE001, S110 — logging setup must never block migrations
    pass

_ASYNC_TO_SYNC_DRIVERS = {
    "postgresql+asyncpg://": "postgresql+psycopg2://",
    "mysql+asyncmy://": "mysql+pymysql://",
    "sqlite+aiosqlite://": "sqlite://",
}


def _get_sync_url() -> str:
    url = os.environ.get(
        "SUPERSET_SQLALCHEMY_DATABASE_URI",
        context.config.get_main_option("sqlalchemy.url", ""),
    )
    for async_prefix, sync_prefix in _ASYNC_TO_SYNC_DRIVERS.items():
        if url.startswith(async_prefix):
            url = url.replace(async_prefix, sync_prefix, 1)
            break
    return url


def _load_models_metadata() -> Any:
    """Load model metadata for autogenerate."""
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

    return Base.metadata


# target_metadata is used by autogenerate; None during upgrade/downgrade
target_metadata = None


def _get_target_metadata() -> Any:
    """Return model metadata for autogenerate, None for upgrade/downgrade."""
    global target_metadata
    if (
        target_metadata is None
        and context.config.cmd_opts
        and getattr(context.config.cmd_opts, "autogenerate", False)
    ):
        target_metadata = _load_models_metadata()
    return target_metadata


def _process_revision_directives(
    migration_context: Any, revision: Any, directives: list[Any]
) -> None:
    """Skip generating an empty migration on ``--autogenerate``.

    1:1 with upstream env.py — when the schema is unchanged, clear the
    directives so Alembic doesn't write an empty revision file.
    """
    if getattr(context.config.cmd_opts, "autogenerate", False):
        script = directives[0]
        if script.upgrade_ops.is_empty():
            directives[:] = []
            logger.info("No changes in schema detected.")


def run_migrations_offline() -> None:
    url = _get_sync_url()
    context.configure(
        url=url,
        target_metadata=_get_target_metadata(),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        process_revision_directives=_process_revision_directives,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = _get_sync_url()
    connectable = create_engine(url, poolclass=pool.NullPool)
    with connectable.connect() as connection:
        # 1:1 with upstream env.py: sqlite/mysql lack reliable transactional
        # DDL, so run each migration in its own transaction to avoid a failed
        # migration leaving a half-applied schema.
        kwargs: dict[str, object] = {}
        if connectable.name in ("sqlite", "mysql"):
            kwargs = {
                "transaction_per_migration": True,
                "transactional_ddl": True,
            }
        context.configure(
            connection=connection,
            target_metadata=_get_target_metadata(),
            process_revision_directives=_process_revision_directives,
            **kwargs,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
