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
"""Async ports of the five ``legacy_*`` CLI commands from
``superset_old/cli/importexport.py``.

These commands operate on the deprecated V0 JSON / YAML dashboard /
datasource formats.  Apache Superset has long shipped V1 zip-based
import/export as the canonical pipeline (see ``importexport.py``), but
the legacy variants are kept in upstream so that automation written
against pre-V1 Superset releases keeps working.

Each command is a 1:1 port of its upstream counterpart with the
following adaptations:

* ``flask.g.user`` is replaced by an explicit ``override_user`` context
  obtained from :func:`superset.utils.core.override_user` (already
  ported to the AsyncSession surface).
* ``security_manager.find_user`` is invoked through the async
  :class:`AsyncSecurityManager` from :mod:`superset.security.manager`.
* The command bodies that don't actually need an event-loop run
  synchronously (matching the original CLI) and read the metadata DB
  via the sync session helper :func:`superset.db.session.get_sync_session`.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

import click
import yaml

logger = logging.getLogger(__name__)


@click.command("legacy-export-dashboards")
@click.option(
    "--dashboard-file",
    "-f",
    default=None,
    help="Specify the file to export to",
)
@click.option(
    "--print_stdout",
    "-p",
    is_flag=True,
    default=False,
    help="Print JSON to stdout",
)
def legacy_export_dashboards(
    dashboard_file: Optional[str], print_stdout: bool = False
) -> None:
    """Export dashboards to JSON (deprecated V0 format)."""
    from superset.utils import dashboard_import_export

    data = dashboard_import_export.export_dashboards()
    if print_stdout or not dashboard_file:
        print(data)
    if dashboard_file:
        logger.info("Exporting dashboards to %s", dashboard_file)
        with open(dashboard_file, "w", encoding="utf-8") as data_stream:
            data_stream.write(data)


@click.command("legacy-export-datasources")
@click.option(
    "--datasource-file",
    "-f",
    default=None,
    help="Specify the file to export to",
)
@click.option(
    "--print_stdout",
    "-p",
    is_flag=True,
    default=False,
    help="Print YAML to stdout",
)
@click.option(
    "--back-references",
    "-b",
    is_flag=True,
    default=False,
    help="Include parent back references",
)
@click.option(
    "--include-defaults",
    "-d",
    is_flag=True,
    default=False,
    help="Include fields containing defaults",
)
def legacy_export_datasources(
    datasource_file: Optional[str],
    print_stdout: bool = False,
    back_references: bool = False,
    include_defaults: bool = False,
) -> None:
    """Export datasources to YAML (deprecated V0 format)."""
    from superset.utils import dict_import_export

    data = dict_import_export.export_to_dict(
        recursive=True,
        back_references=back_references,
        include_defaults=include_defaults,
    )
    if print_stdout or not datasource_file:
        yaml.safe_dump(data, sys.stdout, default_flow_style=False)
    if datasource_file:
        logger.info("Exporting datasources to %s", datasource_file)
        with open(datasource_file, "w", encoding="utf-8") as data_stream:
            yaml.safe_dump(data, data_stream, default_flow_style=False)


@click.command("legacy-import-dashboards")
@click.option(
    "--path",
    "-p",
    help="Path to a single JSON file or path containing multiple JSON "
    "files to import (*.json)",
)
@click.option(
    "--recursive",
    "-r",
    is_flag=True,
    default=False,
    help="recursively search the path for json files",
)
@click.option(
    "--username",
    "-u",
    default=None,
    help="Specify the user name to assign dashboards to",
)
def legacy_import_dashboards(path: str, recursive: bool, username: str) -> None:
    """Import dashboards from JSON file (deprecated V0 format)."""
    import anyio

    anyio.run(_legacy_import_dashboards_async, path, recursive, username)


async def _legacy_import_dashboards_async(
    path: str, recursive: bool, username: str | None
) -> None:
    from superset.config import SupersetSettings
    from superset.db.session import create_db_engine, create_session_factory
    from superset.utils.core import override_user

    try:
        from superset.commands.dashboard.importers.v0 import (
            ImportDashboardsCommand,
        )
    except ImportError:
        click.secho(
            "V0 dashboard importer is not available in this Superset build. "
            "Use ``superset import-dashboards`` (V1 zip-based importer) instead.",
            err=True,
            fg="red",
        )
        sys.exit(1)
        return

    path_object = Path(path)
    files: list[Path] = []
    if path_object.is_file():
        files.append(path_object)
    elif path_object.exists() and not recursive:
        files.extend(path_object.glob("*.json"))
    elif path_object.exists() and recursive:
        files.extend(path_object.rglob("*.json"))

    contents: dict[str, str] = {}
    for path_ in files:
        with open(path_, encoding="utf-8") as fp:
            contents[path_.name] = fp.read()

    settings = SupersetSettings()  # type: ignore[call-arg]
    engine = create_db_engine(settings.sqlalchemy_database_uri)
    session_factory = create_session_factory(engine)

    try:
        user = None
        if username is not None:
            from sqlalchemy import select

            from superset.models.security import User

            async with session_factory() as session:
                stmt = select(User).where(User.username == username)
                user = (await session.execute(stmt)).scalars().first()

        with override_user(user):
            try:
                ImportDashboardsCommand(contents).run()
            except Exception:  # noqa: BLE001
                logger.exception("Error when importing dashboard")
                sys.exit(1)
    finally:
        await engine.dispose()


@click.command("legacy-import-datasources")
@click.option(
    "--path",
    "-p",
    help="Path to a single YAML file or path containing multiple YAML "
    "files to import (*.yaml or *.yml)",
)
@click.option(
    "--sync",
    "-s",
    "sync",
    default="",
    help="comma separated list of element types to synchronize "
    'e.g. "metrics,columns" deletes metrics and columns in the DB '
    "that are not specified in the YAML file",
)
@click.option(
    "--recursive",
    "-r",
    is_flag=True,
    default=False,
    help="recursively search the path for yaml files",
)
def legacy_import_datasources(path: str, sync: str, recursive: bool) -> None:
    """Import datasources from YAML (deprecated V0 format)."""
    try:
        from superset.commands.dataset.importers.v0 import (
            ImportDatasetsCommand,
        )
    except ImportError:
        click.secho(
            "V0 dataset importer is not available in this Superset build. "
            "Use ``superset import-datasources`` (V1 zip-based importer) instead.",
            err=True,
            fg="red",
        )
        sys.exit(1)
        return

    sync_array = sync.split(",")
    sync_columns = "columns" in sync_array
    sync_metrics = "metrics" in sync_array

    path_object = Path(path)
    files: list[Path] = []
    if path_object.is_file():
        files.append(path_object)
    elif path_object.exists() and not recursive:
        files.extend(path_object.glob("*.yaml"))
        files.extend(path_object.glob("*.yml"))
    elif path_object.exists() and recursive:
        files.extend(path_object.rglob("*.yaml"))
        files.extend(path_object.rglob("*.yml"))

    contents: dict[str, str] = {}
    for path_ in files:
        with open(path_, encoding="utf-8") as fp:
            contents[path_.name] = fp.read()

    try:
        ImportDatasetsCommand(
            contents, sync_columns=sync_columns, sync_metrics=sync_metrics
        ).run()
    except Exception:  # noqa: BLE001
        logger.exception("Error when importing dataset")
        sys.exit(1)


@click.command("legacy-export-datasource-schema")
@click.option(
    "--back-references",
    "-b",
    is_flag=True,
    default=False,
    help="Include parent back references",
)
def legacy_export_datasource_schema(back_references: bool) -> None:
    """Export datasource YAML schema to stdout (deprecated V0 format)."""
    from superset.utils import dict_import_export

    data = dict_import_export.export_schema_to_dict(back_references=back_references)
    yaml.safe_dump(data, sys.stdout, default_flow_style=False)
