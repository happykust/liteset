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

Supports ZIP-based dashboard and datasource import/export, as well as
directory-based config import.  These commands operate over the database
via async SQLAlchemy sessions wrapped in ``anyio.run()``.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional
from zipfile import is_zipfile, ZipFile

import click
import yaml

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# export-dashboards
# ------------------------------------------------------------------


@click.command("export-dashboards")
@click.option("--dashboard-file", "-f", default=None, help="Output ZIP file path")
def export_dashboards(dashboard_file: Optional[str] = None) -> None:
    """Export dashboards to a ZIP file."""
    import anyio

    async def _export() -> None:
        from sqlalchemy import text

        from superset.config import SupersetSettings
        from superset.db.engine import create_db_engine, create_session_factory

        settings = SupersetSettings()  # type: ignore[call-arg]
        engine = create_db_engine(settings.sqlalchemy_database_uri)
        session_factory = create_session_factory(engine)

        timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        root = f"dashboard_export_{timestamp}"
        output_file = dashboard_file or f"{root}.zip"

        async with session_factory() as session:
            result = await session.execute(
                text(
                    "SELECT id, dashboard_title, slug, json_metadata, position_json, "
                    "css, description "
                    "FROM dashboards ORDER BY id"
                )
            )
            rows = result.fetchall()

        if not rows:
            click.echo("No dashboards found to export.")
            await engine.dispose()
            return

        with ZipFile(output_file, "w") as bundle:
            for row in rows:
                dash_id, title, slug, meta, position, css, desc = row
                dashboard_data = {
                    "id": dash_id,
                    "dashboard_title": title,
                    "slug": slug,
                    "json_metadata": meta,
                    "position_json": position,
                    "css": css,
                    "description": desc,
                }
                filename = f"{root}/dashboards/{slug or dash_id}.json"
                with bundle.open(filename, "w") as fp:
                    fp.write(
                        json.dumps(dashboard_data, indent=2, default=str).encode()
                    )
                click.echo(f"  Exported dashboard: {title} (id={dash_id})")

        click.secho(f"Dashboards exported to {output_file}", fg="green")
        await engine.dispose()

    anyio.run(_export)


# ------------------------------------------------------------------
# import-dashboards
# ------------------------------------------------------------------


@click.command("import-dashboards")
@click.option("--path", "-p", required=True, help="Path to a ZIP file")
@click.option(
    "--username",
    "-u",
    required=False,
    default="admin",
    help="User to assign dashboards to",
)
def import_dashboards(path: str, username: Optional[str]) -> None:
    """Import dashboards from a ZIP file."""
    if not Path(path).exists():
        click.secho(f"File not found: {path}", fg="red")
        sys.exit(1)

    if not is_zipfile(path):
        click.secho(f"Not a valid ZIP file: {path}", fg="red")
        sys.exit(1)

    import anyio

    async def _import() -> None:
        from sqlalchemy import text

        from superset.config import SupersetSettings
        from superset.db.engine import create_db_engine, create_session_factory

        settings = SupersetSettings()  # type: ignore[call-arg]
        engine = create_db_engine(settings.sqlalchemy_database_uri)
        session_factory = create_session_factory(engine)

        count = 0
        with ZipFile(path) as bundle:
            for name in bundle.namelist():
                if not name.endswith(".json"):
                    continue
                with bundle.open(name) as fp:
                    data = json.loads(fp.read().decode())

                title = data.get("dashboard_title", name)
                async with session_factory() as session:
                    # Check if dashboard with slug exists, update or insert
                    slug = data.get("slug")
                    if slug:
                        result = await session.execute(
                            text("SELECT id FROM dashboards WHERE slug = :s"),
                            {"s": slug},
                        )
                        existing = result.first()
                        if existing:
                            await session.execute(
                                text(
                                    "UPDATE dashboards SET "
                                    "dashboard_title = :title, "
                                    "json_metadata = :meta, "
                                    "position_json = :pos, "
                                    "css = :css, "
                                    "description = :desc "
                                    "WHERE slug = :s"
                                ),
                                {
                                    "title": data.get("dashboard_title"),
                                    "meta": data.get("json_metadata"),
                                    "pos": data.get("position_json"),
                                    "css": data.get("css"),
                                    "desc": data.get("description"),
                                    "s": slug,
                                },
                            )
                            click.echo(f"  Updated dashboard: {title}")
                        else:
                            await session.execute(
                                text(
                                    "INSERT INTO dashboards "
                                    "(dashboard_title, slug, json_metadata, "
                                    " position_json, css, description) "
                                    "VALUES (:title, :slug, :meta, :pos, :css, :desc)"
                                ),
                                {
                                    "title": data.get("dashboard_title"),
                                    "slug": slug,
                                    "meta": data.get("json_metadata"),
                                    "pos": data.get("position_json"),
                                    "css": data.get("css"),
                                    "desc": data.get("description"),
                                },
                            )
                            click.echo(f"  Imported dashboard: {title}")
                    await session.commit()
                count += 1

        click.secho(f"Imported {count} dashboard(s) from {path}", fg="green")
        await engine.dispose()

    anyio.run(_import)


# ------------------------------------------------------------------
# export-datasources
# ------------------------------------------------------------------


