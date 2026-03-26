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
"""Report scheduler Celery tasks for Liteset.

Replaces ``superset/tasks/scheduler.py``. Task registration uses the
``liteset.tasks.*`` namespace; implementations delegate to the superset
command layer during the Strangler Fig migration.
"""
from __future__ import annotations

import logging
from typing import Any

from celery import Task

from liteset.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
    name="liteset.tasks.scheduler.scheduler",
    bind=True,
    autoretry_for=(Exception,),
    retry_kwargs={"max_retries": 3, "countdown": 60},
    retry_backoff=True,
)
def scheduler(self: Task) -> None:
    """Celery beat main scheduler for alert/report execution.

    Delegates to the superset implementation during migration.
    """
    from superset.tasks.scheduler import scheduler as _superset_scheduler

    _superset_scheduler()


@celery_app.task(name="liteset.tasks.scheduler.execute", bind=True)
def execute(self: Task, report_schedule_id: int) -> None:
    """Execute a single alert/report schedule.

    Delegates to the superset implementation during migration.
    """
    from superset.tasks.scheduler import execute as _superset_execute

    _superset_execute(report_schedule_id)


@celery_app.task(name="liteset.tasks.scheduler.prune_log")
def prune_log() -> None:
    """Prune old report schedule log entries.

    Delegates to the superset implementation during migration.
    """
    from superset.tasks.scheduler import prune_log as _superset_prune_log

    _superset_prune_log()


@celery_app.task(name="liteset.tasks.scheduler.prune_query", bind=True)
def prune_query(
    self: Task, retention_period_days: int | None = None, **kwargs: Any
) -> None:
    """Prune old SQL Lab query records.

    Delegates to the superset implementation during migration.
    """
    from superset.tasks.scheduler import prune_query as _superset_prune_query

    _superset_prune_query(retention_period_days, **kwargs)


@celery_app.task(name="liteset.tasks.scheduler.prune_logs", bind=True)
def prune_logs(
    self: Task, retention_period_days: int | None = None, **kwargs: Any
) -> None:
    """Prune old application log entries.

    Delegates to the superset implementation during migration.
    """
    from superset.tasks.scheduler import prune_logs as _superset_prune_logs

    _superset_prune_logs(retention_period_days, **kwargs)
