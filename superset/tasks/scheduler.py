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
"""Report scheduler Celery tasks for Superset.

Self-contained implementations that use :func:`superset.db.session.get_sync_session`
for synchronous DB access inside Celery workers.  Complex report execution
(notification, screenshot) is stubbed and will be expanded incrementally.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from celery import Task
from sqlalchemy import text

from superset.tasks.celery_app import celery_app
from superset.tasks.cron_util import cron_schedule_window

logger = logging.getLogger(__name__)


@celery_app.task(
    name="superset.tasks.scheduler.scheduler",
    bind=True,
    autoretry_for=(Exception,),
    retry_kwargs={"max_retries": 3, "countdown": 60},
    retry_backoff=True,
)
def scheduler(self: Task) -> None:
    """Celery beat main scheduler for alert/report execution.

    Queries active :table:`report_schedule` records, computes the next
    run time via :func:`~superset.tasks.cron_util.cron_schedule_window`,
    and queues :func:`execute` tasks with the appropriate ETA.
    """
    from superset.db.session import get_sync_session

    session = get_sync_session()
    try:
        triggered_at = (
            datetime.fromisoformat(scheduler.request.expires)
            if scheduler.request.expires
            else datetime.now(tz=timezone.utc)
        )

        rows = session.execute(
            text(
                "SELECT id, name, crontab, timezone, working_timeout "
                "FROM report_schedule WHERE active = true"
            )
        ).fetchall()

        for row in rows:
            schedule_id, name, crontab, tz, working_timeout = row
            for schedule_dt in cron_schedule_window(triggered_at, crontab, tz):
                logger.info("Scheduling alert %s eta: %s", name, schedule_dt)
                async_options: dict[str, Any] = {"eta": schedule_dt}
                if working_timeout is not None:
                    # Add a generous hard time limit
                    async_options["time_limit"] = working_timeout + 120
                    async_options["soft_time_limit"] = working_timeout + 60
                execute.apply_async((schedule_id,), **async_options)
    finally:
        session.close()


@celery_app.task(name="superset.tasks.scheduler.execute", bind=True)
def execute(self: Task, report_schedule_id: int) -> None:
    """Execute a single alert/report schedule.

    Loads the report, runs the associated query / generates output, and
    sends notifications.  The complex execution pipeline (screenshots,
    email/Slack delivery) is currently stubbed.
    """
    from superset.db.session import get_sync_session

    session = get_sync_session()
    try:
        task_id = execute.request.id
        scheduled_dttm = execute.request.eta
        logger.info(
            "Executing alert/report %s, task_id=%s, scheduled_dttm=%s",
            report_schedule_id,
            task_id,
            scheduled_dttm,
        )

        row = session.execute(
            text("SELECT id, name, type FROM report_schedule WHERE id = :rid"),
            {"rid": report_schedule_id},
        ).fetchone()
        if not row:
            logger.warning("Report schedule %s not found", report_schedule_id)
            return

        logger.info(
            "Report schedule loaded: id=%s name=%s type=%s", row[0], row[1], row[2]
        )
        # TODO: implement full execution pipeline:
        # 1. Run alert query if applicable
        # 2. Generate screenshot/CSV/dataframe if report
        # 3. Send notifications (email, Slack)
        # 4. Update last_state, last_eval_dttm
        # 5. Create ReportExecutionLog entry
    except Exception:
        logger.exception(
            "An unexpected error occurred while executing report: %s",
            report_schedule_id,
        )
        self.update_state(state="FAILURE")
    finally:
        session.close()


@celery_app.task(name="superset.tasks.scheduler.prune_log")
def prune_log() -> None:
    """Delete old report execution log entries.

    Removes :table:`report_execution_log` rows whose associated
    :table:`report_schedule` ``log_retention`` period has elapsed.
    """
    from superset.db.session import get_sync_session

    session = get_sync_session()
    try:
        logger.info("Pruning report execution logs")
        # Delete logs older than each schedule's log_retention (days).
        # Default retention is 90 days per the ReportSchedule model.
        now = datetime.now(tz=timezone.utc)
        schedules = session.execute(
            text("SELECT id, log_retention FROM report_schedule")
        ).fetchall()

        total_deleted = 0
        for schedule_id, log_retention in schedules:
            retention_days = log_retention if log_retention else 90
            cutoff = now - timedelta(days=retention_days)
            result = session.execute(
                text(
                    "DELETE FROM report_execution_log "
                    "WHERE report_schedule_id = :sid AND start_dttm < :cutoff"
                ),
                {"sid": schedule_id, "cutoff": cutoff},
            )
            total_deleted += result.rowcount

        session.commit()
        logger.info("Pruned %d report execution log entries", total_deleted)
    except Exception:
        session.rollback()
        logger.exception("An exception occurred while pruning report schedule logs")
    finally:
        session.close()


@celery_app.task(name="superset.tasks.scheduler.prune_query", bind=True)
def prune_query(
    self: Task, retention_period_days: int | None = None, **kwargs: Any
) -> None:
    """Delete old SQL Lab query records.

    Removes rows from :table:`query` older than *retention_period_days*.
    If not specified, defaults to 30 days.
    """
    from superset.db.session import get_sync_session

    session = get_sync_session()
    try:
        if retention_period_days is None:
            retention_period_days = 30
            logger.info(
                "No retention_period_days specified, defaulting to %d",
                retention_period_days,
            )

        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=retention_period_days)
        logger.info(
            "Pruning queries older than %s (%d days)",
            cutoff,
            retention_period_days,
        )
        result = session.execute(
            text("DELETE FROM query WHERE start_time < :cutoff"),
            {"cutoff": cutoff},
        )
        session.commit()
        logger.info("Pruned %d query records", result.rowcount)
    except Exception:
        session.rollback()
        logger.exception("An error occurred while pruning queries")
    finally:
        session.close()


@celery_app.task(name="superset.tasks.scheduler.prune_logs", bind=True)
def prune_logs(
    self: Task, retention_period_days: int | None = None, **kwargs: Any
) -> None:
    """Delete old application log entries.

    Removes rows from :table:`logs` older than *retention_period_days*.
    If not specified, defaults to 90 days.
    """
    from superset.db.session import get_sync_session

    session = get_sync_session()
    try:
        if retention_period_days is None:
            retention_period_days = 90
            logger.info(
                "No retention_period_days specified, defaulting to %d",
                retention_period_days,
            )

        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=retention_period_days)
        logger.info(
            "Pruning application logs older than %s (%d days)",
            cutoff,
            retention_period_days,
        )
        result = session.execute(
            text("DELETE FROM logs WHERE dttm < :cutoff"),
            {"cutoff": cutoff},
        )
        session.commit()
        logger.info("Pruned %d application log records", result.rowcount)
    except Exception:
        session.rollback()
        logger.exception("An error occurred while pruning logs")
    finally:
        session.close()
