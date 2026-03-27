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
"""Liteset CLI — async Superset backend management commands."""

from __future__ import annotations

from pathlib import Path

import click


def normalize_token(token_name: str) -> str:
    return token_name.replace("_", "-")


@click.group(context_settings={"token_normalize_func": normalize_token})
@click.version_option(package_name="apache-superset")
def liteset_cli() -> None:
    """The Liteset CLI (async Superset backend)"""


@liteset_cli.command()
@click.option("--host", default="0.0.0.0", help="Bind host")  # noqa: S104
@click.option("--port", default=8088, type=int, help="Bind port")
@click.option("--reload", is_flag=True, help="Enable auto-reload")
@click.option("--workers", default=1, type=int, help="Number of workers")
def runserver(host: str, port: int, reload: bool, workers: int) -> None:
    """Run Liteset dev server via Uvicorn."""
    import uvicorn

    uvicorn.run(
        "liteset.app:create_app",
        factory=True,
        host=host,
        port=port,
        reload=reload,
        workers=workers,
    )


@liteset_cli.command()
def init() -> None:
    """Initialize Liteset application (roles, permissions).

    Creates default roles (Admin, Alpha, Gamma, Public, sql_lab)
    if they do not already exist in the database.
    """
    import anyio

    async def _init() -> None:
        from sqlalchemy import select

        from liteset.config import LitesetSettings
        from liteset.db.session import create_db_engine, create_session_factory
        from liteset.models.security import Role

        settings = LitesetSettings()  # type: ignore[call-arg]
        db_url = settings.sqlalchemy_database_uri

        click.echo(f"Connecting to database: {db_url.split('@')[-1] if '@' in db_url else db_url}")
        engine = create_db_engine(db_url)
        session_factory = create_session_factory(engine)

        default_roles = ["Admin", "Alpha", "Gamma", "Public", "sql_lab"]

        async with session_factory() as session:
            for role_name in default_roles:
                stmt = select(Role).where(Role.name == role_name)
                result = await session.execute(stmt)
                existing = result.scalars().one_or_none()
                if existing is None:
                    session.add(Role(name=role_name))
                    click.echo(f"  Created role: {role_name}")
                else:
                    click.echo(f"  Role already exists: {role_name}")
            await session.commit()

        await engine.dispose()
        click.echo("Initialization complete.")

    click.echo("Initializing Liteset...")
    anyio.run(_init)


@liteset_cli.command()
@click.option("--verbose", "-v", is_flag=True)
def version(verbose: bool) -> None:
    """Print Liteset version."""
    from liteset import __version__

    click.echo(f"Liteset {__version__}")
    if verbose:
        import litestar
        import sqlalchemy

        click.echo(f"  Litestar: {litestar.__version__}")
        click.echo(f"  SQLAlchemy: {sqlalchemy.__version__}")


@liteset_cli.group()
def db() -> None:
    """Database migration commands."""


@db.command()
@click.option("--revision", default="head", help="Revision target")
def upgrade(revision: str) -> None:
    """Run Alembic database migrations."""
    click.echo(f"Upgrading database to {revision}...")
    from alembic import command
    from alembic.config import Config

    alembic_ini = Path(__file__).resolve().parent.parent.parent / "alembic.ini"
    alembic_cfg = Config(str(alembic_ini))
    command.upgrade(alembic_cfg, revision)
    click.echo(f"Database upgraded to {revision}")
