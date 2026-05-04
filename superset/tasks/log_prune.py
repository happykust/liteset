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
"""Celery beat task wrapper around :class:`LogPruneCommand`.

The original ``superset_old/tasks/scheduler.py:prune_logs`` registered
the ``prune_logs`` task that delegated to ``LogPruneCommand``.  In
Liteset we keep the task isolated in this module so the command lives
under :mod:`superset.commands.logs.prune` and the task wiring stays
self-contained.

The task is enabled by adding an entry to the Celery beat schedule of
:class:`superset.config.CeleryConfig`, e.g.::

    "prune_logs": {
        "task": "prune_logs",
        "schedule": crontab(minute=0, hour=0),
        "kwargs": {"retention_period_days": 180},
    }
"""

from __future__ import annotations

import logging
from typing import Any

from celery import Task

from superset.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="prune_logs", bind=True)
def prune_logs(
    self: Task,
    retention_period_days: int | None = None,
    **kwargs: Any,
) -> None:
    """Delete old rows from the FAB ``logs`` activity table.

    Mirrors ``superset_old/tasks/scheduler.py:prune_logs`` 1:1 — including
    the deprecated fallback that reads ``retention_period_days`` from the
    Celery message ``properties`` when not passed as a kwarg.
    """
    from superset.commands.logs.prune import LogPruneCommand
    from superset.exceptions import CommandException

    # Mirror the original stats counter increment.
    try:
        from flask import current_app

        stats_logger = current_app.config.get("STATS_LOGGER") if current_app else None
        if stats_logger is not None:
            stats_logger.incr("prune_logs")
    except Exception:  # noqa: BLE001
        # Stats logging failures must never abort the prune.
        logger.debug("STATS_LOGGER unavailable for prune_logs", exc_info=True)

    # Deprecated: support passing the retention period via Celery
    # message ``properties`` ("options" in old beat schedules).
    if retention_period_days is None:
        properties = getattr(prune_logs.request, "properties", None) or {}
        retention_period_days = properties.get("retention_period_days")
        logger.warning(
            "Your `prune_logs` beat schedule uses `options` to pass the retention "
            "period, please use `kwargs` instead."
        )

    if retention_period_days is None:
        # Mirror the implicit fallback used by superset/tasks/scheduler.py
        # so an unconfigured beat entry still completes safely.
        retention_period_days = 90
        logger.info(
            "No retention_period_days specified, defaulting to %d",
            retention_period_days,
        )

    try:
        LogPruneCommand(retention_period_days).run()
    except CommandException as ex:
        logger.exception("An error occurred while pruning logs: %s", ex)
