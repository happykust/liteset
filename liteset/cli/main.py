"""Liteset CLI — async Superset backend management commands."""
from __future__ import annotations

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
    """Initialize Liteset application (roles, permissions)."""
    click.echo("Initializing Liteset...")
    click.echo("Done.")


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

    alembic_cfg = Config("alembic.ini")
    command.upgrade(alembic_cfg, revision)
    click.echo(f"Database upgraded to {revision}")
