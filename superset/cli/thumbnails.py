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
"""Compute thumbnails CLI command.

Triggers thumbnail generation for dashboards and/or charts, either
synchronously or via Celery tasks.
"""

from __future__ import annotations

from typing import Any

import click


def _compute_one_thumbnail(
    task_obj: Any,
    model_id: int,
    label: str,
    name: str,
    index: int,
    total: int,
    *,
    asynchronous: bool,
    force: bool,
) -> None:
    """Trigger (async) or run (sync, in-process) one thumbnail computation.

    The synchronous path calls the bare task function — mirroring the original
    CLI's ``func(None, model.id, force=force)`` — so ``compute-thumbnails``
    without ``-a`` actually renders rather than no-op'ing.
    """
    action = "Triggering" if asynchronous else "Processing"
    try:
        if asynchronous:
            task_obj.delay(None, model_id, force=force)
        else:
            task_obj(None, model_id, force=force)
    except Exception as exc:  # noqa: BLE001
        click.secho(f"  Failed to process {label} {model_id}: {exc}", fg="red")
        return
    click.secho(f'  {action} {label} "{name}" ({index + 1}/{total})', fg="green")


@click.command("compute-thumbnails")
@click.option(
    "--asynchronous",
    "-a",
    is_flag=True,
    default=False,
    help="Trigger via Celery worker (async)",
)
@click.option(
    "--dashboards-only",
    "-d",
    is_flag=True,
    default=False,
    help="Only process dashboards",
)
@click.option(
    "--charts-only",
    "-c",
    is_flag=True,
    default=False,
    help="Only process charts",
)
@click.option(
    "--force",
    "-f",
    is_flag=True,
    default=False,
    help="Force refresh, even if previously cached",
)
@click.option("--model-id", "-i", multiple=True, type=int)
def compute_thumbnails(  # noqa: C901
    asynchronous: bool,
    dashboards_only: bool,
    charts_only: bool,
    force: bool,
    model_id: tuple[int, ...],
) -> None:
    """Compute thumbnails for dashboards and/or charts."""
    import anyio

    async def _compute() -> None:  # noqa: C901
        from sqlalchemy import text

        from superset.config import SupersetSettings
        from superset.db.engine import create_db_engine, create_session_factory

        settings = SupersetSettings()  # type: ignore[call-arg]
        engine = create_db_engine(settings.sqlalchemy_database_uri)
        session_factory = create_session_factory(engine)

        async with session_factory() as session:
            if not charts_only:
                click.echo("Processing dashboards...")
                if model_id:
                    placeholders = ", ".join(f":id{i}" for i in range(len(model_id)))
                    params = {f"id{i}": mid for i, mid in enumerate(model_id)}
                    result = await session.execute(
                        text(
                            f"SELECT id, dashboard_title FROM dashboards "  # noqa: S608
                            f"WHERE id IN ({placeholders})"
                        ),
                        params,
                    )
                else:
                    result = await session.execute(
                        text("SELECT id, dashboard_title FROM dashboards ORDER BY id")
                    )
                dashboards = result.fetchall()
                from superset.tasks.thumbnails import cache_dashboard_thumbnail

                for i, (dash_id, title) in enumerate(dashboards):
                    _compute_one_thumbnail(
                        cache_dashboard_thumbnail,
                        dash_id,
                        "dashboard",
                        title,
                        i,
                        len(dashboards),
                        asynchronous=asynchronous,
                        force=force,
                    )

            if not dashboards_only:
                click.echo("Processing charts...")
                if model_id:
                    placeholders = ", ".join(f":id{i}" for i in range(len(model_id)))
                    params = {f"id{i}": mid for i, mid in enumerate(model_id)}
                    result = await session.execute(
                        text(
                            f"SELECT id, slice_name FROM slices "  # noqa: S608
                            f"WHERE id IN ({placeholders})"
                        ),
                        params,
                    )
                else:
                    result = await session.execute(
                        text("SELECT id, slice_name FROM slices ORDER BY id")
                    )
                charts = result.fetchall()
                from superset.tasks.thumbnails import cache_chart_thumbnail

                for i, (chart_id, name) in enumerate(charts):
                    _compute_one_thumbnail(
                        cache_chart_thumbnail,
                        chart_id,
                        "chart",
                        name,
                        i,
                        len(charts),
                        asynchronous=asynchronous,
                        force=force,
                    )

        click.echo("Thumbnail computation complete.")
        await engine.dispose()

    anyio.run(_compute)
