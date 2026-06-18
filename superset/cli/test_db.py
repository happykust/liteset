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
# mypy: ignore-errors
"""CLI commands for testing database connectivity and engine spec detection.

``test-db connectivity`` -- verify a database can be reached via SELECT 1.
``test-db engine-spec``  -- check which Superset engine spec matches a URI.
"""

from __future__ import annotations

import sys
from typing import Any

import click
import yaml
from rich.console import Console
from sqlalchemy import create_engine, text
from sqlalchemy.exc import NoSuchModuleError

from superset.databases.utils import make_url_safe


@click.group("test-db")
def test_db() -> None:
    """Database diagnostic commands."""


@test_db.command()
@click.argument("sqlalchemy_uri")
@click.option(
    "--connect-args",
    "-c",
    "raw_engine_kwargs",
    default=None,
    help="Engine kwargs as JSON or YAML string",
)
def connectivity(
    sqlalchemy_uri: str,
    raw_engine_kwargs: str | None = None,
) -> None:
    """Test database connectivity by connecting and running SELECT 1.

    Accepts any SQLAlchemy URI.  Uses a *sync* engine so that it works
    with arbitrary DBAPI drivers (no async requirement).
    """
    console = Console()

    engine_kwargs: dict[str, Any] = (
        yaml.safe_load(raw_engine_kwargs) if raw_engine_kwargs else {}
    )

    console.print(f"[bold]SQLAlchemy URI:[/bold] {sqlalchemy_uri}")
    if engine_kwargs:
        console.print(f"[bold]Engine kwargs:[/bold] {engine_kwargs}")

    try:
        engine = create_engine(sqlalchemy_uri, **engine_kwargs)
    except NoSuchModuleError:
        console.print("[red]No SQLAlchemy dialect found for the URI!")
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Failed to create engine: {exc}")
        sys.exit(1)

    console.print("\n[bold]Connecting to database...")
    try:
        with engine.connect() as conn:
            console.print(":thumbs_up: [green]Connected successfully!")

            console.print("[bold]Running:[/bold] SELECT 1")
            result = conn.execute(text("SELECT 1"))
            value = result.scalar()
            color = "green" if value == 1 else "red"
            console.print(f"[{color}]Result: {value}")

            if value != 1:
                console.print("[red]Unexpected result from SELECT 1.")
                sys.exit(1)

    except Exception as exc:  # noqa: BLE001
        console.print(f":thumbs_down: [red]Connection failed: {exc}")
        sys.exit(1)

    console.print(":thumbs_up: [green]All connectivity checks passed!")
    engine.dispose()


@test_db.command("engine-spec")
@click.argument("sqlalchemy_uri")
def engine_spec(sqlalchemy_uri: str) -> None:
    """Test which Superset engine spec matches a SQLAlchemy URI.

    Scans both native async engine specs and legacy sync engine specs
    to find the best match for the given URI.
    """
    console = Console()
    console.print(f"[bold]SQLAlchemy URI:[/bold] {sqlalchemy_uri}")

    try:
        url = make_url_safe(sqlalchemy_uri)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Invalid SQLAlchemy URI: {exc}")
        sys.exit(1)

    backend = url.get_backend_name()
    console.print(f"[bold]Detected backend:[/bold] {backend}")

    from superset.db.engine_specs import _NATIVE_SPECS

    if backend in _NATIVE_SPECS:
        spec = _NATIVE_SPECS[backend]
        console.print(
            f":thumbs_up: [green]Found native async engine spec: "
            f"[bold]{spec.__name__}[/bold] ({spec.__module__})"
        )
        return

    from superset.db.engine_specs import _get_sync_spec_map

    if backend in (sync_map := _get_sync_spec_map()):
        spec_cls = sync_map[backend]
        engine_name = getattr(spec_cls, "engine_name", spec_cls.__name__)
        console.print(
            f":warning: [yellow]Found sync engine spec (will use fallback wrapper): "
            f"[bold]{engine_name}[/bold] ({spec_cls.__module__})"
        )
        return

    console.print(
        ":thumbs_down: [red]No engine spec found for this SQLAlchemy URI. "
        "The database can still be used with Superset, but some "
        "functionality may be limited."
    )


@test_db.command()
@click.argument("sqlalchemy_uri")
@click.option(
    "--connect-args",
    "-c",
    "raw_engine_kwargs",
    default=None,
    help="Engine kwargs as JSON or YAML string",
)
def full(
    sqlalchemy_uri: str,
    raw_engine_kwargs: str | None = None,
) -> None:
    """Run all diagnostic tests against a database.

    Combines engine-spec detection, SQLAlchemy dialect inspection, and
    connectivity testing in a single command.
    """
    console = Console()
    console.clear()

    engine_kwargs: dict[str, Any] = (
        yaml.safe_load(raw_engine_kwargs) if raw_engine_kwargs else {}
    )

    console.print(f"[bold]SQLAlchemy URI:[/bold] {sqlalchemy_uri}")

    console.print("\n[bold]Checking for engine spec...")
    try:
        url = make_url_safe(sqlalchemy_uri)
        backend = url.get_backend_name()
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Invalid URI: {exc}")
        sys.exit(1)

    from superset.db.engine_specs import _get_sync_spec_map, _NATIVE_SPECS

    if backend in _NATIVE_SPECS:
        spec = _NATIVE_SPECS[backend]
        console.print(f":thumbs_up: [green]Native async spec: [bold]{spec.__name__}")
    elif backend in _get_sync_spec_map():
        spec_cls = _get_sync_spec_map()[backend]
        console.print(
            f":warning: [yellow]Sync fallback spec: "
            f"[bold]{getattr(spec_cls, 'engine_name', spec_cls.__name__)}"
        )
    else:
        console.print(":thumbs_down: [red]No engine spec found.")

    console.print("\n[bold]Testing SQLAlchemy dialect...")
    try:
        engine = create_engine(sqlalchemy_uri, **engine_kwargs)
    except NoSuchModuleError:
        console.print("[red]No SQLAlchemy dialect found for the URI!")
        sys.exit(1)

    dialect = engine.dialect

    console.print("[bold]Inspector functions:")
    for key in (
        "get_schema_names",
        "get_table_names",
        "get_view_names",
        "get_indexes",
        "get_table_comment",
        "get_columns",
        "get_unique_constraints",
        "get_check_constraints",
        "get_pk_constraint",
        "get_foreign_keys",
    ):
        console.print(f"  - {key}: {hasattr(dialect, key)}")

    console.print("[bold]Dialect attributes:")
    if hasattr(dialect, "dbapi") and dialect.dbapi is not None:
        console.print(f"  - dbapi: [bold]{dialect.dbapi.__name__}")
    else:
        console.print("  - dbapi: None")
    for attr in ("name", "driver", "supports_multivalues_insert"):
        console.print(f"  - {attr}: {getattr(dialect, attr, None)}")

    console.print("\n[bold]Testing database connectivity...")
    try:
        with engine.connect() as conn:
            console.print(":thumbs_up: [green]Connected successfully!")

            console.print("[bold]Running:[/bold] SELECT 1")
            result = conn.execute(text("SELECT 1"))
            value = result.scalar()
            color = "green" if value == 1 else "red"
            console.print(f"[{color}]Result: {value}")
    except Exception as exc:  # noqa: BLE001
        console.print(f":thumbs_down: [red]Connection failed: {exc}")
        sys.exit(1)

    console.print("\n:thumbs_up: [green]All checks passed!")
    engine.dispose()
