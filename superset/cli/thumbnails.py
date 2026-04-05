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

import click


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
                for i, (dash_id, title) in enumerate(dashboards):
                    if asynchronous:
                        action = "Triggering"
                        try:
                            from superset.tasks.thumbnails import (
                                cache_dashboard_thumbnail,
                            )

                            cache_dashboard_thumbnail.delay(None, dash_id, force=force)
                        except Exception as exc:
                            click.secho(
                                f"  Failed to trigger task for dashboard "
                                f"{dash_id}: {exc}",
                                fg="red",
                            )
                            continue
                    else:
                        action = "Processing"
                    click.secho(
                        f'  {action} dashboard "{title}" ({i + 1}/{len(dashboards)})',
                        fg="green",
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
                for i, (chart_id, name) in enumerate(charts):
                    if asynchronous:
                        action = "Triggering"
                        try:
                            from superset.tasks.thumbnails import (
                                cache_chart_thumbnail,
                            )

                            cache_chart_thumbnail.delay(None, chart_id, force=force)
                        except Exception as exc:
                            click.secho(
                                f"  Failed to trigger task for chart {chart_id}: {exc}",
                                fg="red",
                            )
                            continue
                    else:
                        action = "Processing"
                    click.secho(
                        f'  {action} chart "{name}" ({i + 1}/{len(charts)})',
                        fg="green",
                    )

        click.echo("Thumbnail computation complete.")
        await engine.dispose()

    anyio.run(_compute)
