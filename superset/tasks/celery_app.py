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

The app is created independently of Litestar (no legacy ``create_app`` call).
Signal handlers manage async engine disposal in forked worker processes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from celery import Celery
from celery.signals import task_postrun, worker_process_init

logger = logging.getLogger(__name__)

celery_app = Celery("superset")
celery_app.autodiscover_tasks(["superset.tasks"])


def _apply_celery_config() -> None:
    """Apply CELERY_CONFIG from SupersetSettings at module-load time.

    Workers never run the Litestar ``on_startup`` hook, so the broker
    URL / beat schedule must be wired here.
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
        logger.warning("Failed to apply CELERY_CONFIG", exc_info=True)


_apply_celery_config()

app = celery_app  # alias for ``celery -A superset.tasks.celery_app:app``


@worker_process_init.connect
def init_worker_db_engine(**kwargs: Any) -> None:
    """Create the async DB engine in each forked Celery worker process.

    Workers never run the Litestar ``on_startup`` hook, so the module-global
    async engine is never created — async tasks fail with
    ``RuntimeError: No engine has been created yet``.  Uses ``NullPool``
    because ``asyncio.run()`` creates a fresh event loop per task and pooled
    asyncpg connections cannot cross event loops.
    """
    try:
        from superset.config import SupersetSettings  # noqa: WPS433
        from superset.db.session import (  # noqa: WPS433
            create_worker_engine,
            dispose_engine,
            get_engine,
        )
    except ImportError:
        logger.debug("DB session module unavailable at worker init")
        return

    # Dispose any engine inherited across os.fork — pooled connections are
    # invalid post-fork.
    try:
        inherited = get_engine()
    except RuntimeError:
        inherited = None
    if inherited is not None:
        with contextlib.suppress(Exception):
            asyncio.run(dispose_engine(inherited))

    try:
        settings = SupersetSettings()  # type: ignore[call-arg]
    except Exception:  # noqa: BLE001
        logger.exception("Failed to load SupersetSettings at worker init")
        return

    # Configure feature-flag manager and stats logger: without this they stay
    # at their no-op defaults (workers never run the Litestar on_startup hook).
    # A missing feature-flag manager causes every ``is_feature_enabled`` to
    # return False — e.g. reports.scheduler never queues alerts.
    try:
        from superset.extensions import stats_logger_manager  # noqa: WPS433
        from superset.utils.feature_flags import (  # noqa: WPS433
            feature_flag_manager,
        )

        feature_flag_manager.init_from_config(settings.feature_flags)
        stats_logger_manager.configure(settings.stats_logger)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to configure worker feature-flag / stats managers")

    worker_engine = None
    try:
        worker_engine = create_worker_engine(settings.sqlalchemy_database_uri)
        logger.info("Worker async DB engine initialized (NullPool)")
    except Exception:  # noqa: BLE001
        logger.exception("Failed to initialize worker async DB engine")

    # Configure the event logger so audit ``Log`` rows are written for tasks
    # (e.g. ``execute_sql``); without this the default no-op logger is used.
    if worker_engine is not None:
        try:
            from superset.db.session import create_session_factory  # noqa: WPS433
            from superset.events import configure_event_logger  # noqa: WPS433

            worker_session_factory = create_session_factory(worker_engine)
            configure_event_logger(session_factory=worker_session_factory)
            logger.info("Worker event logger configured (AsyncDBEventLogger)")
        except Exception:  # noqa: BLE001
            logger.exception("Failed to configure worker event logger")


@task_postrun.connect
def teardown(  # pylint: disable=unused-argument
    retval: Any,
    *args: Any,
    **kwargs: Any,
) -> None:
    """Tear down the SQLAlchemy session after each task.

    Commits when ``SQLALCHEMY_COMMIT_ON_TEARDOWN`` is set and the task
    succeeded.  Removes the session unless ``CELERY_ALWAYS_EAGER`` is set.

    :see: https://docs.celeryq.dev/en/stable/userguide/signals.html#task-postrun
    :see: https://gist.github.com/twolfson/a1b329e9353f9b575131
    """
    try:
        from superset.config import SupersetSettings  # noqa: WPS433

        settings = SupersetSettings()  # type: ignore[call-arg]
    except Exception:  # noqa: BLE001
        # Settings failure must NOT skip session cleanup — leaking the
        # thread-local Session would pin a pool connection for the worker
        # thread's lifetime.
        settings = None

    if settings is not None and settings.sqlalchemy_commit_on_teardown:
        if not isinstance(retval, Exception):
            try:
                from superset.db.session import get_sync_session  # noqa: WPS433

                get_sync_session().commit()  # pylint: disable=consider-using-transaction
            except Exception:  # noqa: BLE001
                logger.debug("task_postrun commit failed", exc_info=True)

    if settings is None or not settings.celery_always_eager:
        try:
            from superset.db.session import remove_sync_session  # noqa: WPS433

            remove_sync_session()
        except Exception:  # noqa: BLE001
            logger.debug("task_postrun session.remove failed", exc_info=True)


# ``autodiscover_tasks`` only picks up modules literally named ``tasks.py``.
# Workers never run ``create_app`` (which imported these via CELERY_CONFIG.imports
# in the original), so each sibling module must be imported explicitly.
def _import_task_modules() -> None:
    import superset.tasks.async_queries  # noqa: F401
    import superset.tasks.cache  # noqa: F401
    import superset.tasks.scheduler  # noqa: F401
    import superset.tasks.slack  # noqa: F401
    import superset.tasks.thumbnails  # noqa: F401

    # Import failures make tasks silently unavailable — log loudly.
    try:
        import superset.tasks.sql_lab  # noqa: F401
    except Exception:  # noqa: BLE001
        logger.exception(
            "Failed to import superset.tasks.sql_lab; its task is unavailable"
        )
    try:
        import superset.tasks.sync_database_permissions  # noqa: F401
    except Exception:  # noqa: BLE001
        logger.exception(
            "Failed to import superset.tasks.sync_database_permissions; "
            "its task is unavailable"
        )


_import_task_modules()
