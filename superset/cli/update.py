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
"""Update / maintenance CLI commands: set-database-uri, sync-tags,
re-encrypt-secrets."""

from __future__ import annotations

import sys
from typing import Optional

import click


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
    # Delegate to ``get_or_create_db`` so the URI goes through
    # ``Database.set_sqlalchemy_uri()`` — the inline password is stripped
    # into the *encrypted* ``password`` column and the stored
    # ``sqlalchemy_uri`` keeps only ``PASSWORD_MASK``.  The previous raw
    # ``UPDATE dbs SET sqlalchemy_uri = :uri`` wrote the plaintext
    # password straight into the metadata DB.
    import superset.utils.database as database_utils
    from superset.db.session import get_sync_session, remove_sync_session

    try:
        database_utils.get_or_create_db(database_name, uri, not skip_create)
        # The original runs under the ``@transaction()`` decorator which
        # commits on success; ``get_or_create_db`` itself only flushes.
        get_sync_session().commit()
    finally:
        remove_sync_session()


@click.command("sync-tags")
def sync_tags() -> None:
    """Rebuild special tags (owner, type, favorited by).

    For each object type (dashboard, chart, query, dataset) ensures that:
      - type tags exist and are linked to every object
      - owner tags exist for every user and are linked to objects they created
      - favorited_by tags exist and are linked to favorited objects
    """
    import anyio

    async def _sync() -> None:
        from sqlalchemy import text

        from superset.config import SupersetSettings
        from superset.db.engine import create_db_engine, create_session_factory

        settings = SupersetSettings()  # type: ignore[call-arg]
        engine = create_db_engine(settings.sqlalchemy_database_uri)
        session_factory = create_session_factory(engine)

        async with session_factory() as session:
            # Check if tag tables exist before syncing
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

            click.echo("  Adding type tags...")
            for type_name in ("chart", "dashboard", "query", "dataset"):
                await session.execute(
                    text(
                        "INSERT INTO tag (name, type) "
                        "SELECT :tag_name, 'type' "
                        "WHERE NOT EXISTS ("
                        "  SELECT 1 FROM tag WHERE name = :tag_name"
                        ")"
                    ),
                    {"tag_name": f"type:{type_name}"},
                )

            await session.execute(
                text(
                    "INSERT INTO tagged_object (tag_id, object_id, object_type) "
                    "SELECT t.id, s.id, 'chart' "
                    "FROM slices s "
                    "JOIN tag t ON t.name = 'type:chart' "
                    "LEFT JOIN tagged_object tobj "
                    "  ON tobj.tag_id = t.id "
                    "  AND tobj.object_id = s.id "
                    "  AND tobj.object_type = 'chart' "
                    "WHERE tobj.tag_id IS NULL"
                )
            )
            await session.execute(
                text(
                    "INSERT INTO tagged_object (tag_id, object_id, object_type) "
                    "SELECT t.id, d.id, 'dashboard' "
                    "FROM dashboards d "
                    "JOIN tag t ON t.name = 'type:dashboard' "
                    "LEFT JOIN tagged_object tobj "
                    "  ON tobj.tag_id = t.id "
                    "  AND tobj.object_id = d.id "
                    "  AND tobj.object_type = 'dashboard' "
                    "WHERE tobj.tag_id IS NULL"
                )
            )
            await session.execute(
                text(
                    "INSERT INTO tagged_object (tag_id, object_id, object_type) "
                    "SELECT t.id, sq.id, 'query' "
                    "FROM saved_query sq "
                    "JOIN tag t ON t.name = 'type:query' "
                    "LEFT JOIN tagged_object tobj "
                    "  ON tobj.tag_id = t.id "
                    "  AND tobj.object_id = sq.id "
                    "  AND tobj.object_type = 'query' "
                    "WHERE tobj.tag_id IS NULL"
                )
            )
            await session.execute(
                text(
                    "INSERT INTO tagged_object (tag_id, object_id, object_type) "
                    "SELECT t.id, tbl.id, 'dataset' "
                    "FROM tables tbl "
                    "JOIN tag t ON t.name = 'type:dataset' "
                    "LEFT JOIN tagged_object tobj "
                    "  ON tobj.tag_id = t.id "
                    "  AND tobj.object_id = tbl.id "
                    "  AND tobj.object_type = 'dataset' "
                    "WHERE tobj.tag_id IS NULL"
                )
            )

            click.echo("  Adding owner tags...")
            user_rows = await session.execute(text("SELECT id FROM ab_user"))
            for (user_id,) in user_rows:
                await session.execute(
                    text(
                        "INSERT INTO tag (name, type) "
                        "SELECT :tag_name, 'owner' "
                        "WHERE NOT EXISTS ("
                        "  SELECT 1 FROM tag WHERE name = :tag_name"
                        ")"
                    ),
                    {"tag_name": f"owner:{user_id}"},
                )

            await session.execute(
                text(
                    "INSERT INTO tagged_object (tag_id, object_id, object_type) "
                    "SELECT t.id, s.id, 'chart' "
                    "FROM slices s "
                    "JOIN tag t ON t.name = 'owner:' || CAST(s.created_by_fk AS TEXT) "
                    "LEFT JOIN tagged_object tobj "
                    "  ON tobj.tag_id = t.id "
                    "  AND tobj.object_id = s.id "
                    "  AND tobj.object_type = 'chart' "
                    "WHERE tobj.tag_id IS NULL "
                    "  AND s.created_by_fk IS NOT NULL"
                )
            )
            await session.execute(
                text(
                    "INSERT INTO tagged_object (tag_id, object_id, object_type) "
                    "SELECT t.id, d.id, 'dashboard' "
                    "FROM dashboards d "
                    "JOIN tag t ON t.name = 'owner:' || CAST(d.created_by_fk AS TEXT) "
                    "LEFT JOIN tagged_object tobj "
                    "  ON tobj.tag_id = t.id "
                    "  AND tobj.object_id = d.id "
                    "  AND tobj.object_type = 'dashboard' "
                    "WHERE tobj.tag_id IS NULL "
                    "  AND d.created_by_fk IS NOT NULL"
                )
            )
            await session.execute(
                text(
                    "INSERT INTO tagged_object (tag_id, object_id, object_type) "
                    "SELECT t.id, sq.id, 'query' "
                    "FROM saved_query sq "
                    "JOIN tag t ON t.name = 'owner:' || CAST(sq.created_by_fk AS TEXT) "
                    "LEFT JOIN tagged_object tobj "
                    "  ON tobj.tag_id = t.id "
                    "  AND tobj.object_id = sq.id "
                    "  AND tobj.object_type = 'query' "
                    "WHERE tobj.tag_id IS NULL "
                    "  AND sq.created_by_fk IS NOT NULL"
                )
            )
            await session.execute(
                text(
                    "INSERT INTO tagged_object (tag_id, object_id, object_type) "
                    "SELECT t.id, tbl.id, 'dataset' "
                    "FROM tables tbl "
                    "JOIN tag t ON t.name = "
                    "'owner:' || CAST(tbl.created_by_fk AS TEXT) "
                    "LEFT JOIN tagged_object tobj "
                    "  ON tobj.tag_id = t.id "
                    "  AND tobj.object_id = tbl.id "
                    "  AND tobj.object_type = 'dataset' "
                    "WHERE tobj.tag_id IS NULL "
                    "  AND tbl.created_by_fk IS NOT NULL"
                )
            )

            click.echo("  Adding favorited_by tags...")
            for (user_id,) in await session.execute(text("SELECT id FROM ab_user")):
                await session.execute(
                    text(
                        "INSERT INTO tag (name, type) "
                        "SELECT :tag_name, 'favorited_by' "
                        "WHERE NOT EXISTS ("
                        "  SELECT 1 FROM tag WHERE name = :tag_name"
                        ")"
                    ),
                    {"tag_name": f"favorited_by:{user_id}"},
                )

            await session.execute(
                text(
                    "INSERT INTO tagged_object (tag_id, object_id, object_type) "
                    "SELECT t.id, f.obj_id, LOWER(f.class_name) "
                    "FROM favstar f "
                    "JOIN tag t "
                    "  ON t.name = 'favorited_by:' || CAST(f.user_id AS TEXT) "
                    "LEFT JOIN tagged_object tobj "
                    "  ON tobj.tag_id = t.id "
                    "  AND tobj.object_id = f.obj_id "
                    "  AND tobj.object_type = LOWER(f.class_name) "
                    "WHERE tobj.tag_id IS NULL"
                )
            )

            await session.commit()
            click.secho("Tags synced successfully.", fg="green")

        await engine.dispose()

    anyio.run(_sync)


