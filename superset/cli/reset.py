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
"""Implements the ``factory-reset`` CLI command: wipes datasets, dashboards,
charts, the key-value store, logs, fav-stars and all non-Admin /
non-system users and roles, then writes a ``Factory Reset`` audit row.

Gated by the ``ENABLE_FACTORY_RESET_COMMAND`` feature flag (verbatim
from upstream).  The command is defensive — it requires both an
admin's username and password, refuses on a default ``--silent``
prompt, and returns a non-zero exit code on any error.
"""

from __future__ import annotations

import sys

import anyio
import click


@click.command()
@click.option("--username", prompt="Admin Username", help="Admin Username")
@click.option(
    "--silent",
    is_flag=True,
    prompt=(
        "Are you sure you want to reset Superset? "
        "This action cannot be undone. Continue?"
    ),
    help="Confirmation flag",
)
@click.option(
    "--exclude-users",
    default=None,
    help="Comma separated list of users to exclude from reset",
)
@click.option(
    "--exclude-roles",
    default=None,
    help="Comma separated list of roles to exclude from reset",
)
def factory_reset(
    username: str,
    silent: bool,
    exclude_users: str | None,
    exclude_roles: str | None,
) -> None:
    """Factory Reset Apache Superset."""
    anyio.run(
        _run_factory_reset,
        username,
        silent,
        exclude_users,
        exclude_roles,
    )


async def _run_factory_reset(
    username: str,
    silent: bool,
    exclude_users: str | None,
    exclude_roles: str | None,
) -> None:
    from superset.commands.security.reset import ResetSupersetCommand
    from superset.config import SupersetSettings
    from superset.db.session import create_db_engine, create_session_factory
    from superset.utils.feature_flags import feature_flag_manager

    settings = SupersetSettings()  # type: ignore[call-arg]

    # Hydrate the feature-flag manager from settings so the gate below
    # honours user-supplied flags from ``superset_config.py``.  The
    # manager is a process-wide singleton so this is idempotent.
    feature_flag_manager.init_from_config(settings.feature_flags)

    if not feature_flag_manager.is_feature_enabled("ENABLE_FACTORY_RESET_COMMAND"):
        click.secho(
            "Factory reset command is disabled. Enable "
            "ENABLE_FACTORY_RESET_COMMAND feature flag.",
            fg="red",
        )
        sys.exit(1)

    password = click.prompt("Admin Password", hide_input=True)

    engine = create_db_engine(settings.sqlalchemy_database_uri)
    session_factory = create_session_factory(engine)

    try:
        async with session_factory() as session:
            from sqlalchemy import select
            from sqlalchemy.orm import selectinload

            from superset.models.security import User
            from superset.security.dao import AsyncSecurityDAO
            from superset.security.manager import AsyncSecurityManager
            from superset.utils.password import check_password_hash

            security_manager = AsyncSecurityManager(
                AsyncSecurityDAO(session), settings=settings
            )
            stmt = (
                select(User)
                .options(selectinload(User.roles))
                .where(User.username == username)
            )
            user = (await session.execute(stmt)).scalars().first()

            if not user or not check_password_hash(str(user.password), password):
                click.secho("Invalid credentials", fg="red")
                sys.exit(1)

            roles = getattr(user, "roles", None) or []
            if not any(getattr(role, "name", "") == "Admin" for role in roles):
                click.secho("Permission Denied", fg="red")
                sys.exit(1)

            try:
                command = ResetSupersetCommand(
                    session=session,
                    security_manager=security_manager,
                    confirm=silent,
                    user=user,
                    exclude_users=exclude_users,
                    exclude_roles=exclude_roles,
                )
                await command.validate()
                await command.run()
                click.secho("Factory reset complete", fg="green")
            except Exception as ex:  # noqa: BLE001
                click.secho(f"Factory reset failed: {ex}", fg="red")
                sys.exit(1)
    finally:
        await engine.dispose()
