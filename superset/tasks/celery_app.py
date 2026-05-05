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
"""Celery application factory for Superset.

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

celery_app = Celery("superset")
celery_app.autodiscover_tasks(["superset.tasks"])


def _apply_celery_config() -> None:
    """Apply CELERY_CONFIG from SupersetSettings at module-load time.

    Workers start via ``celery -A superset.tasks.celery_app:app worker``
    and never run the Litestar app's ``on_startup`` hook, so the broker
    URL / beat schedule / imports must be wired here for the worker
    process to see them.  Mirrors the original Apache Superset wiring
    where ``celery_app = app.extensions['celery']`` was already
    pre-configured before workers booted.
    """
    try:
        from superset.config import SupersetSettings

        settings = SupersetSettings()  # type: ignore[call-arg]
        celery_config = getattr(settings, "celery_config", None)
        if celery_config is not None:
            celery_app.config_from_object(celery_config)
            logger.info(
                "Celery configured from CELERY_CONFIG (broker=%s)",
                celery_app.conf.broker_url or "<default>",
            )
    except Exception:  # noqa: BLE001
        # Settings may fail to load in CI/testing where SECRET_KEY etc.
        # aren't set; fall back to Celery defaults so module-import never
        # crashes the worker.
        logger.warning("Failed to apply CELERY_CONFIG", exc_info=True)


_apply_celery_config()

# Export the celery app globally for Celery (as run on the cmd line) to find
app = celery_app


@worker_process_init.connect
def reset_db_connection_pool(**kwargs: Any) -> None:
    """Reset the async DB connection pool in forked worker processes.

    After :func:`os.fork` the parent's connection pool is invalid. We
    dispose the :class:`~sqlalchemy.ext.asyncio.AsyncEngine` so that
    each worker creates fresh connections on first use.
    """
    # Import lazily -- the engine is only available after app bootstrap.
    try:
        from superset.db.session import dispose_engine, get_engine  # noqa: WPS433

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

    Session cleanup in superset happens at the DAO layer via
    ``async_sessionmaker`` scoped sessions, so this is intentionally
    a no-op placeholder for future use.
    """


def register_task_aliases(app: Celery) -> None:
    """Map legacy Superset task names to the new dotted module names.

    Existing Celery beat schedules and ``apply_async`` calls that reference
    old canonical names (e.g. ``reports.scheduler``, ``cache-warmup``,
    ``fetch_url``, ``slack.cache_channels``, ``prune_query``,
    ``prune_logs``) will resolve correctly against the new worker.
    """
    # Map: legacy_name -> new dotted task name
    alias_map: dict[str, str] = {
        # Reports / scheduler
        "reports.scheduler": "superset.tasks.scheduler.scheduler",
        "reports.execute": "superset.tasks.scheduler.execute",
        "reports.prune_log": "superset.tasks.scheduler.prune_log",
        "prune_query": "superset.tasks.scheduler.prune_query",
        "prune_logs": "superset.tasks.scheduler.prune_logs",
        # Cache warming (note the hyphen in the old name)
        "cache-warmup": "superset.tasks.cache.cache_warmup",
        "fetch_url": "superset.tasks.cache.fetch_url",
        # Slack
        "slack.cache_channels": "superset.tasks.slack.cache_channels",
        # Thumbnail names are unchanged; register them anyway so any
        # ``apply_async(task_name=...)`` spellings keep working.
        "cache_chart_thumbnail": "cache_chart_thumbnail",
        "cache_dashboard_thumbnail": "cache_dashboard_thumbnail",
        "cache_dashboard_screenshot": "cache_dashboard_screenshot",
        # Async query names unchanged
        "load_chart_data_into_cache": "load_chart_data_into_cache",
        "load_explore_json_into_cache": "load_explore_json_into_cache",
    }
    for old_name, new_name in alias_map.items():
        if old_name == new_name:
            continue  # Self-reference: skip (task already registered under this name)
        if new_name in app.tasks and old_name not in app.tasks:
            # ``TaskRegistry.register`` does not accept a ``name`` kwarg in
            # newer Celery versions. Re-register the task object with the
            # alias name by setting it directly on the registry dict.
            task_obj = app.tasks[new_name]
            app.tasks[old_name] = task_obj
            logger.debug("Registered task alias %r -> %r", old_name, new_name)


# ---------------------------------------------------------------------------
# Import all task modules so Celery workers discover tasks before any
# ``apply_async`` call — autodiscover_tasks only picks up modules named
# ``tasks.py`` inside the listed package; we must import each sibling module
# explicitly.  ``register_task_aliases`` is called afterwards so the new
# task objects are already in ``app.tasks``.
# ---------------------------------------------------------------------------

def _discover_and_alias() -> None:
    # Import order does not matter; just ensure all modules are loaded.
    import superset.tasks.alerts  # noqa: F401
    import superset.tasks.async_queries  # noqa: F401
    import superset.tasks.cache  # noqa: F401
    import superset.tasks.scheduler  # noqa: F401
    import superset.tasks.slack  # noqa: F401
    import superset.tasks.thumbnails  # noqa: F401

    # log_prune and sql_lab / sync_database_permissions register themselves
    # during import as well.
    try:
        import superset.tasks.log_prune  # noqa: F401
    except Exception:  # noqa: BLE001
        pass
    try:
        import superset.tasks.sql_lab  # noqa: F401
    except Exception:  # noqa: BLE001
        pass
    try:
        import superset.tasks.sync_database_permissions  # noqa: F401
    except Exception:  # noqa: BLE001
        pass

    register_task_aliases(celery_app)


_discover_and_alias()
