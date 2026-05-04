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
"""Async port of ``superset_old/cli/viz_migrations.py``.

Implements the ``migrate-viz upgrade`` and ``migrate-viz downgrade``
commands.  Both run synchronously against the metadata DB via the
sync session helper :func:`superset.db.session.get_sync_session` —
the migration code in ``superset.migrations.shared.migrate_viz``
operates on raw SQLAlchemy ``Slice`` rows and is intrinsically blocking
(matches upstream).
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Type

import click
from click_option_group import optgroup, RequiredAnyOptionGroup

from superset.migrations.shared.migrate_viz.base import (
    MigrateViz,
    Slice,
)
from superset.migrations.shared.migrate_viz.processors import (
    MigrateAreaChart,
    MigrateBarChart,
    MigrateBubbleChart,
    MigrateDistBarChart,
    MigrateDualLine,
    MigrateHeatmapChart,
    MigrateHistogramChart,
    MigrateLineChart,
    MigratePivotTable,
    MigrateSankey,
    MigrateSunburst,
    MigrateTreeMap,
)
from superset.migrations.shared.utils import paginated_update


class VizType(str, Enum):
    AREA = "area"
    BAR = "bar"
    BUBBLE = "bubble"
    DIST_BAR = "dist_bar"
    DUAL_LINE = "dual_line"
    HEATMAP = "heatmap"
    HISTOGRAM = "histogram"
    LINE = "line"
    PIVOT_TABLE = "pivot_table"
    SANKEY = "sankey"
    SUNBURST = "sunburst"
    TREEMAP = "treemap"


MIGRATIONS: dict[VizType, Type[MigrateViz]] = {
    VizType.AREA: MigrateAreaChart,
    VizType.BAR: MigrateBarChart,
    VizType.BUBBLE: MigrateBubbleChart,
    VizType.DIST_BAR: MigrateDistBarChart,
    VizType.DUAL_LINE: MigrateDualLine,
    VizType.HEATMAP: MigrateHeatmapChart,
    VizType.HISTOGRAM: MigrateHistogramChart,
    VizType.LINE: MigrateLineChart,
    VizType.PIVOT_TABLE: MigratePivotTable,
    VizType.SANKEY: MigrateSankey,
    VizType.SUNBURST: MigrateSunburst,
    VizType.TREEMAP: MigrateTreeMap,
}

PREVIOUS_VERSION = {
    migration.target_viz_type: migration for migration in MIGRATIONS.values()
}


@click.group("migrate-viz")
def migrate_viz() -> None:
    """Migrate a viz from one type to another."""


@migrate_viz.command()
@optgroup.group(cls=RequiredAnyOptionGroup)
@optgroup.option(
    "--viz_type",
    "-t",
    help=f"The viz type to upgrade: {', '.join(list(VizType))}",
    type=str,
)
@optgroup.option(
    "--id",
    "ids",
    help="The chart ID to upgrade. It can be set multiple times.",
    type=int,
    multiple=True,
)
def upgrade(viz_type: str, ids: tuple[int, ...] | None = None) -> None:
    """Upgrade a viz to the latest version."""
    setup_logger()
    if viz_type:
        migrate_by_viz_type(VizType(viz_type))
    elif ids:
        migrate_by_id(ids)


@migrate_viz.command()
@optgroup.group(cls=RequiredAnyOptionGroup)
@optgroup.option(
    "--viz_type",
    "-t",
    help=f"The viz type to downgrade: {', '.join(list(VizType))}",
    type=str,
)
@optgroup.option(
    "--id",
    "ids",
    help="The chart ID to downgrade. It can be set multiple times.",
    type=int,
    multiple=True,
)
def downgrade(viz_type: str, ids: tuple[int, ...] | None = None) -> None:
    """Downgrade a viz to the previous version."""
    setup_logger()
    if viz_type:
        migrate_by_viz_type(VizType(viz_type), is_downgrade=True)
    elif ids:
        migrate_by_id(ids, is_downgrade=True)


def migrate_by_viz_type(viz_type: VizType, is_downgrade: bool = False) -> None:
    """Migrate every chart of the given viz type.

    The migrate-viz pipeline operates on the sync metadata DB; we open
    a fresh sync session for the duration of the migration and let the
    underlying ``paginated_update`` helper drive the commit cadence.
    """
    from superset.db.session import get_sync_session

    migration: Type[MigrateViz] = MIGRATIONS[viz_type]
    session = get_sync_session()
    try:
        if is_downgrade:
            migration.downgrade(session)
        else:
            migration.upgrade(session)
    finally:
        session.close()


def migrate_by_id(ids: tuple[int, ...], is_downgrade: bool = False) -> None:
    """Migrate a subset of charts by their primary keys."""
    from superset.db.session import get_sync_session

    session = get_sync_session()
    try:
        slices = session.query(Slice).filter(Slice.id.in_(ids))
        for slc in paginated_update(
            slices,
            lambda current, total: click.echo(
                f"{('Downgraded' if is_downgrade else 'Upgraded')} "
                f"{current}/{total} charts"
            ),
        ):
            if is_downgrade:
                PREVIOUS_VERSION[slc.viz_type].downgrade_slice(slc)
            elif slc.viz_type in MIGRATIONS:
                MIGRATIONS[slc.viz_type].upgrade_slice(slc)
        session.commit()
    finally:
        session.close()


def setup_logger() -> None:
    """Attach a stream handler to the alembic logger."""
    console_handler = logging.StreamHandler()
    logger = logging.getLogger("alembic")
    logger.addHandler(console_handler)
