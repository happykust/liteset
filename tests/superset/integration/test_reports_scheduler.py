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
"""Flask-free port of ``tests/integration_tests/reports/scheduler_tests.py``.

Drives the real ``reports.scheduler`` and ``reports.execute`` Celery tasks
(``superset.tasks.scheduler``) against the seeded Postgres backend.

Behaviour-preserving adaptations:

* The tasks read their database through the synchronous Celery session
  (``superset.db.session.get_sync_session``), so report schedules are
  inserted on that session (rolled back / deleted afterwards), not the async
  ``db_session``.
* Config no longer comes from ``app.config``.  The scheduler builds a fresh
  :class:`~superset.config.SupersetSettings`; the
  ``ALERT_REPORTS_WORKING_TIME_OUT_KILL`` toggle is applied by patching
  ``superset.config.SupersetSettings`` to return an instance with
  ``alert_reports_working_time_out_kill`` overridden (the lag defaults of 10 /
  1 seconds match the upstream test config, giving 3610 / 3601).
* The ``ALERT_REPORTS`` feature flag (off by default in the test env) is
  enabled by patching ``superset.utils.feature_flags.feature_flag_manager``.
* The ``execute`` task delegates to the **synchronous**
  ``superset.commands.report_execute.ExecuteReportScheduleCommand`` (the port
  of ``AsyncExecuteReportScheduleCommand``); its ``__init__`` / ``run`` are
  patched accordingly.
* ``CommandException`` logging goes through ``get_logger_from_status`` which
  resolves the level-specific logger off ``superset.events.logger`` (the
  single source of truth for that helper in the port), so the logger patch
  targets ``superset.events.logger`` instead of ``superset.utils.log.logger``.
  The emitted message is identical to upstream.
"""

from __future__ import annotations

from collections.abc import Iterator
from random import randint
from unittest.mock import MagicMock, patch

import pytest
from freezegun import freeze_time
from freezegun.api import FakeDatetime
from sqlalchemy.orm import Session

from superset.config import SupersetSettings
from superset.db.session import get_sync_session
from superset.models.reports import ReportSchedule, ReportScheduleType
from superset.models.security import User
from superset.tasks.scheduler import execute, scheduler


@pytest.fixture
def sync_session() -> Iterator[Session]:
    session = get_sync_session()
    try:
        yield session
    finally:
        session.rollback()


@pytest.fixture
def owners(sync_session: Session) -> list[User]:
    admin = (
        sync_session.query(User).filter(User.username == "admin").one_or_none()
    )
    if admin is None:
        admin = User(
            username="admin",
            first_name="admin",
            last_name="user",
            email="admin@example.com",
        )
        sync_session.add(admin)
        sync_session.flush()
    return [admin]


def _insert_report_schedule(
    sync_session: Session,
    *,
    crontab: str,
    timezone: str,
    owners: list[User],
    name: str = "report",
) -> ReportSchedule:
    report_schedule = ReportSchedule(
        type=ReportScheduleType.ALERT,
        name=name,
        crontab=crontab,
        timezone=timezone,
        owners=owners,
        active=True,
    )
    sync_session.add(report_schedule)
    sync_session.commit()
    return report_schedule


@pytest.fixture
def alert_reports_enabled() -> Iterator[MagicMock]:
    """Patch the feature-flag manager so ``ALERT_REPORTS`` is enabled."""
    ff = MagicMock()
    ff.is_feature_enabled.return_value = True
    with patch(
        "superset.utils.feature_flags.feature_flag_manager", ff
    ):
        yield ff


@patch("superset.tasks.scheduler.execute.apply_async")
def test_scheduler_celery_timeout_ny(
    execute_mock, sync_session: Session, owners, alert_reports_enabled
):
    """Reports scheduler: celery soft and hard timeout (NY timezone)."""
    report_schedule = _insert_report_schedule(
        sync_session,
        crontab="0 4 * * *",
        timezone="America/New_York",
        owners=owners,
    )

    try:
        with freeze_time("2020-01-01T09:00:00Z"):
            scheduler()
            assert execute_mock.call_args[1]["soft_time_limit"] == 3601
            assert execute_mock.call_args[1]["time_limit"] == 3610
    finally:
        sync_session.delete(report_schedule)
        sync_session.commit()


@patch("superset.tasks.scheduler.execute.apply_async")
def test_scheduler_celery_no_timeout_ny(
    execute_mock, sync_session: Session, owners, alert_reports_enabled
):
    """Reports scheduler: no celery timeout when kill is disabled (NY)."""
    report_schedule = _insert_report_schedule(
        sync_session,
        crontab="0 4 * * *",
        timezone="America/New_York",
        owners=owners,
    )
    settings = SupersetSettings().model_copy(  # type: ignore[call-arg]
        update={"alert_reports_working_time_out_kill": False}
    )

    try:
        with (
            patch("superset.config.SupersetSettings", return_value=settings),
            freeze_time("2020-01-01T09:00:00Z"),
        ):
            scheduler()
            assert execute_mock.call_args[1] == {"eta": FakeDatetime(2020, 1, 1, 9, 0)}
    finally:
        sync_session.delete(report_schedule)
        sync_session.commit()


