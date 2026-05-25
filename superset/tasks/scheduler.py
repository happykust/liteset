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

Ported 1:1 from ``superset_old/tasks/scheduler.py``.  The task names,
decorator options, control flow and side effects mirror the original.  The
only structural difference is that — because Celery workers run synchronously
and never boot the Litestar app — each task takes a synchronous
:class:`sqlalchemy.orm.Session` from
:func:`superset.db.session.get_sync_session` and delegates to the synchronous
command variants (``ExecuteReportScheduleCommand``,
``PruneReportScheduleLogCommand``, ``QueryPruneCommand``, ``LogPruneCommand``).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from celery import Task
from celery.exceptions import SoftTimeLimitExceeded
from celery.signals import task_failure

from superset.tasks.celery_app import celery_app
from superset.tasks.cron_util import cron_schedule_window

logger = logging.getLogger(__name__)


@task_failure.connect
def log_task_failure(  # pylint: disable=unused-argument
    sender: Task | None = None,
    task_id: str | None = None,
    exception: Exception | None = None,
    args: tuple[Any, ...] | None = None,
    kwargs: dict[str, Any] | None = None,
    traceback: Any = None,
    einfo: Any = None,
    **kw: Any,
) -> None:
    task_name = sender.name if sender else "Unknown"
    logger.exception("Celery task %s failed: %s", task_name, exception, exc_info=einfo)


@celery_app.task(
    name="reports.scheduler",
    bind=True,
    autoretry_for=(Exception,),
    retry_kwargs={
        "max_retries": 3,
        "countdown": 60,
    },  # Retry up to 3 times, wait 60s between
    retry_backoff=True,  # exponential backoff
)
def scheduler(self: Task) -> None:  # pylint: disable=unused-argument
    """
    Celery beat main scheduler for reports
    """
    from superset.config import SupersetSettings
    from superset.db.session import get_sync_session
    from superset.extensions import stats_logger_manager
    from superset.models.reports import ReportSchedule
    from superset.utils.feature_flags import feature_flag_manager

    stats_logger_manager.incr("reports.scheduler")

    if not feature_flag_manager.is_feature_enabled("ALERT_REPORTS"):
        return

    settings = SupersetSettings()  # type: ignore[call-arg]
    session = get_sync_session()
    try:
        triggered_at = (
            datetime.fromisoformat(scheduler.request.expires)
            - timedelta(seconds=settings.celery_beat_scheduler_expires)
            if scheduler.request.expires
            else datetime.now(tz=timezone.utc)
        )
        active_schedules = (
            session.query(ReportSchedule)
            .filter(ReportSchedule.active.is_(True))
            .all()
        )
        for active_schedule in active_schedules:
            for schedule in cron_schedule_window(
                triggered_at,
                active_schedule.crontab,
                active_schedule.timezone,
                window_size=settings.alert_reports_cron_window_size,
            ):
                logger.info(
                    "Scheduling alert %s eta: %s", active_schedule.name, schedule
                )
                async_options: dict[str, Any] = {"eta": schedule}
                if (
                    active_schedule.working_timeout is not None
                    and settings.alert_reports_working_time_out_kill
                ):
                    async_options["time_limit"] = (
                        active_schedule.working_timeout
                        + settings.alert_reports_working_time_out_lag
                    )
                    async_options["soft_time_limit"] = (
                        active_schedule.working_timeout
                        + settings.alert_reports_working_soft_time_out_lag
                    )
                execute.apply_async((active_schedule.id,), **async_options)
    finally:
        session.close()


@celery_app.task(name="reports.execute", bind=True)
def execute(self: Task, report_schedule_id: int) -> None:
    from superset.commands.report_exceptions import ReportScheduleUnexpectedError
    from superset.commands.report_execute import ExecuteReportScheduleCommand
    from superset.db.session import get_sync_session
    from superset.exceptions import CommandException
    from superset.extensions import stats_logger_manager
    from superset.utils.core import LoggerLevel
    from superset.utils.log import get_logger_from_status

    stats_logger_manager.incr("reports.execute")

    session = get_sync_session()
    task_id = None
    try:
        task_id = execute.request.id
        scheduled_dttm = execute.request.eta
        logger.info(
            "Executing alert/report, task id: %s, scheduled_dttm: %s",
            task_id,
            scheduled_dttm,
        )
        ExecuteReportScheduleCommand(
            task_id,
            report_schedule_id,
            scheduled_dttm,
            session,
        ).run()
    except ReportScheduleUnexpectedError:
        logger.exception(
            "An unexpected error occurred while executing the report: %s", task_id
        )
        self.update_state(state="FAILURE")
    except CommandException as ex:
        logger_func, level = get_logger_from_status(ex.status_code)
        logger_func(
            f"A downstream {level} occurred "
            f"while generating a report: {task_id}. {ex.message}",
            exc_info=True,
        )
        if level == LoggerLevel.EXCEPTION:
            self.update_state(state="FAILURE")
    finally:
        session.close()


@celery_app.task(name="reports.prune_log")
def prune_log() -> None:
    from superset.commands.report_log_prune import PruneReportScheduleLogCommand
    from superset.exceptions import CommandException
    from superset.extensions import stats_logger_manager

    stats_logger_manager.incr("reports.prune_log")

    try:
        PruneReportScheduleLogCommand().run()
    except SoftTimeLimitExceeded as ex:
        logger.warning("A timeout occurred while pruning report schedule logs: %s", ex)
    except CommandException:
        logger.exception("An exception occurred while pruning report schedule logs")


@celery_app.task(name="prune_query", bind=True)
def prune_query(
    self: Task, retention_period_days: int | None = None, **kwargs: Any
) -> None:
    from superset.commands.sqllab.query import QueryPruneCommand
    from superset.exceptions import CommandException
    from superset.extensions import stats_logger_manager

    stats_logger_manager.incr("prune_query")

    # TODO: Deprecated: Remove support for passing retention period via options in 6.0
    if retention_period_days is None:
        retention_period_days = prune_query.request.properties.get(
            "retention_period_days"
        )
        logger.warning(
            "Your `prune_query` beat schedule uses `options` to pass the retention "
            "period, please use `kwargs` instead."
        )

    try:
        QueryPruneCommand(retention_period_days).run()
    except CommandException as ex:
        logger.exception("An error occurred while pruning queries: %s", ex)


@celery_app.task(name="prune_logs", bind=True)
def prune_logs(
    self: Task, retention_period_days: int | None = None, **kwargs: Any
) -> None:
    from superset.commands.logs.prune import LogPruneCommand
    from superset.exceptions import CommandException
    from superset.extensions import stats_logger_manager

    stats_logger_manager.incr("prune_logs")

    # TODO: Deprecated: Remove support for passing retention period via options in 6.0
    if retention_period_days is None:
        retention_period_days = prune_logs.request.properties.get(
            "retention_period_days"
        )
        logger.warning(
            "Your `prune_logs` beat schedule uses `options` to pass the retention "
            "period, please use `kwargs` instead."
        )

    try:
        LogPruneCommand(retention_period_days).run()
    except CommandException as ex:
        logger.exception("An error occurred while pruning logs: %s", ex)
