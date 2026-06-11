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
"""Database migration commands -- Alembic wrappers.

Every sub-command creates a fresh :class:`alembic.config.Config` pointing
at ``superset/migrations/alembic.ini`` and overrides ``sqlalchemy.url``
with the **sync** database URI derived from
:class:`~superset.config.SupersetSettings`.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from alembic.config import Config


def _get_alembic_config(sql: bool = False) -> "Config":  # noqa: F821
    """Build an Alembic Config from the project's ``alembic.ini``."""
    import logging

    from alembic.config import Config

    from superset.config import SupersetSettings

    # Ensure alembic logger outputs to console
    logging.basicConfig(
        format="%(levelname)-5.5s [%(name)s] %(message)s",
        level=logging.INFO,
    )
    logging.getLogger("alembic").setLevel(logging.INFO)

    settings = SupersetSettings()  # type: ignore[call-arg]
    db_url = settings.sqlalchemy_database_uri

    alembic_ini = Path(__file__).resolve().parent.parent / "migrations" / "alembic.ini"
    cfg = Config(str(alembic_ini))

    # Convert async URI to sync for Alembic (psycopg2)
    sync_url = (
        db_url.replace("postgresql+asyncpg", "postgresql+psycopg2")
        .replace("sqlite+aiosqlite", "sqlite")
        .replace("mysql+asyncmy", "mysql+pymysql")
    )
    # configparser interpolation: a literal ``%`` (e.g. in a DB password)
    # must be escaped as ``%%`` or ``superset db upgrade`` dies with
    # InterpolationSyntaxError — 1:1 with upstream env.py's
    # ``DATABASE_URI.replace("%", "%%")``.
    cfg.set_main_option("sqlalchemy.url", sync_url.replace("%", "%%"))
    cfg.set_main_option(
        "script_location",
        str(alembic_ini.parent),
    )
    if sql:
        cfg.set_main_option("output_encoding", "utf-8")
    return cfg


@click.group("db")
def db_group() -> None:
    """Database migration commands (Alembic)."""


# ------------------------------------------------------------------
# upgrade
# ------------------------------------------------------------------


@db_group.command()
@click.option("--revision", default="head", help="Revision target")
@click.option("--sql", is_flag=True, help="Generate SQL script instead of applying")
@click.option("--tag", default=None, help="Arbitrary tag for the revision")
def upgrade(revision: str, sql: bool, tag: str | None) -> None:
    """Upgrade to a later version."""
    from alembic import command

    cfg = _get_alembic_config(sql=sql)
    command.upgrade(cfg, revision, sql=sql, tag=tag)


# ------------------------------------------------------------------
# downgrade
# ------------------------------------------------------------------


@db_group.command()
@click.argument("revision")
@click.option("--sql", is_flag=True, help="Generate SQL script instead of applying")
@click.option("--tag", default=None, help="Arbitrary tag for the revision")
def downgrade(revision: str, sql: bool, tag: str | None) -> None:
    """Revert to a previous version."""
    from alembic import command

    cfg = _get_alembic_config(sql=sql)
    click.echo(f"Downgrading database to {revision}...")
    command.downgrade(cfg, revision, sql=sql, tag=tag)
    if not sql:
        click.echo(f"Database downgraded to {revision}.")


# ------------------------------------------------------------------
# current
# ------------------------------------------------------------------


@db_group.command()
@click.option("--verbose", "-v", is_flag=True, help="Show full revision info")
def current(verbose: bool) -> None:
    """Display the current revision for the database."""
    from alembic import command

    cfg = _get_alembic_config()
    command.current(cfg, verbose=verbose)


# ------------------------------------------------------------------
# heads
# ------------------------------------------------------------------


@db_group.command()
@click.option("--verbose", "-v", is_flag=True, help="Show full revision info")
@click.option(
    "--resolve-dependencies",
    is_flag=True,
    help="Treat dependencies as down revisions",
)
def heads(verbose: bool, resolve_dependencies: bool) -> None:
    """Show current available heads."""
    from alembic import command

    cfg = _get_alembic_config()
    command.heads(cfg, verbose=verbose, resolve_dependencies=resolve_dependencies)


# ------------------------------------------------------------------
# history
# ------------------------------------------------------------------


@db_group.command()
@click.option("--rev-range", "-r", default=None, help="Revision range (e.g. rev1:rev2)")
@click.option("--verbose", "-v", is_flag=True, help="Show full revision info")
@click.option(
    "--indicate-current",
    "-i",
    is_flag=True,
    help="Indicate current revision",
)
def history(
    rev_range: str | None,
    verbose: bool,
    indicate_current: bool,
) -> None:
    """List changeset scripts in chronological order."""
    from alembic import command

    cfg = _get_alembic_config()
    command.history(
        cfg,
        rev_range=rev_range,
        verbose=verbose,
        indicate_current=indicate_current,
    )