@patch("superset.tasks.scheduler.execute.apply_async")
def test_scheduler_celery_timeout_utc(
    execute_mock, sync_session: Session, owners, alert_reports_enabled
):
    """Reports scheduler: celery soft and hard timeout (UTC timezone)."""
    report_schedule = _insert_report_schedule(
        sync_session,
        crontab="0 9 * * *",
        timezone="UTC",
        owners=owners,
    )

    try:
        with freeze_time("2020-01-01T09:00:00Z"):
            scheduler()
            assert execute_mock.call_args[1]["soft_time_limit"] == 3601
            assert execute_mock.call_args[1]["time_limit"] == 3610
    finally:
        sync_session.delete(report_schedule)
        sync_session.commit()


@patch("superset.tasks.scheduler.execute.apply_async")
def test_scheduler_celery_no_timeout_utc(
    execute_mock, sync_session: Session, owners, alert_reports_enabled
):
    """Reports scheduler: no celery timeout when kill is disabled (UTC)."""
    report_schedule = _insert_report_schedule(
        sync_session,
        crontab="0 9 * * *",
        timezone="UTC",
        owners=owners,
    )
    settings = SupersetSettings().model_copy(  # type: ignore[call-arg]
        update={"alert_reports_working_time_out_kill": False}
    )

    try:
        with (
            patch("superset.config.SupersetSettings", return_value=settings),
            freeze_time("2020-01-01T09:00:00Z"),
        ):
            scheduler()
            assert execute_mock.call_args[1] == {"eta": FakeDatetime(2020, 1, 1, 9, 0)}
    finally:
        sync_session.delete(report_schedule)
        sync_session.commit()


@patch("superset.tasks.scheduler.execute.apply_async")
def test_scheduler_feature_flag_off(execute_mock, sync_session: Session, owners):
    """Reports scheduler: nothing scheduled with the feature flag off."""
    ff = MagicMock()
    ff.is_feature_enabled.return_value = False
    report_schedule = _insert_report_schedule(
        sync_session,
        crontab="0 9 * * *",
        timezone="UTC",
        owners=owners,
    )

    try:
        with (
            patch("superset.utils.feature_flags.feature_flag_manager", ff),
            freeze_time("2020-01-01T09:00:00Z"),
        ):
            scheduler()
            execute_mock.assert_not_called()
    finally:
        sync_session.delete(report_schedule)
        sync_session.commit()


@patch("superset.commands.report_execute.ExecuteReportScheduleCommand.__init__")
@patch("superset.commands.report_execute.ExecuteReportScheduleCommand.run")
@patch("superset.tasks.scheduler.execute.update_state")
def test_execute_task(
    update_state_mock, command_mock, init_mock, sync_session: Session, owners
):
    from superset.commands.report_exceptions import ReportScheduleUnexpectedError

    report_schedule = _insert_report_schedule(
        sync_session,
        crontab="0 4 * * *",
        timezone="America/New_York",
        owners=owners,
        name=f"report-{randint(0, 1000)}",  # noqa: S311
    )
    init_mock.return_value = None
    command_mock.side_effect = ReportScheduleUnexpectedError("Unexpected error")
    try:
        with freeze_time("2020-01-01T09:00:00Z"):
            execute(report_schedule.id)
            update_state_mock.assert_called_with(state="FAILURE")
    finally:
        sync_session.delete(report_schedule)
        sync_session.commit()


@patch("superset.commands.report_execute.ExecuteReportScheduleCommand.__init__")
@patch("superset.commands.report_execute.ExecuteReportScheduleCommand.run")
@patch("superset.tasks.scheduler.execute.update_state")
@patch("superset.events.logger")
def test_execute_task_with_command_exception(
    logger_mock,
    update_state_mock,
    command_mock,
    init_mock,
    sync_session: Session,
    owners,
):
    from superset.exceptions import CommandException

    report_schedule = _insert_report_schedule(
        sync_session,
        crontab="0 4 * * *",
        timezone="America/New_York",
        owners=owners,
        name=f"report-{randint(0, 1000)}",  # noqa: S311
    )
    init_mock.return_value = None
    command_mock.side_effect = CommandException("Unexpected error")
    try:
        with freeze_time("2020-01-01T09:00:00Z"):
            execute(report_schedule.id)
            update_state_mock.assert_called_with(state="FAILURE")
            logger_mock.exception.assert_called_with(
                "A downstream exception occurred while generating a report: None. Unexpected error",  # noqa: E501
                exc_info=True,
            )
    finally:
        sync_session.delete(report_schedule)
        sync_session.commit()
