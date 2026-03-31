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
"""Superset CLI — async Superset backend management commands."""

from __future__ import annotations

from pathlib import Path

import click


def normalize_token(token_name: str) -> str:
    return token_name.replace("_", "-")


@click.group(context_settings={"token_normalize_func": normalize_token})
@click.version_option(package_name="apache-superset")
def superset_cli() -> None:
    """The Superset CLI (async Superset backend)"""


@superset_cli.command()
@click.option("--host", default="0.0.0.0", help="Bind host")  # noqa: S104
@click.option("--port", default=8088, type=int, help="Bind port")
@click.option("--reload", is_flag=True, help="Enable auto-reload")
@click.option("--workers", default=1, type=int, help="Number of workers")
def runserver(host: str, port: int, reload: bool, workers: int) -> None:
    """Run Superset dev server via Uvicorn."""
    import uvicorn

    uvicorn.run(
        "superset.app:create_app",
        factory=True,
        host=host,
        port=port,
        reload=reload,
        workers=workers,
    )


@superset_cli.command()
def init() -> None:
    """Initialize Superset application (roles, permissions).

    Creates default roles (Admin, Alpha, Gamma, Public, sql_lab)
    if they do not already exist in the database.
    """
    import anyio

    async def _init() -> None:
        from superset.config import SupersetSettings
        from superset.db.session import (
            create_db_engine,
            create_session_factory,
        )
        from superset.security.sync_roles import (
            sync_role_definitions,
        )

        settings = SupersetSettings()  # type: ignore[call-arg]
        db_url = settings.sqlalchemy_database_uri

        safe_url = (
            db_url.split("@")[-1]
            if "@" in db_url
            else db_url
        )
        click.echo(
            f"Connecting to database: {safe_url}"
        )
        engine = create_db_engine(db_url)
        session_factory = create_session_factory(engine)

        click.echo("Syncing role definitions...")
        public_role_like = getattr(
            settings, "public_role_like", None
        )
        async with session_factory() as session:
            summary = await sync_role_definitions(
                session,
                public_role_like=public_role_like,
            )
            await session.commit()

        for role in summary.get(
            "roles_synced", []
        ):
            count = summary.get(
                f"{role.lower()}_permissions",
                summary.get(
                    f"{role}_permissions", "?"
                ),
            )
            click.echo(f"  {role}: {count} permissions")

        total = summary.get("total_pvms", "?")
        click.echo(
            f"  Total PVMs in database: {total}"
        )

        await engine.dispose()
        click.echo("Initialization complete.")

    click.echo("Initializing Superset...")
    anyio.run(_init)


@superset_cli.command()
@click.option("--verbose", "-v", is_flag=True)
def version(verbose: bool) -> None:
    """Print Superset version."""
    from superset import __version__

    click.echo(f"Superset {__version__}")
    if verbose:
        import litestar
        import sqlalchemy

        click.echo(f"  Litestar: {litestar.__version__}")
        click.echo(f"  SQLAlchemy: {sqlalchemy.__version__}")


# -----------------------------------------------------------
# Register sub-modules
# -----------------------------------------------------------

from superset.cli.db import db_group

superset_cli.add_command(db_group, "db")

# Users management (create-admin, create-user, list-users,
# reset-password, load-test-users, fab group)
try:
    from superset.cli.users import (
        create_admin,
        create_user,
        fab_group,
        list_users,
        load_test_users,
        reset_password,
    )

    superset_cli.add_command(create_admin)
    superset_cli.add_command(create_user)
    superset_cli.add_command(list_users)
    superset_cli.add_command(reset_password)
    superset_cli.add_command(load_test_users)
    superset_cli.add_command(fab_group, "fab")
except ImportError:
    pass

# Import / export
try:
    from superset.cli.importexport import (
        export_dashboards,
        export_datasources,
        import_dashboards,
        import_datasources,
        import_directory,
    )

    superset_cli.add_command(export_dashboards)
    superset_cli.add_command(export_datasources)
    superset_cli.add_command(import_dashboards)
    superset_cli.add_command(import_datasources)
    superset_cli.add_command(import_directory)
except ImportError:
    pass

# Examples
try:
    from superset.cli.examples import load_examples

    superset_cli.add_command(load_examples)
except ImportError:
    pass

# Update utilities
try:
    from superset.cli.update import (
        re_encrypt_secrets,
        set_database_uri,
        sync_tags,
    )

    superset_cli.add_command(set_database_uri)
    superset_cli.add_command(sync_tags)
    superset_cli.add_command(re_encrypt_secrets)
except ImportError:
    pass

# Thumbnails
try:
    from superset.cli.thumbnails import (
        compute_thumbnails,
    )

    superset_cli.add_command(compute_thumbnails)
except ImportError:
    pass