@click.command("re-encrypt-secrets")
@click.option(
    "--previous-secret-key",
    "-a",
    required=False,
    default=None,
    help="Previous secret key (or set PREVIOUS_SECRET_KEY env var)",
)
def re_encrypt_secrets(previous_secret_key: Optional[str] = None) -> None:
    """Re-encrypt secrets with a new SECRET_KEY.

    Walks every metadata table and re-encrypts every ``EncryptedType``
    column under the new ``SECRET_KEY`` configured in the active
    :class:`superset.config.SupersetSettings`.  The previous key must be
    supplied via ``--previous-secret-key`` or the
    ``PREVIOUS_SECRET_KEY`` environment variable.

    The migrator uses a synchronous engine (mirroring
    ``utils.rls._metadata_sync_engine``) because re-encryption is a
    one-shot operation that runs outside any Litestar request context.
    """
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

    # Importing :mod:`superset.models` (transitively, via the encryption
    # utilities) registers every model on ``Base.metadata`` so the
    # migrator's ``discover_encrypted_fields`` walk sees them.
    import superset.models  # noqa: F401  (side-effect: model registration)
    from superset.utils.encrypt import SecretsMigrator

    click.echo("Re-encrypting metadata secrets with the new SECRET_KEY...")
    migrator = SecretsMigrator(previous_secret_key=previous_secret_key)
    try:
        migrator.run()
    except ValueError as exc:
        # A wrong previous key surfaces as a decryption ValueError; give
        # the operator a hint instead of a raw traceback.
        click.secho(
            f"An error occurred, "
            f"probably an invalid previous secret key was provided. Error:[{exc}]",
            err=True,
        )
        sys.exit(1)
    click.secho("All encrypted columns have been re-encrypted.", fg="green")
