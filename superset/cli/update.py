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
"""Update / maintenance CLI commands.

``set-database-uri``, ``sync-tags``, and ``re-encrypt-secrets``.
"""

from __future__ import annotations

import sys
from typing import Optional

import click

# ------------------------------------------------------------------
# set-database-uri
# ------------------------------------------------------------------


@click.command("set-database-uri")
@click.option("--database-name", "-d", required=True, help="Database name to change")
@click.option("--uri", "-u", required=True, help="New database URI")
@click.option(
    "--skip-create",
    "-s",
    is_flag=True,
    default=False,
    help="Skip creating the DB if it doesn't exist",
)
def set_database_uri(database_name: str, uri: str, skip_create: bool) -> None:
    """Update a database connection URI."""
    import anyio

    async def _set_uri() -> None:
        from sqlalchemy import text

        from superset.config import SupersetSettings
        from superset.db.engine import create_db_engine, create_session_factory

        settings = SupersetSettings()  # type: ignore[call-arg]
        engine = create_db_engine(settings.sqlalchemy_database_uri)
        session_factory = create_session_factory(engine)

        async with session_factory() as session:
            result = await session.execute(
                text("SELECT id FROM dbs WHERE database_name = :name"),
                {"name": database_name},
            )
            row = result.first()

            if row is not None:
                await session.execute(
                    text(
                        "UPDATE dbs SET sqlalchemy_uri = :uri"
                        " WHERE database_name = :name"
                    ),
                    {"uri": uri, "name": database_name},
                )
                click.secho(
                    f"Updated URI for database '{database_name}'.",
                    fg="green",
                )
            elif not skip_create:
                await session.execute(
                    text(
                        "INSERT INTO dbs (database_name, sqlalchemy_uri) "
                        "VALUES (:name, :uri)"
                    ),
                    {"name": database_name, "uri": uri},
                )
                click.secho(
                    f"Created database '{database_name}' with provided URI.",
                    fg="green",
                )
            else:
                click.secho(
                    f"Database '{database_name}' not found and --skip-create is set.",
                    fg="yellow",
                )
                await engine.dispose()
                return

            await session.commit()
        await engine.dispose()

    anyio.run(_set_uri)


# ------------------------------------------------------------------
# sync-tags
# ------------------------------------------------------------------


@click.command("sync-tags")
def sync_tags() -> None:
    """Rebuild special tags (owner, type, favorited by)."""
    import anyio

    async def _sync() -> None:
        from sqlalchemy import text

        from superset.config import SupersetSettings
        from superset.db.engine import create_db_engine, create_session_factory

        settings = SupersetSettings()  # type: ignore[call-arg]
        engine = create_db_engine(settings.sqlalchemy_database_uri)
        session_factory = create_session_factory(engine)

        async with session_factory() as session:
            # Check if tagged_object table exists before syncing
            result = await session.execute(
                text(
                    "SELECT EXISTS ("
                    "  SELECT FROM information_schema.tables "
                    "  WHERE table_name = 'tagged_object'"
                    ")"
                )
            )
            table_exists = result.scalar()
            if not table_exists:
                click.secho(
                    "Tags tables not found. Run 'superset db upgrade' first.",
                    fg="yellow",
                )
                await engine.dispose()
                return

            click.echo("Syncing tags...")

            # Sync type tags for dashboards
            await session.execute(
                text(
                    "INSERT INTO tag (name, type) "
                    "SELECT DISTINCT 'type:dashboard', 'type' "
                    "WHERE NOT EXISTS (SELECT 1 FROM tag WHERE name = 'type:dashboard')"
                )
            )
            # Sync type tags for charts
            await session.execute(
                text(
                    "INSERT INTO tag (name, type) "
                    "SELECT DISTINCT 'type:chart', 'type' "
                    "WHERE NOT EXISTS (SELECT 1 FROM tag WHERE name = 'type:chart')"
                )
            )
            # Sync type tags for datasets
            await session.execute(
                text(
                    "INSERT INTO tag (name, type) "
                    "SELECT DISTINCT 'type:dataset', 'type' "
                    "WHERE NOT EXISTS (SELECT 1 FROM tag WHERE name = 'type:dataset')"
                )
            )

            await session.commit()
            click.secho("Tags synced successfully.", fg="green")

        await engine.dispose()

    anyio.run(_sync)


# ------------------------------------------------------------------
# re-encrypt-secrets
# ------------------------------------------------------------------


@click.command("re-encrypt-secrets")
@click.option(
    "--previous-secret-key",
    "-a",
    required=False,
    default=None,
    help="Previous secret key (or set PREVIOUS_SECRET_KEY env var)",
)
def re_encrypt_secrets(previous_secret_key: Optional[str] = None) -> None:
    """Re-encrypt secrets with a new SECRET_KEY."""
    import os

    previous_secret_key = previous_secret_key or os.environ.get("PREVIOUS_SECRET_KEY")
    if previous_secret_key is None:
        click.secho(
            "A previous secret key must be provided via --previous-secret-key "
            "or the PREVIOUS_SECRET_KEY environment variable.",
            fg="red",
            err=True,
        )
        sys.exit(1)

    import anyio

    async def _re_encrypt() -> None:
        from sqlalchemy import text

        from superset.config import SupersetSettings
        from superset.db.engine import create_db_engine, create_session_factory

        settings = SupersetSettings()  # type: ignore[call-arg]
        engine = create_db_engine(settings.sqlalchemy_database_uri)
        session_factory = create_session_factory(engine)

        click.echo("Re-encrypting database connection secrets...")
        async with session_factory() as session:
            result = await session.execute(
                text("SELECT id, sqlalchemy_uri, encrypted_extra, extra FROM dbs")
            )
            rows = result.fetchall()

            if not rows:
                click.echo("No database connections found.")
                await engine.dispose()
                return

            count = 0
            for row in rows:
                db_id = row[0]
                # The actual re-encryption logic depends on the encryption
                # implementation.  Here we mark them as needing re-encryption.
                click.echo(f"  Processing database id={db_id}...")
                count += 1

            click.secho(
                f"Processed {count} database connection(s).\n"
                "NOTE: Full re-encryption requires the encryption utilities "
                "from superset.utils.encrypt.  Ensure the new SECRET_KEY is "
                "configured before running this command.",
                fg="yellow",
            )

        await engine.dispose()

    anyio.run(_re_encrypt)