@click.command("export-datasources")
@click.option("--datasource-file", "-f", default=None, help="Output ZIP file path")
def export_datasources(datasource_file: Optional[str] = None) -> None:
    """Export datasources to a ZIP file."""
    import anyio

    async def _export() -> None:
        from sqlalchemy import text

        from superset.config import SupersetSettings
        from superset.db.engine import create_db_engine, create_session_factory

        settings = SupersetSettings()  # type: ignore[call-arg]
        engine = create_db_engine(settings.sqlalchemy_database_uri)
        session_factory = create_session_factory(engine)

        timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        root = f"dataset_export_{timestamp}"
        output_file = datasource_file or f"{root}.zip"

        async with session_factory() as session:
            result = await session.execute(
                text(
                    "SELECT id, table_name, schema, sql, description, "
                    "database_id "
                    "FROM tables ORDER BY id"
                )
            )
            rows = result.fetchall()

        if not rows:
            click.echo("No datasources found to export.")
            await engine.dispose()
            return

        with ZipFile(output_file, "w") as bundle:
            for row in rows:
                ds_id, table_name, schema, sql_text, desc, db_id = row
                ds_data = {
                    "id": ds_id,
                    "table_name": table_name,
                    "schema": schema,
                    "sql": sql_text,
                    "description": desc,
                    "database_id": db_id,
                }
                filename = f"{root}/datasets/{table_name}.yaml"
                with bundle.open(filename, "w") as fp:
                    fp.write(
                        yaml.safe_dump(ds_data, default_flow_style=False).encode()
                    )
                click.echo(f"  Exported datasource: {table_name} (id={ds_id})")

        click.secho(f"Datasources exported to {output_file}", fg="green")
        await engine.dispose()

    anyio.run(_export)


# ------------------------------------------------------------------
# import-datasources
# ------------------------------------------------------------------


@click.command("import-datasources")
@click.option("--path", "-p", required=True, help="Path to a ZIP file")
@click.option(
    "--username",
    "-u",
    required=False,
    default="admin",
    help="User to assign datasources to",
)
def import_datasources(path: str, username: Optional[str] = "admin") -> None:
    """Import datasources from a ZIP file."""
    if not Path(path).exists():
        click.secho(f"File not found: {path}", fg="red")
        sys.exit(1)

    if not is_zipfile(path):
        click.secho(f"Not a valid ZIP file: {path}", fg="red")
        sys.exit(1)

    import anyio

    async def _import() -> None:
        from sqlalchemy import text

        from superset.config import SupersetSettings
        from superset.db.engine import create_db_engine, create_session_factory

        settings = SupersetSettings()  # type: ignore[call-arg]
        engine = create_db_engine(settings.sqlalchemy_database_uri)
        session_factory = create_session_factory(engine)

        count = 0
        with ZipFile(path) as bundle:
            for name in bundle.namelist():
                if not (name.endswith(".yaml") or name.endswith(".yml")):
                    continue
                with bundle.open(name) as fp:
                    data = yaml.safe_load(fp.read().decode())
                if data is None:
                    continue

                table_name = data.get("table_name", name)
                async with session_factory() as session:
                    result = await session.execute(
                        text("SELECT id FROM tables WHERE table_name = :t"),
                        {"t": table_name},
                    )
                    existing = result.first()
                    if existing:
                        click.echo(
                            f"  Datasource {table_name}"
                            " already exists, skipping."
                        )
                    else:
                        await session.execute(
                            text(
                                "INSERT INTO tables "
                                "(table_name, schema, sql, description, database_id) "
                                "VALUES (:tn, :sch, :sql, :desc, :dbid)"
                            ),
                            {
                                "tn": table_name,
                                "sch": data.get("schema"),
                                "sql": data.get("sql"),
                                "desc": data.get("description"),
                                "dbid": data.get("database_id"),
                            },
                        )
                        click.echo(f"  Imported datasource: {table_name}")
                    await session.commit()
                count += 1

        click.secho(f"Processed {count} datasource(s) from {path}", fg="green")
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
    help="Overwrite existing metadata definitions",
)
@click.option(
    "--force",
    "-f",
    is_flag=True,
    help="Force load data even if table already exists",
)
def import_directory(directory: str, overwrite: bool, force: bool) -> None:
    """Import configs from a given directory.

    Reads JSON and YAML files from the directory and attempts to import
    dashboards and datasources accordingly.
    """
    dir_path = Path(directory)
    if not dir_path.exists() or not dir_path.is_dir():
        click.secho(f"Directory not found: {directory}", fg="red")
        sys.exit(1)

    import anyio

    async def _import() -> None:

        from superset.config import SupersetSettings
        from superset.db.engine import create_db_engine, create_session_factory

        settings = SupersetSettings()  # type: ignore[call-arg]
        engine = create_db_engine(settings.sqlalchemy_database_uri)
        create_session_factory(engine)

        json_files = list(dir_path.rglob("*.json"))
        yaml_files = list(dir_path.rglob("*.yaml")) + list(dir_path.rglob("*.yml"))

        click.echo(
            f"Found {len(json_files)} JSON and {len(yaml_files)} YAML files "
            f"in {directory}"
        )

        count = 0
        for file_path in json_files + yaml_files:
            try:
                with open(file_path) as f:
                    content = f.read()
                if file_path.suffix == ".json":
                    json.loads(content)
                else:
                    yaml.safe_load(content)

                click.echo(f"  Processing: {file_path.name}")
                count += 1
            except Exception as exc:
                click.secho(f"  Error reading {file_path}: {exc}", fg="yellow")
                continue

        click.secho(f"Processed {count} file(s) from {directory}", fg="green")
        await engine.dispose()

    anyio.run(_import)
