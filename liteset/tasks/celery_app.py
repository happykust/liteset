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
"""Celery application factory for Liteset.

Replaces ``superset/tasks/celery_app.py``. The Celery app is created
independently of Litestar (no Flask ``create_app`` call). Signal handlers
manage async engine disposal in forked worker processes.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from celery import Celery
from celery.signals import task_postrun, worker_process_init

logger = logging.getLogger(__name__)

celery_app = Celery("liteset")
celery_app.autodiscover_tasks(["liteset.tasks"])


@worker_process_init.connect
def reset_db_connection_pool(**kwargs: Any) -> None:
    """Reset the async DB connection pool in forked worker processes.

    After :func:`os.fork` the parent's connection pool is invalid. We
    dispose the :class:`~sqlalchemy.ext.asyncio.AsyncEngine` so that
    each worker creates fresh connections on first use.
    """
    # Import lazily -- the engine is only available after app bootstrap.
    try:
        from liteset.db.session import dispose_engine, get_engine  # noqa: WPS433

        engine = get_engine()
    except (ImportError, RuntimeError):
        logger.debug("Engine not available at worker init; skipping disposal")
        return

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(dispose_engine(engine))
        else:
            loop.run_until_complete(dispose_engine(engine))
    except RuntimeError:
        asyncio.run(dispose_engine(engine))


@task_postrun.connect
def teardown(**kwargs: Any) -> None:
    """Clean up after each task execution.

    Session cleanup in liteset happens at the DAO layer via
    ``async_sessionmaker`` scoped sessions, so this is intentionally
    a no-op placeholder for future use.
    """


def register_task_aliases(app: Celery) -> None:
    """Map old ``superset.tasks.*`` names to ``liteset.tasks.*``.

    This allows existing Celery beat schedules and ``apply_async`` calls
    that reference the old task names to resolve correctly.
    """
    alias_map: dict[str, str] = {
        "superset.tasks.scheduler.execute": "liteset.tasks.scheduler.execute",
        "superset.tasks.cache.fetch_url": "liteset.tasks.cache.fetch_url",
        "superset.tasks.thumbnails.cache_chart_thumbnail": (
            "liteset.tasks.thumbnails.cache_chart_thumbnail"
        ),
        "superset.tasks.thumbnails.cache_dashboard_thumbnail": (
            "liteset.tasks.thumbnails.cache_dashboard_thumbnail"
        ),
        "superset.tasks.async_queries.load_chart_data_into_cache": (
            "liteset.tasks.async_queries.load_chart_data_into_cache"
        ),
        "superset.tasks.alerts.execute": "liteset.tasks.alerts.execute",
    }
    for old_name, new_name in alias_map.items():
        if new_name in app.tasks:
            app.tasks[old_name] = app.tasks[new_name]