# ------------------------------------------------------------------
# stamp
# ------------------------------------------------------------------


@db_group.command()
@click.argument("revision")
@click.option("--sql", is_flag=True, help="Generate SQL script instead of applying")
@click.option("--tag", default=None, help="Arbitrary tag for the revision")
@click.option(
    "--purge",
    is_flag=True,
    help="Delete all entries in alembic_version first",
)
def stamp(revision: str, sql: bool, tag: str | None, purge: bool) -> None:
    """Stamp the revision table with the given revision (don't run migrations)."""
    from alembic import command

    cfg = _get_alembic_config(sql=sql)
    command.stamp(cfg, revision, sql=sql, tag=tag, purge=purge)
    click.echo(f"Database stamped to {revision}.")


# ------------------------------------------------------------------
# migrate  (autogenerate revision)
# ------------------------------------------------------------------


@db_group.command()
@click.option("-m", "--message", default=None, help="Revision message")
@click.option("--head", default="head", help="Head revision to base new revision on")
@click.option("--splice", is_flag=True, help="Allow non-head revision as head")
@click.option("--branch-label", default=None, help="Branch label for the revision")
@click.option("--rev-id", default=None, help="Override revision ID")
def migrate(
    message: str | None,
    head: str,
    splice: bool,
    branch_label: str | None,
    rev_id: str | None,
) -> None:
    """Autogenerate a new migration (alias for ``revision --autogenerate``)."""
    from alembic import command

    cfg = _get_alembic_config()
    command.revision(
        cfg,
        message=message,
        autogenerate=True,
        head=head,
        splice=splice,
        branch_label=branch_label,
        rev_id=rev_id,
    )
    click.echo("New migration generated.")


# ------------------------------------------------------------------
# revision  (manual revision)
# ------------------------------------------------------------------


@db_group.command()
@click.option("-m", "--message", default=None, help="Revision message")
@click.option("--head", default="head", help="Head revision to base new revision on")
@click.option("--splice", is_flag=True, help="Allow non-head revision as head")
@click.option("--branch-label", default=None, help="Branch label for the revision")
@click.option("--rev-id", default=None, help="Override revision ID")
def revision(
    message: str | None,
    head: str,
    splice: bool,
    branch_label: str | None,
    rev_id: str | None,
) -> None:
    """Create a new (empty) migration script."""
    from alembic import command

    cfg = _get_alembic_config()
    command.revision(
        cfg,
        message=message,
        autogenerate=False,
        head=head,
        splice=splice,
        branch_label=branch_label,
        rev_id=rev_id,
    )
    click.echo("New revision created.")


# ------------------------------------------------------------------
# branches
# ------------------------------------------------------------------


@db_group.command()
@click.option("--verbose", "-v", is_flag=True, help="Show full revision info")
def branches(verbose: bool) -> None:
    """Show current branch points."""
    from alembic import command

    cfg = _get_alembic_config()
    command.branches(cfg, verbose=verbose)


# ------------------------------------------------------------------
# show
# ------------------------------------------------------------------


@db_group.command()
@click.argument("revision")
def show(revision: str) -> None:
    """Show the detail of a revision."""
    from alembic import command

    cfg = _get_alembic_config()
    command.show(cfg, revision)


# ------------------------------------------------------------------
# merge
# ------------------------------------------------------------------


@db_group.command()
@click.argument("revisions", nargs=-1, required=True)
@click.option("-m", "--message", default=None, help="Merge revision message")
@click.option(
    "--branch-label",
    default=None,
    help="Branch label for the merge revision",
)
@click.option("--rev-id", default=None, help="Override revision ID")
def merge(
    revisions: tuple[str, ...],
    message: str | None,
    branch_label: str | None,
    rev_id: str | None,
) -> None:
    """Merge two revisions together, creating a new migration."""
    from alembic import command

    cfg = _get_alembic_config()
    command.merge(
        cfg,
        revisions=list(revisions),
        message=message,
        branch_label=branch_label,
        rev_id=rev_id,
    )
    click.echo("Merge revision created.")


# ------------------------------------------------------------------
# check
# ------------------------------------------------------------------


@db_group.command()
def check() -> None:
    """Check if there are any pending migrations (non-zero exit if true)."""
    import sys

    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    cfg = _get_alembic_config()
    script = ScriptDirectory.from_config(cfg)
    head_revs = set(script.get_heads())

    from sqlalchemy import create_engine

    url = cfg.get_main_option("sqlalchemy.url") or ""
    engine = create_engine(url)
    with engine.connect() as conn:
        context = MigrationContext.configure(conn)
        current_revs = set(context.get_current_heads())

    if current_revs == head_revs:
        click.echo("Database is up to date.")
    else:
        click.secho(
            f"Pending migrations detected.\n"
            f"  Current: {current_revs or '{none}'}\n"
            f"  Head:    {head_revs}",
            fg="yellow",
        )
        sys.exit(1)
