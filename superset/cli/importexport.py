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
"""Import / Export CLI commands.

Async port of ``superset_old/cli/importexport.py`` (V1 bundle commands) plus
``superset_old/examples/utils.load_configs_from_directory`` (import-directory).

Supported commands:
* ``export-dashboards``    — V1 ZIP bundle
* ``import-dashboards``    — V1 ZIP bundle
* ``export-datasources``   — V1 ZIP bundle
* ``import-datasources``   — V1 ZIP bundle
* ``import-directory``     — walk directory of YAML configs (example loader)
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional
from zipfile import is_zipfile, ZipFile

import click

logger = logging.getLogger(__name__)


def _make_session() -> tuple[object, object]:
    """Create an async session-factory + engine from current settings."""
    from superset.config import SupersetSettings
    from superset.db.engine import create_db_engine, create_session_factory

    settings = SupersetSettings()  # type: ignore[call-arg]
    engine = create_db_engine(settings.sqlalchemy_database_uri)
    return create_session_factory(engine), engine


# ------------------------------------------------------------------
# export-dashboards
# ------------------------------------------------------------------


@click.command("export-dashboards")
@click.option(
    "--dashboard-file",
    "-f",
    default=None,
    help="Specify the file to export to",
)
def export_dashboards(dashboard_file: Optional[str] = None) -> None:
    """Export dashboards to ZIP file"""
    # 1:1 port of superset_old/cli/importexport.py export_dashboards.
    # The original uses ExportDashboardsCommand(dashboard_ids).run() which
    # yields (file_name, file_content_callable) pairs; the port's async
    # ExportDashboardsCommand.execute() returns io.BytesIO directly.
    import anyio

    async def _export() -> None:
        from sqlalchemy import select

        from superset.commands.dashboard.export import ExportDashboardsCommand
        from superset.db.daos.dashboard import AsyncDashboardDAO
        from superset.models.dashboard import Dashboard

        session_factory, engine = _make_session()

        timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        root = f"dashboard_export_{timestamp}"
        output_file = dashboard_file or f"{root}.zip"

        try:
            async with session_factory() as session:
                result = await session.execute(select(Dashboard.id))
                dashboard_ids = [row[0] for row in result.fetchall()]

                if not dashboard_ids:
                    click.echo("No dashboards found to export.")
                    await engine.dispose()
                    return

                dao = AsyncDashboardDAO(session)
                cmd = ExportDashboardsCommand(model_ids=dashboard_ids, dao=dao)
                cmd._root = root  # noqa: SLF001
                buf = await cmd.execute()

            buf.seek(0)
            with open(output_file, "wb") as fp:
                fp.write(buf.read())

            click.secho(f"Dashboards exported to {output_file}", fg="green")
        except Exception:  # pylint: disable=broad-except
            logger.exception(
                "There was an error when exporting the dashboards, please check "
                "the exception traceback in the log"
            )
            sys.exit(1)
        finally:
            await engine.dispose()

    anyio.run(_export)


# ------------------------------------------------------------------
# export-datasources
# ------------------------------------------------------------------


@click.command("export-datasources")
@click.option(
    "--datasource-file",
    "-f",
    default=None,
    help="Specify the file to export to",
)
def export_datasources(datasource_file: Optional[str] = None) -> None:
    """Export datasources to ZIP file"""
    # 1:1 port of superset_old/cli/importexport.py export_datasources.
    import anyio

    async def _export() -> None:
        from sqlalchemy import select

        from superset.commands.dataset.export import ExportDatasetsCommand
        from superset.db.daos.dataset import AsyncDatasetDAO
        from superset.models.connectors import SqlaTable

        session_factory, engine = _make_session()

        timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        root = f"dataset_export_{timestamp}"
        output_file = datasource_file or f"{root}.zip"

        try:
            async with session_factory() as session:
                result = await session.execute(select(SqlaTable.id))
                dataset_ids = [row[0] for row in result.fetchall()]

                if not dataset_ids:
                    click.echo("No datasources found to export.")
                    await engine.dispose()
                    return

                dao = AsyncDatasetDAO(session)
                cmd = ExportDatasetsCommand(model_ids=dataset_ids, dao=dao)
                cmd._root = root  # noqa: SLF001
                buf = await cmd.execute()

            buf.seek(0)
            with open(output_file, "wb") as fp:
                fp.write(buf.read())

            click.secho(f"Datasources exported to {output_file}", fg="green")
        except Exception:  # pylint: disable=broad-except
            logger.exception(
                "There was an error when exporting the datasets, please check "
                "the exception traceback in the log"
            )
            sys.exit(1)
        finally:
            await engine.dispose()

    anyio.run(_export)


# ------------------------------------------------------------------
# import-dashboards
# ------------------------------------------------------------------


@click.command("import-dashboards")
@click.option(
    "--path",
    "-p",
    required=True,
    help="Path to a single ZIP file",
)
@click.option(
    "--username",
    "-u",
    required=True,
    help="Specify the user name to assign dashboards to",
)
def import_dashboards(path: str, username: Optional[str]) -> None:
    """Import dashboards from ZIP file"""
    # 1:1 port of superset_old/cli/importexport.py import_dashboards.
    # Original: get_contents_from_bundle -> ImportDashboardsCommand(contents).run()
    # Port: read zip as BytesIO -> ImportDashboardsCommand(buf).execute()
    import anyio

    async def _import() -> None:
        import io as _io

        from superset.commands.dashboard.importers.v1 import ImportDashboardsCommand
        from superset.db.daos.dashboard import AsyncDashboardDAO

        session_factory, engine = _make_session()

        try:
            if is_zipfile(path):
                with open(path, "rb") as fp:
                    buf = _io.BytesIO(fp.read())
            else:
                with open(path) as file:
                    contents = {path: file.read()}
                # Non-ZIP path: pack into a minimal ZIP for the v1 importer
                buf = _io.BytesIO()
                with ZipFile(buf, "w") as zf:
                    for fname, fcontent in contents.items():
                        zf.writestr(fname, fcontent)
                buf.seek(0)

            async with session_factory() as session:
                # Resolve --username inside the import session so the ORM user
                # can be attached as owner — mirrors the original's
                # ``g.user = security_manager.find_user(username=...)`` before
                # ImportDashboardsCommand (superset_old/cli/importexport.py:
                # 149-164); without it imports are owner-less.
                user = None
                if username is not None:
                    from sqlalchemy import select

                    from superset.models.security import User

                    stmt = select(User).where(User.username == username)
                    user = (await session.execute(stmt)).scalars().first()
                    if user is None:
                        # FAB ``find_user`` returns None silently on a miss
                        # and the original proceeds with an owner-less import
                        # (superset_old/cli/importexport.py:149-150) — do NOT
                        # abort. ``import_datasources`` below behaves the same.
                        click.secho(
                            f"User not found: {username}; importing without owner",
                            fg="yellow",
                            err=True,
                        )

                dao = AsyncDashboardDAO(session)
                cmd = ImportDashboardsCommand(
                    buf,
                    dao=dao,
                    overwrite=True,
                    current_user=user,
                )
                try:
                    await cmd.execute()
                except Exception:  # pylint: disable=broad-except
                    logger.exception(
                        "There was an error when importing the dashboards(s), "
                        "please check the exception traceback in the log"
                    )
                    sys.exit(1)
        finally:
            await engine.dispose()

    anyio.run(_import)


# ------------------------------------------------------------------
# import-datasources
# ------------------------------------------------------------------


@click.command("import-datasources")
@click.option(
    "--path",
    "-p",
    help="Path to a single ZIP file",
)
@click.option(
    "--username",
    "-u",
    required=False,
    default="admin",
    help="Specify the user name to assign datasources to",
)
def import_datasources(path: str, username: Optional[str] = "admin") -> None:
    """Import datasources from ZIP file"""
    # 1:1 port of superset_old/cli/importexport.py import_datasources.
    # Original: override_user + get_contents_from_bundle
    # -> ImportDatasetsCommand(contents).run()
    # Port: read zip as BytesIO -> ImportDatasetsCommand(buf).execute()
    import anyio

    async def _import() -> None:
        import io as _io

        from superset.commands.dataset.importers.v1 import ImportDatasetsCommand
        from superset.db.daos.dataset import AsyncDatasetDAO
        from superset.utils.core import override_user

        session_factory, engine = _make_session()

        try:
            user = None
            if username is not None:
                from sqlalchemy import select

                from superset.models.security import User

                async with session_factory() as session:
                    stmt = select(User).where(User.username == username)
                    user = (await session.execute(stmt)).scalars().first()

            if is_zipfile(path):
                with open(path, "rb") as fp:
                    buf = _io.BytesIO(fp.read())
            else:
                with open(path) as file:
                    contents = {path: file.read()}
                buf = _io.BytesIO()
                with ZipFile(buf, "w") as zf:
                    for fname, fcontent in contents.items():
                        zf.writestr(fname, fcontent)
                buf.seek(0)

            with override_user(user=user):
                async with session_factory() as session:
                    dao = AsyncDatasetDAO(session)
                    cmd = ImportDatasetsCommand(
                        buf,
                        dao=dao,
                        overwrite=True,
                    )
                    try:
                        await cmd.execute()
                    except Exception:  # pylint: disable=broad-except
                        logger.exception(
                            "There was an error when importing the dataset(s), "
                            "please check the exception traceback in the log"
                        )
                        sys.exit(1)
        finally:
            await engine.dispose()

    anyio.run(_import)


# ------------------------------------------------------------------
# import-directory
# ------------------------------------------------------------------


@click.command("import-directory")
@click.argument("directory")
@click.option(
    "--overwrite",
    "-o",
    is_flag=True,
    help="Overwriting existing metadata definitions",
)
@click.option(
    "--force",
    "-f",
    is_flag=True,
    help="Force load data even if table already exists",
)
def import_directory(directory: str, overwrite: bool, force: bool) -> None:
    """Imports configs from a given directory"""
    # 1:1 port of superset_old/cli/importexport.py import_directory which
    # delegates to superset.examples.utils.load_configs_from_directory.
    from superset.examples.utils import (
        load_configs_from_directory,  # pylint: disable=import-outside-toplevel
    )

    load_configs_from_directory(
        root=Path(directory),
        overwrite=overwrite,
        force_data=force,
    )
