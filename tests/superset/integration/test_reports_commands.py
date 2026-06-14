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
"""Flask-free port of ``tests/integration_tests/reports/commands_tests.py``.

Drives the real synchronous report-execution command
(:class:`superset.commands.report_execute.ExecuteReportScheduleCommand` — the
port of the upstream ``AsyncExecuteReportScheduleCommand``) against the seeded
Postgres backend. Screenshots, SMTP, Slack and the CSV/dataframe HTTP fetch are
mocked; the state machine, executor resolution, notification content, grace
period and logging logic run for real.

Behaviour-preserving adaptations (all faithful 1:1 in intent):

* ``AsyncExecuteReportScheduleCommand(task, id, dttm)`` →
  ``ExecuteReportScheduleCommand(task, id, dttm, session)`` (the port runs
  synchronously inside a Celery worker and takes the sync ``Session`` as a 4th
  arg). Report rows are built on the synchronous Celery session
  (:func:`superset.db.session.get_sync_session`) and cleaned up afterwards.
* Config is no longer read from ``app.config``. Per-test knobs
  (``ALERT_REPORTS_NOTIFICATION_DRY_RUN``, ``ALERT_REPORTS_EXECUTORS``,
  ``EMAIL_REPORTS_CTA``, the ``ALERT_REPORT_TABS`` / ``ALERTS_ATTACH_REPORTS``
  feature flags) are injected by patching
  ``superset.commands.report_execute._get_settings`` to return a freshly built
  :class:`~superset.config.SupersetSettings` with those fields overridden.
* Module paths differ: ``superset.commands.report.execute`` →
  ``superset.commands.report_execute``; the email send helper is the port's
  ``superset.reports.notifications.email._send_email_smtp`` (same positional /
  kwargs layout as the upstream ``send_email_smtp``); the Slack helpers live on
  ``superset.reports.notifications.slack`` (``_get_slack_client`` /
  ``_should_use_v2_api`` — the port merged ``slackv2`` into ``slack``); the
  channel-id lookup used by ``update_report_schedule_slack_v2`` is
  ``superset.controllers.report._get_slack_channels``.
* ``ReportDataFormat`` enum members were renamed in the port:
  upstream ``CSV`` → ``DATA``, upstream ``PNG`` → ``VISUALIZATION`` (values are
  unchanged: ``"CSV"`` / ``"PNG"``).
* The port's ``get_url_path`` percent-encodes the ``:`` inside the JSON
  ``form_data`` query param as ``%3A`` (``urllib.parse.urlencode``) whereas the
  upstream ``url_for`` left it literal. The link is otherwise identical; the
  expected substrings below carry ``%3A`` to match the real port output.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from unittest.mock import call, Mock, patch
from uuid import uuid4

import pytest
from freezegun import freeze_time
from slack_sdk.errors import (
    BotUserAccessError,
    SlackApiError,
    SlackClientConfigurationError,
    SlackClientError,
    SlackClientNotConnectedError,
    SlackObjectFormationError,
    SlackRequestError,
    SlackTokenRotationError,
)
from sqlalchemy.orm import Session
from sqlalchemy.sql import func

from superset.commands.report_exceptions import (
    AlertQueryError,
    AlertQueryInvalidTypeError,
    AlertQueryMultipleColumnsError,
    AlertQueryMultipleRowsError,
    ReportScheduleClientErrorsException,
    ReportScheduleCsvFailedError,
    ReportScheduleCsvTimeout,
    ReportScheduleNotFoundError,
    ReportSchedulePreviousWorkingError,
    ReportScheduleScreenshotFailedError,
    ReportScheduleScreenshotTimeout,
    ReportScheduleSystemErrorsException,
    ReportScheduleWorkingTimeoutError,
)
from superset.commands.report_execute import (
    BaseReportState,
    ExecuteReportScheduleCommand,
)
from superset.commands.report_log_prune import PruneReportScheduleLogCommand
from superset.config import SupersetSettings
from superset.db.session import get_sync_session
from superset.exceptions import SupersetException
from superset.models.core import Database
from superset.models.dashboard import Dashboard
from superset.models.key_value import KeyValueEntry
from superset.models.reports import (
    ReportDataFormat,
    ReportExecutionLog,
    ReportRecipients,
    ReportRecipientType,
    ReportSchedule,
    ReportScheduleType,
    ReportScheduleValidatorType,
    ReportState,
)
from superset.models.security import User
from superset.models.slice import Slice
from superset.reports.notifications.exceptions import (
    NotificationError,
    NotificationParamException,
)
from superset.tasks.types import ExecutorType
from superset.utils import json
from superset.utils.database import get_example_database
from tests.superset.integration.fixtures import read_fixture

TEST_ID = str(uuid4())
CSV_FILE = read_fixture("trends.csv")
SCREENSHOT_FILE = read_fixture("sample.png")
DEFAULT_OWNER_EMAIL = "admin@fab.org"


# ---------------------------------------------------------------------------
# Settings injection helpers (replace app.config mutation)
# ---------------------------------------------------------------------------
def _settings(**overrides: Any) -> SupersetSettings:
    """Build a real settings instance with selected fields overridden."""
    settings = SupersetSettings()  # type: ignore[call-arg]
    feature_overrides = overrides.pop("feature_flags", None)
    if feature_overrides is not None:
        settings.feature_flags = {**settings.feature_flags, **feature_overrides}
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


def _patch_settings(**overrides: Any):
    """Patch the execute command's settings accessor for the test body."""
    return patch(
        "superset.commands.report_execute._get_settings",
        return_value=_settings(**overrides),
    )


def _template_processing_on():
    """Enable ``ENABLE_TEMPLATE_PROCESSING`` for alert SQL Jinja rendering.

    The upstream integration test app config enables template processing, so
    ``SELECT {{ 5 + 5 }}`` renders to ``SELECT 10``. The port's default test
    env leaves the feature flag off (``NoOpTemplateProcessor``), so the flag is
    flipped on the feature-flag manager that ``get_template_processor`` reads.
    """
    ff = Mock()
    ff.is_feature_enabled.side_effect = (
        lambda name: name == "ENABLE_TEMPLATE_PROCESSING"
    )
    return patch("superset.jinja_context.feature_flag_manager", ff)


# ---------------------------------------------------------------------------
# Data builders (sync Celery session)
# ---------------------------------------------------------------------------
def _get_owner(session: Session) -> User:
    """Get-or-create the default report owner (``admin@fab.org``)."""
    owner = (
        session.query(User).filter(User.email == DEFAULT_OWNER_EMAIL).one_or_none()
    )
    if owner is None:
        owner = User(
            username="admin",
            first_name="admin",
            last_name="user",
            email=DEFAULT_OWNER_EMAIL,
        )
        session.add(owner)
        session.flush()
    return owner


def _get_named_user(session: Session, username: str) -> User:
    user = session.query(User).filter(User.username == username).one_or_none()
    if user is None:
        user = User(
            username=username,
            first_name=username,
            last_name="user",
            email=f"{username}@example.com",
        )
        session.add(user)
        session.flush()
    return user


def _first_chart(session: Session) -> Slice:
    return session.query(Slice).order_by(Slice.id).first()


def _first_dashboard(session: Session) -> Dashboard:
    return session.query(Dashboard).order_by(Dashboard.id).first()


def create_report_notification(
    session: Session,
    *,
    email_target: Optional[str] = None,
    slack_channel: Optional[str] = None,
    chart: Optional[Slice] = None,
    dashboard: Optional[Dashboard] = None,
    database: Optional[Database] = None,
    sql: Optional[str] = None,
    report_type: ReportScheduleType = ReportScheduleType.REPORT,
    validator_type: Optional[str] = None,
    validator_config_json: Optional[str] = None,
    grace_period: Optional[int] = None,
    report_format: Optional[ReportDataFormat] = None,
    name: Optional[str] = None,
    extra: Optional[dict[str, Any]] = None,
    force_screenshot: bool = False,
    owners: Optional[list[User]] = None,
    ccTarget: Optional[str] = None,  # noqa: N803
    bccTarget: Optional[str] = None,  # noqa: N803
    use_slack_v2: bool = False,
) -> ReportSchedule:
    """1:1 port of ``tests/integration_tests/reports/utils.create_report_notification``
    against the sync Celery session."""
    if not owners:
        owners = [_get_owner(session)]

    if slack_channel:
        rtype = (
            ReportRecipientType.SLACKV2 if use_slack_v2 else ReportRecipientType.SLACK
        )
        recipient = ReportRecipients(
            type=rtype,
            recipient_config_json=json.dumps({"target": slack_channel}),
        )
    else:
        recipient = ReportRecipients(
            type=ReportRecipientType.EMAIL,
            recipient_config_json=json.dumps(
                {"target": email_target, "ccTarget": ccTarget, "bccTarget": bccTarget}
            ),
        )

    if name is None:
        name = "report_with_csv" if report_format else "report"

    report_schedule = ReportSchedule(
        type=report_type,
        name=name,
        crontab="0 9 * * *",
        description="Daily report",
        sql=sql,
        chart=chart,
        dashboard=dashboard,
        database=database,
        owners=owners,
        validator_type=validator_type,
        validator_config_json=validator_config_json,
        grace_period=grace_period,
        recipients=[recipient],
        last_state=ReportState.NOOP,
        report_format=report_format or ReportDataFormat.VISUALIZATION,
        extra=extra,
        force_screenshot=force_screenshot,
    )
    session.add(report_schedule)
    session.commit()
    return report_schedule


def cleanup_report_schedule(
    session: Session, report_schedule: Optional[ReportSchedule] = None
) -> None:
    if report_schedule:
        session.query(ReportExecutionLog).filter(
            ReportExecutionLog.report_schedule == report_schedule
        ).delete()
        session.query(ReportRecipients).filter(
            ReportRecipients.report_schedule == report_schedule
        ).delete()
        session.delete(report_schedule)
    session.commit()


def reset_key_values(session: Session) -> None:
    session.query(KeyValueEntry).delete()
    session.commit()


# ---------------------------------------------------------------------------
# Log assertion helpers
# ---------------------------------------------------------------------------
def get_target_from_report_schedule(report_schedule: ReportSchedule) -> list[str]:
    return [
        json.loads(recipient.recipient_config_json)["target"]
        for recipient in report_schedule.recipients
    ]


def get_cctarget_from_report_schedule(report_schedule: ReportSchedule) -> list[str]:
    return [
        json.loads(recipient.recipient_config_json).get("ccTarget", "")
        for recipient in report_schedule.recipients
    ]


def get_bcctarget_from_report_schedule(report_schedule: ReportSchedule) -> list[str]:
    return [
        json.loads(recipient.recipient_config_json).get("bccTarget", "")
        for recipient in report_schedule.recipients
    ]


def get_error_logs_query(session: Session, report_schedule: ReportSchedule):
    return (
        session.query(ReportExecutionLog)
        .filter(
            ReportExecutionLog.report_schedule == report_schedule,
            ReportExecutionLog.state == ReportState.ERROR,
        )
        .order_by(ReportExecutionLog.end_dttm.desc())
    )


def get_notification_error_sent_count(
    session: Session, report_schedule: ReportSchedule
) -> int:
    logs = get_error_logs_query(session, report_schedule).all()
    notification_sent_logs = [
        log.error_message
        for log in logs
        if log.error_message == "Notification sent with error"
    ]
    return len(notification_sent_logs)


def assert_log(
    session: Session,
    report_schedule: ReportSchedule,
    state: str,
    error_message: Optional[str] = None,
) -> None:
    """Port of the upstream ``assert_log`` scoped to a single report schedule.

    Upstream counts every ``ReportExecutionLog`` in the (per-test rolled-back)
    DB; here the seeded backend may carry rows from other fixtures, so the
    count is scoped to this report schedule — equivalent in intent.
    """
    session.commit()
    logs = (
        session.query(ReportExecutionLog)
        .filter(ReportExecutionLog.report_schedule == report_schedule)
        .all()
    )

    if state == ReportState.ERROR:
        # On error we send an email
        assert len(logs) == 3
    else:
        assert len(logs) == 2
    log_states = [log.state for log in logs]
    assert ReportState.WORKING in log_states
    assert state in log_states
    assert error_message in [log.error_message for log in logs]

    for log in logs:
        if log.state == ReportState.WORKING:
            assert log.value is None
            assert log.value_row_json is None


# ---------------------------------------------------------------------------
# Test-table context (alert SQL fixtures)
# ---------------------------------------------------------------------------
class _TestTableContext:
    def __init__(self, database: Database) -> None:
        self._database = database

    def __enter__(self) -> None:
        from sqlalchemy import text

        with self._database.get_sqla_engine() as engine:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "CREATE TABLE IF NOT EXISTS test_table "
                        "AS SELECT 1 as first, 2 as second"
                    )
                )
                conn.execute(
                    text("INSERT INTO test_table (first, second) VALUES (1, 2)")
                )
                conn.execute(
                    text("INSERT INTO test_table (first, second) VALUES (3, 4)")
                )

    def __exit__(self, *exc: Any) -> None:
        from sqlalchemy import text

        with self._database.get_sqla_engine() as engine:
            with engine.begin() as conn:
                conn.execute(text("DROP TABLE test_table"))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _machine_auth_cookies():
    """Supply machine-auth cookies for the CSV/dataframe HTTP fetch path.

    Upstream ran inside a Flask app where ``machine_auth_provider_factory`` was
    booted, so ``_get_auth_cookies`` returned a non-empty cookie dict and the
    real ``get_chart_csv_data`` / ``get_chart_dataframe`` fetched via the mocked
    ``urlopen``. The bare port harness does not boot the provider, so the cookie
    dict is injected here (no behaviour change for the screenshot-only tests).
    """
    with patch.object(
        BaseReportState, "_get_auth_cookies", return_value={"session": "cookie"}
    ):
        yield


@pytest.fixture
def sync_session():
    session = get_sync_session()
    try:
        yield session
    finally:
        session.rollback()


@pytest.fixture
def create_report_email_chart(sync_session):
    chart = _first_chart(sync_session)
    report_schedule = create_report_notification(
        sync_session, email_target="target@email.com", chart=chart
    )
    yield report_schedule
    cleanup_report_schedule(sync_session, report_schedule)


@pytest.fixture
def create_report_email_chart_with_cc_and_bcc(sync_session):
    chart = _first_chart(sync_session)
    report_schedule = create_report_notification(
        sync_session,
        email_target="target@email.com",
        ccTarget="cc@email.com",
        bccTarget="bcc@email.com",
        chart=chart,
    )
    yield report_schedule
    cleanup_report_schedule(sync_session, report_schedule)


@pytest.fixture
def create_report_email_chart_alpha_owner(sync_session):
    owners = [_get_named_user(sync_session, "alpha")]
    chart = _first_chart(sync_session)
    report_schedule = create_report_notification(
        sync_session, email_target="target@email.com", chart=chart, owners=owners
    )
    yield report_schedule
    cleanup_report_schedule(sync_session, report_schedule)


@pytest.fixture
def create_report_email_chart_force_screenshot(sync_session):
    chart = _first_chart(sync_session)
    report_schedule = create_report_notification(
        sync_session,
        email_target="target@email.com",
        chart=chart,
        force_screenshot=True,
    )
    yield report_schedule
    cleanup_report_schedule(sync_session, report_schedule)


@pytest.fixture
def create_report_email_chart_with_csv(sync_session):
    chart = _first_chart(sync_session)
    chart.query_context = '{"mock": "query_context"}'
    sync_session.commit()
    report_schedule = create_report_notification(
        sync_session,
        email_target="target@email.com",
        chart=chart,
        report_format=ReportDataFormat.DATA,
    )
    yield report_schedule
    cleanup_report_schedule(sync_session, report_schedule)


@pytest.fixture
def create_report_email_chart_with_text(sync_session):
    chart = _first_chart(sync_session)
    chart.query_context = '{"mock": "query_context"}'
    sync_session.commit()
    report_schedule = create_report_notification(
        sync_session,
        email_target="target@email.com",
        chart=chart,
        report_format=ReportDataFormat.TEXT,
    )
    yield report_schedule
    cleanup_report_schedule(sync_session, report_schedule)


@pytest.fixture
def create_report_email_chart_with_csv_no_query_context(sync_session):
    chart = _first_chart(sync_session)
    chart.query_context = None
    sync_session.commit()
    report_schedule = create_report_notification(
        sync_session,
        email_target="target@email.com",
        chart=chart,
        report_format=ReportDataFormat.DATA,
        name="report_csv_no_query_context",
    )
    yield report_schedule
    cleanup_report_schedule(sync_session, report_schedule)


@pytest.fixture
def create_report_email_dashboard(sync_session):
    dashboard = _first_dashboard(sync_session)
    report_schedule = create_report_notification(
        sync_session, email_target="target@email.com", dashboard=dashboard
    )
    yield report_schedule
    cleanup_report_schedule(sync_session, report_schedule)


@pytest.fixture
def create_report_email_dashboard_force_screenshot(sync_session):
    dashboard = _first_dashboard(sync_session)
    report_schedule = create_report_notification(
        sync_session,
        email_target="target@email.com",
        dashboard=dashboard,
        force_screenshot=True,
    )
    yield report_schedule
    cleanup_report_schedule(sync_session, report_schedule)


@pytest.fixture
def create_report_slack_chart(sync_session):
    chart = _first_chart(sync_session)
    report_schedule = create_report_notification(
        sync_session, slack_channel="slack_channel", chart=chart
    )
    yield report_schedule
    cleanup_report_schedule(sync_session, report_schedule)


@pytest.fixture
def create_report_slack_chartv2(sync_session):
    chart = _first_chart(sync_session)
    report_schedule = create_report_notification(
        sync_session,
        slack_channel="slack_channel_id",
        chart=chart,
        name="report_slack_chartv2",
        use_slack_v2=True,
    )
    yield report_schedule
    cleanup_report_schedule(sync_session, report_schedule)


@pytest.fixture
def create_report_slack_chart_with_csv(sync_session):
    chart = _first_chart(sync_session)
    chart.query_context = '{"mock": "query_context"}'
    sync_session.commit()
    report_schedule = create_report_notification(
        sync_session,
        slack_channel="slack_channel",
        chart=chart,
        report_format=ReportDataFormat.DATA,
    )
    yield report_schedule
    cleanup_report_schedule(sync_session, report_schedule)


@pytest.fixture
def create_report_slack_chart_with_text(sync_session):
    chart = _first_chart(sync_session)
    chart.query_context = '{"mock": "query_context"}'
    sync_session.commit()
    report_schedule = create_report_notification(
        sync_session,
        slack_channel="slack_channel",
        chart=chart,
        report_format=ReportDataFormat.TEXT,
    )
    yield report_schedule
    cleanup_report_schedule(sync_session, report_schedule)


@pytest.fixture
def create_report_slack_chart_working(sync_session):
    chart = _first_chart(sync_session)
    report_schedule = create_report_notification(
        sync_session, slack_channel="slack_channel", chart=chart
    )
    report_schedule.last_state = ReportState.WORKING
    report_schedule.last_eval_dttm = datetime(2020, 1, 1, 0, 0)
    report_schedule.last_value = None
    report_schedule.last_value_row_json = None
    sync_session.commit()
    log = ReportExecutionLog(
        scheduled_dttm=report_schedule.last_eval_dttm,
        start_dttm=report_schedule.last_eval_dttm,
        end_dttm=report_schedule.last_eval_dttm,
        value=report_schedule.last_value,
        value_row_json=report_schedule.last_value_row_json,
        state=ReportState.WORKING,
        report_schedule=report_schedule,
        uuid=uuid4(),
    )
    sync_session.add(log)
    sync_session.commit()
    yield report_schedule
    cleanup_report_schedule(sync_session, report_schedule)


@pytest.fixture
def create_alert_slack_chart_success(sync_session):
    chart = _first_chart(sync_session)
    report_schedule = create_report_notification(
        sync_session,
        slack_channel="slack_channel",
        chart=chart,
        report_type=ReportScheduleType.ALERT,
    )
    report_schedule.last_state = ReportState.SUCCESS
    report_schedule.last_eval_dttm = datetime(2020, 1, 1, 0, 0)
    log = ReportExecutionLog(
        report_schedule=report_schedule,
        state=ReportState.SUCCESS,
        start_dttm=report_schedule.last_eval_dttm,
        end_dttm=report_schedule.last_eval_dttm,
        scheduled_dttm=report_schedule.last_eval_dttm,
    )
    sync_session.add(log)
    sync_session.commit()
    yield report_schedule
    cleanup_report_schedule(sync_session, report_schedule)


@pytest.fixture(params=["alert1"])
def create_alert_slack_chart_grace(request, sync_session):
    param_config = {
        "alert1": {
            "sql": "SELECT count(*) from test_table",
            "validator_type": ReportScheduleValidatorType.OPERATOR,
            "validator_config_json": '{"op": "<", "threshold": 10}',
        },
    }
    chart = _first_chart(sync_session)
    example_database = get_example_database()
    with _TestTableContext(example_database):
        report_schedule = create_report_notification(
            sync_session,
            slack_channel="slack_channel",
            chart=chart,
            report_type=ReportScheduleType.ALERT,
            database=example_database,
            sql=param_config[request.param]["sql"],
            validator_type=param_config[request.param]["validator_type"],
            validator_config_json=param_config[request.param]["validator_config_json"],
        )
        report_schedule.last_state = ReportState.GRACE
        report_schedule.last_eval_dttm = datetime(2020, 1, 1, 0, 0)
        log = ReportExecutionLog(
            report_schedule=report_schedule,
            state=ReportState.SUCCESS,
            start_dttm=report_schedule.last_eval_dttm,
            end_dttm=report_schedule.last_eval_dttm,
            scheduled_dttm=report_schedule.last_eval_dttm,
        )
        sync_session.add(log)
        sync_session.commit()
        yield report_schedule
        cleanup_report_schedule(sync_session, report_schedule)


@pytest.fixture(
    params=[
        "alert1",
        "alert2",
        "alert3",
        "alert4",
        "alert5",
        "alert6",
        "alert7",
        "alert8",
    ]
)
def create_alert_email_chart(request, sync_session):
    param_config = {
        "alert1": {
            "sql": "SELECT 10 as metric",
            "validator_type": ReportScheduleValidatorType.OPERATOR,
            "validator_config_json": '{"op": ">", "threshold": 9}',
        },
        "alert2": {
            "sql": "SELECT 10 as metric",
            "validator_type": ReportScheduleValidatorType.OPERATOR,
            "validator_config_json": '{"op": ">=", "threshold": 10}',
        },
        "alert3": {
            "sql": "SELECT 10 as metric",
            "validator_type": ReportScheduleValidatorType.OPERATOR,
            "validator_config_json": '{"op": "<", "threshold": 11}',
        },
        "alert4": {
            "sql": "SELECT 10 as metric",
            "validator_type": ReportScheduleValidatorType.OPERATOR,
            "validator_config_json": '{"op": "<=", "threshold": 10}',
        },
        "alert5": {
            "sql": "SELECT 10 as metric",
            "validator_type": ReportScheduleValidatorType.OPERATOR,
            "validator_config_json": '{"op": "!=", "threshold": 11}',
        },
        "alert6": {
            "sql": "SELECT 'something' as metric",
            "validator_type": ReportScheduleValidatorType.NOT_NULL,
            "validator_config_json": "{}",
        },
        "alert7": {
            "sql": "SELECT {{ 5 + 5 }} as metric",
            "validator_type": ReportScheduleValidatorType.OPERATOR,
            "validator_config_json": '{"op": "!=", "threshold": 11}',
        },
        "alert8": {
            "sql": "SELECT 55 as metric",
            "validator_type": ReportScheduleValidatorType.OPERATOR,
            "validator_config_json": '{"op": ">", "threshold": 54.999}',
        },
    }
    chart = _first_chart(sync_session)
    example_database = get_example_database()
    with _TestTableContext(example_database):
        report_schedule = create_report_notification(
            sync_session,
            email_target="target@email.com",
            chart=chart,
            report_type=ReportScheduleType.ALERT,
            database=example_database,
            sql=param_config[request.param]["sql"],
            validator_type=param_config[request.param]["validator_type"],
            validator_config_json=param_config[request.param]["validator_config_json"],
            force_screenshot=True,
        )
        with _template_processing_on():
            yield report_schedule
        cleanup_report_schedule(sync_session, report_schedule)


@pytest.fixture(
    params=[
        "alert1",
        "alert2",
        "alert3",
        "alert4",
        "alert5",
        "alert6",
        "alert7",
        "alert8",
        "alert9",
    ]
)
def create_no_alert_email_chart(request, sync_session):
    param_config = {
        "alert1": {
            "sql": "SELECT 10 as metric",
            "validator_type": ReportScheduleValidatorType.OPERATOR,
            "validator_config_json": '{"op": "<", "threshold": 10}',
        },
        "alert2": {
            "sql": "SELECT 10 as metric",
            "validator_type": ReportScheduleValidatorType.OPERATOR,
            "validator_config_json": '{"op": ">=", "threshold": 11}',
        },
        "alert3": {
            "sql": "SELECT 10 as metric",
            "validator_type": ReportScheduleValidatorType.OPERATOR,
            "validator_config_json": '{"op": "<", "threshold": 10}',
        },
        "alert4": {
            "sql": "SELECT 10 as metric",
            "validator_type": ReportScheduleValidatorType.OPERATOR,
            "validator_config_json": '{"op": "<=", "threshold": 9}',
        },
        "alert5": {
            "sql": "SELECT 10 as metric",
            "validator_type": ReportScheduleValidatorType.OPERATOR,
            "validator_config_json": '{"op": "!=", "threshold": 10}',
        },
        "alert6": {
            "sql": "SELECT first from test_table where 1=0",
            "validator_type": ReportScheduleValidatorType.NOT_NULL,
            "validator_config_json": "{}",
        },
        "alert7": {
            "sql": "SELECT first from test_table where 1=0",
            "validator_type": ReportScheduleValidatorType.OPERATOR,
            "validator_config_json": '{"op": ">", "threshold": 0}',
        },
        "alert8": {
            "sql": "SELECT Null as metric",
            "validator_type": ReportScheduleValidatorType.NOT_NULL,
            "validator_config_json": "{}",
        },
        "alert9": {
            "sql": "SELECT Null as metric",
            "validator_type": ReportScheduleValidatorType.OPERATOR,
            "validator_config_json": '{"op": ">", "threshold": 0}',
        },
    }
    chart = _first_chart(sync_session)
    example_database = get_example_database()
    with _TestTableContext(example_database):
        report_schedule = create_report_notification(
            sync_session,
            email_target="target@email.com",
            chart=chart,
            report_type=ReportScheduleType.ALERT,
            database=example_database,
            sql=param_config[request.param]["sql"],
            validator_type=param_config[request.param]["validator_type"],
            validator_config_json=param_config[request.param]["validator_config_json"],
        )
        yield report_schedule
        cleanup_report_schedule(sync_session, report_schedule)


@pytest.fixture(params=["alert1", "alert2"])
def create_mul_alert_email_chart(request, sync_session):
    param_config = {
        "alert1": {
            "sql": "SELECT first, second from test_table",
            "validator_type": ReportScheduleValidatorType.OPERATOR,
            "validator_config_json": '{"op": "<", "threshold": 10}',
        },
        "alert2": {
            "sql": "SELECT first from test_table",
            "validator_type": ReportScheduleValidatorType.OPERATOR,
            "validator_config_json": '{"op": "<", "threshold": 10}',
        },
    }
    chart = _first_chart(sync_session)
    example_database = get_example_database()
    with _TestTableContext(example_database):
        report_schedule = create_report_notification(
            sync_session,
            email_target="target@email.com",
            chart=chart,
            report_type=ReportScheduleType.ALERT,
            database=example_database,
            sql=param_config[request.param]["sql"],
            validator_type=param_config[request.param]["validator_type"],
            validator_config_json=param_config[request.param]["validator_config_json"],
        )
        yield report_schedule
        cleanup_report_schedule(sync_session, report_schedule)


@pytest.fixture(params=["alert1", "alert2"])
def create_invalid_sql_alert_email_chart(request, sync_session):
    param_config = {
        "alert1": {
            "sql": "SELECT 'string' ",
            "validator_type": ReportScheduleValidatorType.OPERATOR,
            "validator_config_json": '{"op": "<", "threshold": 10}',
        },
        "alert2": {
            "sql": "SELECT first from foo_table",
            "validator_type": ReportScheduleValidatorType.OPERATOR,
            "validator_config_json": '{"op": "<", "threshold": 10}',
        },
    }
    chart = _first_chart(sync_session)
    example_database = get_example_database()
    with _TestTableContext(example_database):
        report_schedule = create_report_notification(
            sync_session,
            email_target="target@email.com",
            chart=chart,
            report_type=ReportScheduleType.ALERT,
            database=example_database,
            sql=param_config[request.param]["sql"],
            validator_type=param_config[request.param]["validator_type"],
            validator_config_json=param_config[request.param]["validator_config_json"],
            grace_period=60 * 60,
        )
        yield report_schedule
        cleanup_report_schedule(sync_session, report_schedule)


def _run(report_id: int, session: Session, dttm: Optional[datetime] = None) -> None:
    ExecuteReportScheduleCommand(
        TEST_ID, report_id, dttm or datetime.utcnow(), session
    ).run()


# ===========================================================================
# Email chart/dashboard report tests
# ===========================================================================
@pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
@patch("superset.reports.notifications.email._send_email_smtp")
@patch("superset.commands.report_execute.ChartScreenshot.get_screenshot")
def test_email_chart_report_schedule_with_cc_bcc(
    screenshot_mock,
    email_mock,
    sync_session,
    create_report_email_chart_with_cc_and_bcc,
):
    """ExecuteReport Command: chart email report with screenshot and cc/bcc."""
    screenshot_mock.return_value = SCREENSHOT_FILE
    report = create_report_email_chart_with_cc_and_bcc

    with freeze_time("2020-01-01T00:00:00Z"):
        with _patch_settings():
            _run(report.id, sync_session)

        notification_targets = get_target_from_report_schedule(report)
        notification_cctargets = get_cctarget_from_report_schedule(report)
        notification_bcctargets = get_bcctarget_from_report_schedule(report)

        assert (
            '<a href="http://0.0.0.0:8080/explore/?form_data=%7B%22slice_id%22%3A+'
            f"{report.chart.id}"
            '%7D&force=false">Explore in Superset</a>' in email_mock.call_args[0][2]
        )
        if notification_targets:
            assert email_mock.call_args[0][0] == notification_targets[0]
        if notification_cctargets:
            expected_cc_targets = [t.strip() for t in notification_cctargets]
            assert (
                email_mock.call_args[1].get("cc", "").split(",") == expected_cc_targets
            )
        if notification_bcctargets:
            expected_bcc_targets = [t.strip() for t in notification_bcctargets]
            assert (
                email_mock.call_args[1].get("bcc", "").split(",")
                == expected_bcc_targets
            )
        smtp_images = email_mock.call_args[1]["images"]
        assert smtp_images[list(smtp_images.keys())[0]] == SCREENSHOT_FILE
        assert_log(sync_session, report, ReportState.SUCCESS)


@pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
@patch("superset.reports.notifications.email._send_email_smtp")
@patch("superset.commands.report_execute.ChartScreenshot.get_screenshot")
def test_email_chart_report_schedule(
    screenshot_mock,
    email_mock,
    sync_session,
    create_report_email_chart,
):
    """ExecuteReport Command: chart email report with screenshot."""
    screenshot_mock.return_value = SCREENSHOT_FILE
    report = create_report_email_chart

    with freeze_time("2020-01-01T00:00:00Z"):
        with _patch_settings():
            _run(report.id, sync_session)

        notification_targets = get_target_from_report_schedule(report)
        assert (
            '<a href="http://0.0.0.0:8080/explore/?form_data=%7B%22slice_id%22%3A+'
            f"{report.chart.id}"
            '%7D&force=false">Explore in Superset</a>' in email_mock.call_args[0][2]
        )
        assert email_mock.call_args[0][0] == notification_targets[0]
        smtp_images = email_mock.call_args[1]["images"]
        assert smtp_images[list(smtp_images.keys())[0]] == SCREENSHOT_FILE
        assert_log(sync_session, report, ReportState.SUCCESS)


@pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
@patch("superset.reports.notifications.email._send_email_smtp")
@patch("superset.commands.report_execute.ChartScreenshot.get_screenshot")
def test_email_chart_report_schedule_alpha_owner(
    screenshot_mock,
    email_mock,
    sync_session,
    create_report_email_chart_alpha_owner,
):
    """ExecuteReport Command: chart email report executed as the chart owner."""
    report = create_report_email_chart_alpha_owner
    username = ""

    def _screenshot_side_effect(user) -> Optional[bytes]:
        nonlocal username
        username = user.username
        return SCREENSHOT_FILE

    screenshot_mock.side_effect = _screenshot_side_effect

    with freeze_time("2020-01-01T00:00:00Z"):
        with _patch_settings(alert_reports_executors=[ExecutorType.OWNER]):
            _run(report.id, sync_session)

        notification_targets = get_target_from_report_schedule(report)
        assert username == "alpha"
        assert (
            '<a href="http://0.0.0.0:8080/explore/?form_data=%7B%22slice_id%22%3A+'
            f"{report.chart.id}"
            '%7D&force=false">Explore in Superset</a>' in email_mock.call_args[0][2]
        )
        assert email_mock.call_args[0][0] == notification_targets[0]
        smtp_images = email_mock.call_args[1]["images"]
        assert smtp_images[list(smtp_images.keys())[0]] == SCREENSHOT_FILE
        assert_log(sync_session, report, ReportState.SUCCESS)


@pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
@patch("superset.reports.notifications.email._send_email_smtp")
@patch("superset.commands.report_execute.ChartScreenshot.get_screenshot")
def test_email_chart_report_schedule_force_screenshot(
    screenshot_mock,
    email_mock,
    sync_session,
    create_report_email_chart_force_screenshot,
):
    """ExecuteReport Command: chart email report with force_screenshot true."""
    screenshot_mock.return_value = SCREENSHOT_FILE
    report = create_report_email_chart_force_screenshot

    with freeze_time("2020-01-01T00:00:00Z"):
        with _patch_settings():
            _run(report.id, sync_session)

        notification_targets = get_target_from_report_schedule(report)
        assert (
            '<a href="http://0.0.0.0:8080/explore/?form_data=%7B%22slice_id%22%3A+'
            f"{report.chart.id}"
            '%7D&force=true">Explore in Superset</a>' in email_mock.call_args[0][2]
        )
        assert email_mock.call_args[0][0] == notification_targets[0]
        smtp_images = email_mock.call_args[1]["images"]
        assert smtp_images[list(smtp_images.keys())[0]] == SCREENSHOT_FILE
        assert_log(sync_session, report, ReportState.SUCCESS)


@pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
@patch("superset.reports.notifications.email._send_email_smtp")
@patch("superset.commands.report_execute.ChartScreenshot.get_screenshot")
def test_email_chart_alert_schedule(
    screenshot_mock,
    email_mock,
    sync_session,
    create_alert_email_chart,
):
    """ExecuteReport Command: chart email alert with screenshot."""
    screenshot_mock.return_value = SCREENSHOT_FILE
    report = create_alert_email_chart

    with freeze_time("2020-01-01T00:00:00Z"):
        with _patch_settings():
            _run(report.id, sync_session)

        notification_targets = get_target_from_report_schedule(report)
        assert (
            '<a href="http://0.0.0.0:8080/explore/?form_data=%7B%22slice_id%22%3A+'
            f"{report.chart.id}"
            '%7D&force=true">Explore in Superset</a>' in email_mock.call_args[0][2]
        )
        assert email_mock.call_args[0][0] == notification_targets[0]
        smtp_images = email_mock.call_args[1]["images"]
        assert smtp_images[list(smtp_images.keys())[0]] == SCREENSHOT_FILE
        assert_log(sync_session, report, ReportState.SUCCESS)


@pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
@patch("superset.reports.notifications.email._send_email_smtp")
@patch("superset.commands.report_execute.ChartScreenshot.get_screenshot")
def test_email_chart_report_dry_run(
    screenshot_mock,
    email_mock,
    sync_session,
    create_report_email_chart,
):
    """ExecuteReport Command: chart email report dry run (no email sent)."""
    screenshot_mock.return_value = SCREENSHOT_FILE
    report = create_report_email_chart

    with freeze_time("2020-01-01T00:00:00Z"):
        with _patch_settings(alert_reports_notification_dry_run=True):
            _run(report.id, sync_session)
        email_mock.assert_not_called()


@pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
@patch("superset.utils.csv.urllib.request.urlopen")
@patch("superset.utils.csv.urllib.request.OpenerDirector.open")
@patch("superset.reports.notifications.email._send_email_smtp")
@patch("superset.utils.csv.get_chart_csv_data")
def test_email_chart_report_schedule_with_csv(
    csv_mock,
    email_mock,
    mock_open,
    mock_urlopen,
    sync_session,
    create_report_email_chart_with_csv,
):
    """ExecuteReport Command: chart email report with CSV."""
    response = Mock()
    mock_open.return_value = response
    mock_urlopen.return_value = response
    mock_urlopen.return_value.getcode.return_value = 200
    response.read.return_value = CSV_FILE
    report = create_report_email_chart_with_csv

    with freeze_time("2020-01-01T00:00:00Z"):
        with _patch_settings():
            _run(report.id, sync_session)

        notification_targets = get_target_from_report_schedule(report)
        assert (
            '<a href="http://0.0.0.0:8080/explore/?form_data=%7B%22slice_id%22%3A+'
            f"{report.chart.id}%7D&"
            'force=false">Explore in Superset</a>' in email_mock.call_args[0][2]
        )
        assert email_mock.call_args[0][0] == notification_targets[0]
        smtp_images = email_mock.call_args[1]["data"]
        assert smtp_images[list(smtp_images.keys())[0]] == CSV_FILE
        assert_log(sync_session, report, ReportState.SUCCESS)


@pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
@patch("superset.utils.csv.urllib.request.urlopen")
@patch("superset.utils.csv.urllib.request.OpenerDirector.open")
@patch("superset.reports.notifications.email._send_email_smtp")
@patch("superset.utils.csv.get_chart_csv_data")
@patch("superset.commands.report_execute.ChartScreenshot.get_screenshot")
def test_email_chart_report_schedule_with_csv_no_query_context(
    screenshot_mock,
    csv_mock,
    email_mock,
    mock_open,
    mock_urlopen,
    sync_session,
    create_report_email_chart_with_csv_no_query_context,
):
    """ExecuteReport Command: chart email report with CSV (no query context)."""
    screenshot_mock.return_value = SCREENSHOT_FILE
    response = Mock()
    mock_open.return_value = response
    mock_urlopen.return_value = response
    mock_urlopen.return_value.getcode.return_value = 200
    response.read.return_value = CSV_FILE
    report = create_report_email_chart_with_csv_no_query_context

    with freeze_time("2020-01-01T00:00:00Z"):
        with _patch_settings():
            _run(report.id, sync_session)
        # When query context is null we request a screenshot to generate it.
        screenshot_mock.assert_called_once()


@pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
@patch("superset.utils.csv.urllib.request.urlopen")
@patch("superset.utils.csv.urllib.request.OpenerDirector.open")
@patch("superset.reports.notifications.email._send_email_smtp")
@patch("superset.utils.csv.get_chart_dataframe")
def test_email_chart_report_schedule_with_text(
    dataframe_mock,
    email_mock,
    mock_open,
    mock_urlopen,
    sync_session,
    create_report_email_chart_with_text,
):
    """ExecuteReport Command: chart email report with embedded text table."""
    response = Mock()
    mock_open.return_value = response
    mock_urlopen.return_value = response
    mock_urlopen.return_value.getcode.return_value = 200
    report = create_report_email_chart_with_text

    response.read.return_value = json.dumps(
        {
            "result": [
                {
                    "data": {
                        "t1": {0: "c11", 1: "c21"},
                        "t2": {0: "c12", 1: "c22"},
                        "t3__sum": {0: "c13", 1: "c23"},
                    },
                    "colnames": [("t1",), ("t2",), ("t3__sum",)],
                    "indexnames": [(0,), (1,)],
                    "coltypes": [1, 1],
                },
            ],
        }
    ).encode("utf-8")

    with freeze_time("2020-01-01T00:00:00Z"):
        with _patch_settings():
            _run(report.id, sync_session)

        table_html = """<table border="1" class="dataframe">
  <thead>
    <tr>
      <th></th>
      <th>t1</th>
      <th>t2</th>
      <th>t3__sum</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <th>0</th>
      <td>c11</td>
      <td>c12</td>
      <td>c13</td>
    </tr>
    <tr>
      <th>1</th>
      <td>c21</td>
      <td>c22</td>
      <td>c23</td>
    </tr>
  </tbody>
</table>"""
        assert table_html in email_mock.call_args[0][2]
        assert_log(sync_session, report, ReportState.SUCCESS)

    dt = datetime(2022, 1, 1).replace(tzinfo=timezone.utc)
    ts = datetime.timestamp(dt) * 1000
    response.read.return_value = json.dumps(
        {
            "result": [
                {
                    "data": {
                        "t1": {0: "c11", 1: "c21"},
                        "t2__date": {0: ts, 1: ts},
                        "t3__sum": {0: "c13", 1: "c23"},
                    },
                    "colnames": [("t1",), ("t2__date",), ("t3__sum",)],
                    "indexnames": [(0,), (1,)],
                    "coltypes": [1, 2],
                },
            ],
        }
    ).encode("utf-8")

    with freeze_time("2020-01-01T00:00:00Z"):
        with _patch_settings():
            _run(report.id, sync_session)

        table_html = """<table border="1" class="dataframe">
  <thead>
    <tr>
      <th></th>
      <th>t1</th>
      <th>t2__date</th>
      <th>t3__sum</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <th>0</th>
      <td>c11</td>
      <td>2022-01-01</td>
      <td>c13</td>
    </tr>
    <tr>
      <th>1</th>
      <td>c21</td>
      <td>2022-01-01</td>
      <td>c23</td>
    </tr>
  </tbody>
</table>"""
        assert table_html in email_mock.call_args[0][2]


@pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
@patch("superset.reports.notifications.email._send_email_smtp")
@patch("superset.commands.report_execute.DashboardScreenshot.get_screenshot")
def test_email_dashboard_report_schedule(
    screenshot_mock, email_mock, sync_session, create_report_email_dashboard
):
    """ExecuteReport Command: dashboard email report schedule."""
    screenshot_mock.return_value = SCREENSHOT_FILE
    report = create_report_email_dashboard

    with freeze_time("2020-01-01T00:00:00Z"):
        with _patch_settings():
            with patch(
                "superset.extensions.stats_logger_manager.instance.gauge"
            ) as statsd_mock:
                _run(report.id, sync_session)

                notification_targets = get_target_from_report_schedule(report)
                assert email_mock.call_args[0][0] == notification_targets[0]
                smtp_images = email_mock.call_args[1]["images"]
                assert smtp_images[list(smtp_images.keys())[0]] == SCREENSHOT_FILE
                assert_log(sync_session, report, ReportState.SUCCESS)
                statsd_mock.assert_called_once_with("reports.email.send.ok", 1)


@pytest.mark.usefixtures("tabbed_dashboard")
@patch("superset.commands.report_execute.DashboardScreenshot.get_screenshot")
@patch("superset.reports.notifications.email._send_email_smtp")
def test_email_dashboard_report_schedule_with_tab_anchor(
    _email_mock,  # noqa: PT019
    _screenshot_mock,  # noqa: PT019
    sync_session,
):
    """ExecuteReport Command: dashboard email report with tab metadata."""
    _screenshot_mock.return_value = SCREENSHOT_FILE
    dashboard = (
        sync_session.query(Dashboard)
        .filter(Dashboard.dashboard_title == "Tabbed Dashboard")
        .one()
    )
    report_schedule = create_report_notification(
        sync_session,
        email_target="target@email.com",
        dashboard=dashboard,
        extra={"dashboard": {"anchor": "TAB-L2AB"}},
    )
    with freeze_time("2020-01-01T00:00:00Z"):
        with _patch_settings(feature_flags={"ALERT_REPORT_TABS": True}):
            with patch(
                "superset.extensions.stats_logger_manager.instance.gauge"
            ) as statsd_mock:
                _run(report_schedule.id, sync_session)

                assert_log(sync_session, report_schedule, ReportState.SUCCESS)
                statsd_mock.assert_called_once_with("reports.email.send.ok", 1)

                pl = (
                    sync_session.query(KeyValueEntry)
                    .order_by(KeyValueEntry.id.desc())
                    .first()
                )
                value = json.loads(pl.value)
                assert report_schedule.extra["dashboard"] == value["state"]

    cleanup_report_schedule(sync_session, report_schedule)
    reset_key_values(sync_session)


@pytest.mark.usefixtures("tabbed_dashboard")
@patch("superset.commands.report_execute.DashboardScreenshot.get_screenshot")
@patch("superset.reports.notifications.email._send_email_smtp")
def test_email_dashboard_report_schedule_disabled_tabs(
    _email_mock,  # noqa: PT019
    _screenshot_mock,  # noqa: PT019
    sync_session,
):
    """ExecuteReport Command: dashboard email report with tabs disabled."""
    _screenshot_mock.return_value = SCREENSHOT_FILE
    dashboard = (
        sync_session.query(Dashboard)
        .filter(Dashboard.dashboard_title == "Tabbed Dashboard")
        .one()
    )
    report_schedule = create_report_notification(
        sync_session,
        email_target="target@email.com",
        dashboard=dashboard,
        extra={"dashboard": {"anchor": "TAB-L2AB"}},
    )
    reset_key_values(sync_session)
    with freeze_time("2020-01-01T00:00:00Z"):
        with _patch_settings(feature_flags={"ALERT_REPORT_TABS": False}):
            with patch(
                "superset.extensions.stats_logger_manager.instance.gauge"
            ) as statsd_mock:
                _run(report_schedule.id, sync_session)

                assert_log(sync_session, report_schedule, ReportState.SUCCESS)
                statsd_mock.assert_called_once_with("reports.email.send.ok", 1)

                permalinks = sync_session.query(KeyValueEntry).all()
                assert len(permalinks) == 0

    cleanup_report_schedule(sync_session, report_schedule)


@pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
@patch("superset.reports.notifications.email._send_email_smtp")
@patch("superset.commands.report_execute.DashboardScreenshot.get_screenshot")
def test_email_dashboard_report_schedule_force_screenshot(
    screenshot_mock,
    email_mock,
    sync_session,
    create_report_email_dashboard_force_screenshot,
):
    """ExecuteReport Command: dashboard email report with force_screenshot."""
    screenshot_mock.return_value = SCREENSHOT_FILE
    report = create_report_email_dashboard_force_screenshot

    with freeze_time("2020-01-01T00:00:00Z"):
        with _patch_settings():
            _run(report.id, sync_session)

        notification_targets = get_target_from_report_schedule(report)
        assert email_mock.call_args[0][0] == notification_targets[0]
        smtp_images = email_mock.call_args[1]["images"]
        assert smtp_images[list(smtp_images.keys())[0]] == SCREENSHOT_FILE
        assert_log(sync_session, report, ReportState.SUCCESS)


# ===========================================================================
# Slack chart report tests
# ===========================================================================
@patch("superset.commands.report_execute._get_slack_channels", create=True)
@patch("superset.reports.notifications.slack._should_use_v2_api", return_value=True)
@patch("superset.reports.notifications.slack._get_slack_client")
@patch("superset.commands.report_execute.ChartScreenshot.get_screenshot")
def test_slack_chart_report_schedule_converts_to_v2(
    screenshot_mock,
    slack_client_mock,
    slack_should_use_v2_api_mock,
    get_channels_with_search_mock,
    sync_session,
    create_report_slack_chart,
):
    """ExecuteReport Command: chart slack report converts recipients to v2."""
    screenshot_mock.return_value = SCREENSHOT_FILE
    report = create_report_slack_chart
    channel_id = "slack_channel_id"
    get_channels_with_search_mock.return_value = [
        {
            "id": channel_id,
            "name": "slack_channel",
            "is_member": True,
            "is_private": False,
        },
    ]

    with freeze_time("2020-01-01T00:00:00Z"):
        with _patch_settings():
            with patch(
                "superset.controllers.report._get_slack_channels",
                get_channels_with_search_mock,
            ):
                with patch(
                    "superset.extensions.stats_logger_manager.instance.gauge"
                ) as statsd_mock:
                    _run(report.id, sync_session)

                    assert (
                        slack_client_mock.return_value.files_upload_v2.call_args[1][
                            "channel"
                        ]
                        == channel_id
                    )
                    assert (
                        slack_client_mock.return_value.files_upload_v2.call_args[1][
                            "file"
                        ]
                        == SCREENSHOT_FILE
                    )
                    assert report.recipients[0].recipient_config_json == json.dumps(
                        {"target": channel_id}
                    )
                    assert report.recipients[0].type == ReportRecipientType.SLACKV2

                    assert_log(sync_session, report, ReportState.SUCCESS)
                    assert statsd_mock.call_args_list[0] == call(
                        "reports.slack.send.warning", 1
                    )
                    assert statsd_mock.call_args_list[1] == call(
                        "reports.slack.send.ok", 1
                    )


@pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
@patch("superset.reports.notifications.slack._should_use_v2_api", return_value=True)
@patch("superset.reports.notifications.slack._get_slack_client")
@patch("superset.commands.report_execute.ChartScreenshot.get_screenshot")
def test_slack_chart_report_schedule_converts_to_v2_channel_with_hash(
    screenshot_mock,
    slack_client_mock,
    slack_should_use_v2_api_mock,
    sync_session,
):
    """ExecuteReport Command: convert Slack report to v2 with leading-hash name."""
    screenshot_mock.return_value = SCREENSHOT_FILE
    channel_id = "slack_channel_id"
    chart = _first_chart(sync_session)
    report_schedule = create_report_notification(
        sync_session, slack_channel="#slack_channel", chart=chart
    )
    get_channels_with_search_mock = Mock(
        return_value=[
            {
                "id": channel_id,
                "name": "slack_channel",
                "is_member": True,
                "is_private": False,
            },
        ]
    )

    with freeze_time("2020-01-01T00:00:00Z"):
        with _patch_settings():
            with patch(
                "superset.controllers.report._get_slack_channels",
                get_channels_with_search_mock,
            ):
                with patch(
                    "superset.extensions.stats_logger_manager.instance.gauge"
                ) as statsd_mock:
                    _run(report_schedule.id, sync_session)

                    assert (
                        slack_client_mock.return_value.files_upload_v2.call_args[1][
                            "channel"
                        ]
                        == channel_id
                    )
                    assert (
                        slack_client_mock.return_value.files_upload_v2.call_args[1][
                            "file"
                        ]
                        == SCREENSHOT_FILE
                    )
                    assert report_schedule.recipients[
                        0
                    ].recipient_config_json == json.dumps({"target": channel_id})
                    assert (
                        report_schedule.recipients[0].type
                        == ReportRecipientType.SLACKV2
                    )
                    assert_log(sync_session, report_schedule, ReportState.SUCCESS)
                    assert statsd_mock.call_args_list[0] == call(
                        "reports.slack.send.warning", 1
                    )
                    assert statsd_mock.call_args_list[1] == call(
                        "reports.slack.send.ok", 1
                    )

    cleanup_report_schedule(sync_session, report_schedule)


@pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
@patch("superset.reports.notifications.slack._should_use_v2_api", return_value=True)
@patch("superset.reports.notifications.slack._get_slack_client")
@patch("superset.commands.report_execute.ChartScreenshot.get_screenshot")
def test_slack_chart_report_schedule_fails_to_converts_to_v2(
    screenshot_mock,
    slack_client_mock,
    slack_should_use_v2_api_mock,
    sync_session,
):
    """ExecuteReport Command: convert Slack report to v2 fails (missing channel)."""
    screenshot_mock.return_value = SCREENSHOT_FILE
    channel_id = "slack_channel_id"
    chart = _first_chart(sync_session)
    report_schedule = create_report_notification(
        sync_session, slack_channel="#slack_channel,my_member_ID", chart=chart
    )
    get_channels_with_search_mock = Mock(
        return_value=[
            {
                "id": channel_id,
                "name": "slack_channel",
                "is_member": True,
                "is_private": False,
            },
        ]
    )

    with _patch_settings():
        with patch(
            "superset.controllers.report._get_slack_channels",
            get_channels_with_search_mock,
        ):
            with pytest.raises(ReportScheduleSystemErrorsException):
                _run(report_schedule.id, sync_session)

    expected_message = (
        "Failed to update slack recipients to v2: "
        "Could not find the following channels: my_member_ID"
    )
    assert_log(
        sync_session,
        report_schedule,
        ReportState.ERROR,
        error_message=expected_message,
    )
    assert report_schedule.recipients[0].recipient_config_json == json.dumps(
        {"target": "#slack_channel,my_member_ID"}
    )
    assert report_schedule.recipients[0].type == ReportRecipientType.SLACK

    cleanup_report_schedule(sync_session, report_schedule)


@patch("superset.reports.notifications.slack._should_use_v2_api", return_value=True)
@patch("superset.reports.notifications.slack._get_slack_client")
@patch("superset.commands.report_execute.ChartScreenshot.get_screenshot")
def test_slack_chart_report_schedule_v2(
    screenshot_mock,
    slack_client_mock,
    slack_should_use_v2_api_mock,
    sync_session,
    create_report_slack_chartv2,
):
    """ExecuteReport Command: chart slack report using Slack v2."""
    screenshot_mock.return_value = SCREENSHOT_FILE
    report = create_report_slack_chartv2

    with freeze_time("2020-01-01T00:00:00Z"):
        with _patch_settings():
            with patch(
                "superset.extensions.stats_logger_manager.instance.gauge"
            ) as statsd_mock:
                _run(report.id, sync_session)

                assert (
                    slack_client_mock.return_value.files_upload_v2.call_args[1][
                        "channel"
                    ]
                    == "slack_channel_id"
                )
                assert (
                    slack_client_mock.return_value.files_upload_v2.call_args[1]["file"]
                    == SCREENSHOT_FILE
                )
                assert_log(sync_session, report, ReportState.SUCCESS)
                assert statsd_mock.call_args_list[0] == call(
                    "reports.slack.send.ok", 1
                )


@pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
@patch("superset.reports.notifications.slack._get_slack_client")
@patch("superset.commands.report_execute.ChartScreenshot.get_screenshot")
def test_slack_chart_report_schedule_with_errors(
    screenshot_mock,
    web_client_mock,
    sync_session,
    create_report_slack_chart,
):
    """ExecuteReport Command: every slack error is logged."""
    screenshot_mock.return_value = SCREENSHOT_FILE
    report = create_report_slack_chart

    slack_errors = [
        BotUserAccessError(),
        SlackRequestError(),
        SlackClientConfigurationError(),
        SlackObjectFormationError(),
        SlackTokenRotationError(api_error="foo"),
        SlackClientNotConnectedError(),
        SlackClientError(),
        SlackApiError(message="foo", response="bar"),
    ]

    with _patch_settings(feature_flags={"ALERT_REPORT_SLACK_V2": False}):
        for er in slack_errors:
            web_client_mock.side_effect = [SlackApiError(None, None), er]
            with pytest.raises(ReportScheduleClientErrorsException):
                _run(report.id, sync_session)
            sync_session.commit()

    notification_logs_count = get_notification_error_sent_count(sync_session, report)
    error_logs = get_error_logs_query(sync_session, report)

    assert error_logs.count() == (len(slack_errors) + notification_logs_count) * 2
    assert len([log.error_message for log in error_logs]) == error_logs.count()


@pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
@patch("superset.reports.notifications.slack._should_use_v2_api", return_value=False)
@patch("superset.reports.notifications.slack._get_slack_client")
@patch("superset.utils.csv.urllib.request.urlopen")
@patch("superset.utils.csv.urllib.request.OpenerDirector.open")
@patch("superset.utils.csv.get_chart_csv_data")
def test_slack_chart_report_schedule_with_csv(
    csv_mock,
    mock_open,
    mock_urlopen,
    slack_client_mock_class,
    slack_should_use_v2_api_mock,
    sync_session,
    create_report_slack_chart_with_csv,
):
    """ExecuteReport Command: chart slack v1 report with CSV."""
    response = Mock()
    mock_open.return_value = response
    mock_urlopen.return_value = response
    mock_urlopen.return_value.getcode.return_value = 200
    response.read.return_value = CSV_FILE
    report = create_report_slack_chart_with_csv

    notification_targets = get_target_from_report_schedule(report)
    channel_name = notification_targets[0]

    with freeze_time("2020-01-01T00:00:00Z"):
        with _patch_settings():
            _run(report.id, sync_session)

        assert (
            slack_client_mock_class.return_value.files_upload.call_args[1]["channels"]
            == channel_name
        )
        assert (
            slack_client_mock_class.return_value.files_upload.call_args[1]["file"]
            == CSV_FILE
        )
        assert_log(sync_session, report, ReportState.SUCCESS)


@pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
@patch("superset.reports.notifications.slack._should_use_v2_api", return_value=False)
@patch("superset.utils.csv.urllib.request.urlopen")
@patch("superset.utils.csv.urllib.request.OpenerDirector.open")
@patch("superset.reports.notifications.slack._get_slack_client")
@patch("superset.utils.csv.get_chart_dataframe")
def test_slack_chart_report_schedule_with_text(
    dataframe_mock,
    slack_client_mock_class,
    mock_open,
    mock_urlopen,
    slack_should_use_v2_api_mock,
    sync_session,
    create_report_slack_chart_with_text,
):
    """ExecuteReport Command: chart slack report with embedded text table."""
    response = Mock()
    mock_open.return_value = response
    mock_urlopen.return_value = response
    mock_urlopen.return_value.getcode.return_value = 200
    response.read.return_value = json.dumps(
        {
            "result": [
                {
                    "data": {
                        "t1": {0: "c11", 1: "c21"},
                        "t2": {0: "c12", 1: "c22"},
                        "t3__sum": {0: "c13", 1: "c23"},
                    },
                    "colnames": [("t1",), ("t2",), ("t3__sum",)],
                    "indexnames": [(0,), (1,)],
                    "coltypes": [1, 1, 0],
                },
            ],
        }
    ).encode("utf-8")
    report = create_report_slack_chart_with_text

    with freeze_time("2020-01-01T00:00:00Z"):
        with _patch_settings():
            _run(report.id, sync_session)

        table_markdown = """|    | t1   | t2   | t3__sum   |
|---:|:-----|:-----|:----------|
|  0 | c11  | c12  | c13       |
|  1 | c21  | c22  | c23       |"""
        assert (
            table_markdown
            in slack_client_mock_class.return_value.chat_postMessage.call_args[1][
                "text"
            ]
        )
        assert (
            f"<http://0.0.0.0:8080/explore/?form_data=%7B%22slice_id%22%3A+{report.chart.id}%7D&force=false|Explore in Superset>"  # noqa: E501
            in slack_client_mock_class.return_value.chat_postMessage.call_args[1][
                "text"
            ]
        )
        assert_log(sync_session, report, ReportState.SUCCESS)


# ===========================================================================
# State machine / grace period tests
# ===========================================================================
def test_report_schedule_not_found(sync_session, create_report_slack_chart):
    """ExecuteReport Command: report schedule not found."""
    max_id = sync_session.query(func.max(ReportSchedule.id)).scalar()
    with _patch_settings():
        with pytest.raises(ReportScheduleNotFoundError):
            _run(max_id + 1, sync_session)


def test_report_schedule_working(sync_session, create_report_slack_chart_working):
    """ExecuteReport Command: report schedule still working."""
    report = create_report_slack_chart_working
    with freeze_time("2020-01-01T00:00:00Z"):
        with _patch_settings():
            with pytest.raises(ReportSchedulePreviousWorkingError):
                _run(report.id, sync_session)

        assert_log(
            sync_session,
            report,
            ReportState.WORKING,
            error_message=ReportSchedulePreviousWorkingError.message,
        )
        assert report.last_state == ReportState.WORKING


def test_report_schedule_working_timeout(
    sync_session, create_report_slack_chart_working
):
    """ExecuteReport Command: report schedule working timeout."""
    report = create_report_slack_chart_working
    current_time = report.last_eval_dttm + timedelta(
        seconds=report.working_timeout + 1
    )
    with freeze_time(current_time):
        with _patch_settings():
            with pytest.raises(ReportScheduleWorkingTimeoutError):
                _run(report.id, sync_session)

    logs = (
        sync_session.query(ReportExecutionLog)
        .filter(ReportExecutionLog.report_schedule == report)
        .all()
    )
    # Two logs, first is created by fixture
    assert len(logs) == 2
    assert ReportScheduleWorkingTimeoutError.message in [
        log.error_message for log in logs
    ]
    assert report.last_state == ReportState.ERROR


def test_report_schedule_success_grace(
    sync_session, create_alert_slack_chart_success
):
    """ExecuteReport Command: report schedule on success to grace."""
    report = create_alert_slack_chart_success
    current_time = report.last_eval_dttm + timedelta(
        seconds=report.grace_period - 10
    )
    with freeze_time(current_time):
        with _patch_settings():
            _run(report.id, sync_session)

    sync_session.commit()
    assert report.last_state == ReportState.GRACE


@patch("superset.reports.notifications.slack._should_use_v2_api", return_value=False)
@patch("superset.commands.report_execute.ChartScreenshot.get_screenshot")
@patch("superset.reports.notifications.slack._get_slack_client")
def test_report_schedule_success_grace_end(
    slack_client_mock_class,
    screenshot_mock,
    slack_should_use_v2_api_mock,
    sync_session,
    create_alert_slack_chart_grace,
):
    """ExecuteReport Command: report schedule on grace to noop/success."""
    screenshot_mock.return_value = SCREENSHOT_FILE
    report = create_alert_slack_chart_grace
    current_time = report.last_eval_dttm + timedelta(
        seconds=report.grace_period + 1
    )
    notification_targets = get_target_from_report_schedule(report)
    channel_name = notification_targets[0]
    channel_id = "channel_id"
    slack_client_mock_class.return_value.conversations_list.return_value = {
        "channels": [{"id": channel_id, "name": channel_name}]
    }

    with freeze_time(current_time):
        with _patch_settings():
            _run(report.id, sync_session)

    sync_session.commit()
    assert report.last_state == ReportState.SUCCESS


@pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
@patch("superset.reports.notifications.email._send_email_smtp")
@patch("superset.commands.report_execute.ChartScreenshot.get_screenshot")
def test_alert_limit_is_applied(
    screenshot_mock,
    email_mock,
    sync_session,
    create_alert_email_chart,
):
    """ExecuteReport Command: alerts apply a SQL LIMIT to statements."""
    screenshot_mock.return_value = SCREENSHOT_FILE
    report = create_alert_email_chart

    with patch.object(
        report.database.db_engine_spec, "execute", return_value=None
    ) as execute_mock:
        with patch.object(
            report.database.db_engine_spec,
            "fetch_data",
            return_value=[],
        ):
            with _patch_settings():
                _run(report.id, sync_session)
            assert "LIMIT 2" in execute_mock.call_args[0][1]


@pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
@patch("superset.reports.notifications.email._send_email_smtp")
@patch("superset.commands.report_execute.DashboardScreenshot.get_screenshot")
def test_email_dashboard_report_fails(
    screenshot_mock, email_mock, sync_session, create_report_email_dashboard
):
    """ExecuteReport Command: dashboard email report notification fails (SMTP)."""
    from smtplib import SMTPException

    screenshot_mock.return_value = SCREENSHOT_FILE
    email_mock.side_effect = SMTPException("Could not connect to SMTP XPTO")
    report = create_report_email_dashboard

    with _patch_settings():
        with pytest.raises(ReportScheduleSystemErrorsException):
            _run(report.id, sync_session)

    assert_log(
        sync_session,
        report,
        ReportState.ERROR,
        error_message="Could not connect to SMTP XPTO",
    )


@pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
@patch("superset.reports.notifications.email._send_email_smtp")
@patch("superset.commands.report_execute.DashboardScreenshot.get_screenshot")
def test_email_dashboard_report_fails_uncaught_exception(
    screenshot_mock, email_mock, sync_session, create_report_email_dashboard
):
    """ExecuteReport Command: dashboard email report fails (uncaught exception).

    The email call-to-action link text is read from the notification config
    dict (``_build_notification_config`` → ``EMAIL_REPORTS_CTA``), not from the
    execute command's ``_get_settings``, so ``EMAIL_REPORTS_CTA`` is injected
    there to mirror the upstream ``app.config`` override.
    """
    from superset.reports.notifications import _build_notification_config

    screenshot_mock.return_value = SCREENSHOT_FILE
    email_mock.side_effect = Exception("Uncaught exception")
    report = create_report_email_dashboard

    base_config = _build_notification_config()
    base_config["EMAIL_REPORTS_CTA"] = "Call to action"

    with _patch_settings(email_reports_cta="Call to action"):
        with patch(
            "superset.reports.notifications._build_notification_config",
            return_value=base_config,
        ):
            with pytest.raises(Exception):  # noqa: B017, PT011
                _run(report.id, sync_session)

    assert_log(
        sync_session,
        report,
        ReportState.ERROR,
        error_message="Uncaught exception",
    )
    assert (
        '<a href="http://0.0.0.0:8080/superset/dashboard/'
        f"{report.dashboard.uuid}/"
        '?force=false">Call to action</a>' in email_mock.call_args[0][2]
    )


@pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
@patch("superset.reports.notifications.email._send_email_smtp")
@patch("superset.commands.report_execute.ChartScreenshot.get_screenshot")
def test_slack_chart_alert(
    screenshot_mock,
    email_mock,
    sync_session,
    create_alert_email_chart,
):
    """ExecuteReport Command: chart alert with attached screenshot."""
    screenshot_mock.return_value = SCREENSHOT_FILE
    report = create_alert_email_chart

    with freeze_time("2020-01-01T00:00:00Z"):
        with _patch_settings(feature_flags={"ALERTS_ATTACH_REPORTS": True}):
            _run(report.id, sync_session)

        notification_targets = get_target_from_report_schedule(report)
        assert email_mock.call_args[0][0] == notification_targets[0]
        smtp_images = email_mock.call_args[1]["images"]
        assert smtp_images[list(smtp_images.keys())[0]] == SCREENSHOT_FILE
        assert_log(sync_session, report, ReportState.SUCCESS)


@pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
@patch("superset.reports.notifications.email._send_email_smtp")
def test_slack_chart_alert_no_attachment(
    email_mock, sync_session, create_alert_email_chart
):
    """ExecuteReport Command: chart alert without attached image."""
    report = create_alert_email_chart

    with freeze_time("2020-01-01T00:00:00Z"):
        with _patch_settings(feature_flags={"ALERTS_ATTACH_REPORTS": False}):
            _run(report.id, sync_session)

        notification_targets = get_target_from_report_schedule(report)
        assert email_mock.call_args[0][0] == notification_targets[0]
        assert email_mock.call_args[1]["images"] == {}
        assert_log(sync_session, report, ReportState.SUCCESS)


@pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
@patch("superset.reports.notifications.slack.WebClient")
@patch("superset.commands.report_execute.ChartScreenshot.get_screenshot")
def test_slack_token_callable_chart_report(
    screenshot_mock,
    slack_client_mock_class,
    sync_session,
    create_report_slack_chart,
):
    """ExecuteReport Command: chart slack alert with a callable Slack token.

    The port resolves the callable Slack token inside the real
    ``_get_slack_client`` (built from the notification config dict), so the
    callable is injected into ``_build_notification_config`` and the
    ``WebClient`` class is patched (mirroring the upstream
    ``superset.utils.slack.WebClient`` patch) to assert it receives the
    resolved token.
    """
    report = create_report_slack_chart
    notification_targets = get_target_from_report_schedule(report)
    channel_name = notification_targets[0]
    channel_id = "channel_id"
    slack_client_mock_class.return_value = Mock()
    slack_client_mock_class.return_value.conversations_list.return_value = {
        "channels": [{"id": channel_id, "name": channel_name}]
    }
    screenshot_mock.return_value = SCREENSHOT_FILE

    slack_token_mock = Mock(return_value="cool_code")
    # In the port the Slack token is materialised into the notification config
    # dict (``_build_notification_config``), so inject the callable there
    # instead of mutating ``flask.current_app.config``.
    from superset.reports.notifications import _build_notification_config

    base_config = _build_notification_config()
    base_config["SLACK_API_TOKEN"] = slack_token_mock

    with freeze_time("2020-01-01T00:00:00Z"):
        with _patch_settings(feature_flags={"ALERT_REPORT_SLACK_V2": False}):
            with patch(
                "superset.reports.notifications._build_notification_config",
                return_value=base_config,
            ):
                _run(report.id, sync_session)
                slack_token_mock.assert_called()
                slack_client_mock_class.assert_called_with(
                    token="cool_code", proxy=None  # noqa: S106
                )
                assert_log(sync_session, report, ReportState.SUCCESS)


def test_email_chart_no_alert(sync_session, create_no_alert_email_chart):
    """ExecuteReport Command: chart email no alert (noop)."""
    report = create_no_alert_email_chart
    with freeze_time("2020-01-01T00:00:00Z"):
        with _patch_settings():
            _run(report.id, sync_session)
    assert_log(sync_session, report, ReportState.NOOP)


def test_email_mul_alert(sync_session, create_mul_alert_email_chart):
    """ExecuteReport Command: chart email multiple rows/columns errors."""
    report = create_mul_alert_email_chart
    with freeze_time("2020-01-01T00:00:00Z"):
        with _patch_settings():
            with pytest.raises(
                (AlertQueryMultipleRowsError, AlertQueryMultipleColumnsError)
            ):
                _run(report.id, sync_session)


@pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
@patch("superset.reports.notifications.email._send_email_smtp")
def test_soft_timeout_alert(email_mock, sync_session, create_alert_email_chart):
    """ExecuteReport Command: soft timeout on alert query."""
    from celery.exceptions import SoftTimeLimitExceeded

    from superset.commands.report_exceptions import AlertQueryTimeout

    report = create_alert_email_chart

    with patch.object(
        report.database.db_engine_spec, "execute", return_value=None
    ) as execute_mock:
        execute_mock.side_effect = SoftTimeLimitExceeded()
        with _patch_settings():
            with pytest.raises(AlertQueryTimeout):
                _run(report.id, sync_session)

    assert email_mock.call_args[0][0] == DEFAULT_OWNER_EMAIL
    assert_log(
        sync_session,
        report,
        ReportState.ERROR,
        error_message="A timeout occurred while executing the query.",
    )


@pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
@patch("superset.reports.notifications.email._send_email_smtp")
@patch("superset.commands.report_execute.ChartScreenshot.get_screenshot")
def test_soft_timeout_screenshot(
    screenshot_mock, email_mock, sync_session, create_alert_email_chart
):
    """ExecuteReport Command: soft timeout on screenshot."""
    from celery.exceptions import SoftTimeLimitExceeded

    screenshot_mock.side_effect = SoftTimeLimitExceeded()
    report = create_alert_email_chart

    with _patch_settings(feature_flags={"ALERTS_ATTACH_REPORTS": True}):
        with pytest.raises(ReportScheduleScreenshotTimeout):
            _run(report.id, sync_session)

    assert email_mock.call_args[0][0] == DEFAULT_OWNER_EMAIL
    assert_log(
        sync_session,
        report,
        ReportState.ERROR,
        error_message="A timeout occurred while taking a screenshot.",
    )


@pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
@patch("superset.utils.csv.urllib.request.urlopen")
@patch("superset.utils.csv.urllib.request.OpenerDirector.open")
@patch("superset.reports.notifications.email._send_email_smtp")
@patch("superset.utils.csv.get_chart_csv_data")
def test_soft_timeout_csv(
    csv_mock,
    email_mock,
    mock_open,
    mock_urlopen,
    sync_session,
    create_report_email_chart_with_csv,
):
    """ExecuteReport Command: soft timeout generating csv."""
    from celery.exceptions import SoftTimeLimitExceeded

    response = Mock()
    mock_open.return_value = response
    mock_urlopen.return_value = response
    mock_urlopen.return_value.getcode.side_effect = SoftTimeLimitExceeded()
    report = create_report_email_chart_with_csv

    with _patch_settings():
        with pytest.raises(ReportScheduleCsvTimeout):
            _run(report.id, sync_session)

    assert email_mock.call_args[0][0] == DEFAULT_OWNER_EMAIL
    assert_log(
        sync_session,
        report,
        ReportState.ERROR,
        error_message="A timeout occurred while generating a csv.",
    )


@pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
@patch("superset.utils.csv.urllib.request.urlopen")
@patch("superset.utils.csv.urllib.request.OpenerDirector.open")
@patch("superset.reports.notifications.email._send_email_smtp")
@patch("superset.utils.csv.get_chart_csv_data")
def test_generate_no_csv(
    csv_mock,
    email_mock,
    mock_open,
    mock_urlopen,
    sync_session,
    create_report_email_chart_with_csv,
):
    """ExecuteReport Command: fail generating csv (empty response)."""
    response = Mock()
    mock_open.return_value = response
    mock_urlopen.return_value = response
    mock_urlopen.return_value.getcode.return_value = 200
    response.read.return_value = None
    report = create_report_email_chart_with_csv

    with _patch_settings():
        with pytest.raises(ReportScheduleCsvFailedError):
            _run(report.id, sync_session)

    assert email_mock.call_args[0][0] == DEFAULT_OWNER_EMAIL
    assert_log(
        sync_session,
        report,
        ReportState.ERROR,
        error_message="Report Schedule execution failed when generating a csv.",
    )


@pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
@patch("superset.reports.notifications.email._send_email_smtp")
@patch("superset.commands.report_execute.ChartScreenshot.get_screenshot")
def test_fail_screenshot(
    screenshot_mock, email_mock, sync_session, create_report_email_chart
):
    """ExecuteReport Command: screenshot failure."""
    screenshot_mock.side_effect = Exception("Unexpected error")
    report = create_report_email_chart

    with _patch_settings():
        with pytest.raises(ReportScheduleScreenshotFailedError):
            _run(report.id, sync_session)

    assert email_mock.call_args[0][0] == DEFAULT_OWNER_EMAIL
    assert_log(
        sync_session,
        report,
        ReportState.ERROR,
        error_message="Failed taking a screenshot Unexpected error",
    )


@pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
@patch("superset.reports.notifications.email._send_email_smtp")
@patch("superset.utils.csv.urllib.request.urlopen")
@patch("superset.utils.csv.urllib.request.OpenerDirector.open")
@patch("superset.utils.csv.get_chart_csv_data")
def test_fail_csv(
    csv_mock,
    mock_open,
    mock_urlopen,
    email_mock,
    sync_session,
    create_report_email_chart_with_csv,
):
    """ExecuteReport Command: csv generation HTTP error."""
    response = Mock()
    mock_open.return_value = response
    mock_urlopen.return_value = response
    mock_urlopen.return_value.getcode.return_value = 500
    report = create_report_email_chart_with_csv

    with _patch_settings():
        with pytest.raises(ReportScheduleCsvFailedError):
            _run(report.id, sync_session)

    assert email_mock.call_args[0][0] == DEFAULT_OWNER_EMAIL
    assert_log(
        sync_session,
        report,
        ReportState.ERROR,
        error_message="Failed generating csv <urlopen error 500>",
    )


@pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
@patch("superset.reports.notifications.email._send_email_smtp")
def test_email_disable_screenshot(
    email_mock, sync_session, create_alert_email_chart
):
    """ExecuteReport Command: alert with screenshot attachment disabled."""
    report = create_alert_email_chart

    with _patch_settings(feature_flags={"ALERTS_ATTACH_REPORTS": False}):
        _run(report.id, sync_session)

    notification_targets = get_target_from_report_schedule(report)
    assert email_mock.call_args[0][0] == notification_targets[0]
    assert email_mock.call_args[1]["images"] == {}
    assert_log(sync_session, report, ReportState.SUCCESS)


@patch("superset.reports.notifications.email._send_email_smtp")
def test_invalid_sql_alert(
    email_mock, sync_session, create_invalid_sql_alert_email_chart
):
    """ExecuteReport Command: alert with invalid SQL."""
    report = create_invalid_sql_alert_email_chart
    with freeze_time("2020-01-01T00:00:00Z"):
        with _patch_settings():
            with pytest.raises((AlertQueryError, AlertQueryInvalidTypeError)):
                _run(report.id, sync_session)

        assert email_mock.call_args[0][0] == DEFAULT_OWNER_EMAIL
        assert_log(sync_session, report, ReportState.ERROR)


@patch("superset.reports.notifications.email._send_email_smtp")
def test_grace_period_error(
    email_mock, sync_session, create_invalid_sql_alert_email_chart
):
    """ExecuteReport Command: alert grace period on error."""
    report = create_invalid_sql_alert_email_chart
    with freeze_time("2020-01-01T00:00:00Z"):
        with _patch_settings():
            with pytest.raises((AlertQueryError, AlertQueryInvalidTypeError)):
                _run(report.id, sync_session)

        assert email_mock.call_args[0][0] == DEFAULT_OWNER_EMAIL
        assert get_notification_error_sent_count(sync_session, report) == 1

    with freeze_time("2020-01-01T00:30:00Z"):
        with _patch_settings():
            with pytest.raises((AlertQueryError, AlertQueryInvalidTypeError)):
                _run(report.id, sync_session)
        sync_session.commit()
        assert get_notification_error_sent_count(sync_session, report) == 1

    with freeze_time("2020-01-01T01:30:00Z"):
        with _patch_settings():
            with pytest.raises((AlertQueryError, AlertQueryInvalidTypeError)):
                _run(report.id, sync_session)
        sync_session.commit()
        assert get_notification_error_sent_count(sync_session, report) == 2


@patch("superset.reports.notifications.email._send_email_smtp")
@patch("superset.commands.report_execute.ChartScreenshot.get_screenshot")
def test_grace_period_error_flap(
    screenshot_mock,
    email_mock,
    sync_session,
    create_invalid_sql_alert_email_chart,
):
    """ExecuteReport Command: alert grace period on error (with recovery)."""
    report = create_invalid_sql_alert_email_chart
    with freeze_time("2020-01-01T00:00:00Z"):
        with _patch_settings():
            with pytest.raises((AlertQueryError, AlertQueryInvalidTypeError)):
                _run(report.id, sync_session)
        sync_session.commit()
        assert get_notification_error_sent_count(sync_session, report) == 1

    with freeze_time("2020-01-01T00:30:00Z"):
        with _patch_settings():
            with pytest.raises((AlertQueryError, AlertQueryInvalidTypeError)):
                _run(report.id, sync_session)
        sync_session.commit()
        assert get_notification_error_sent_count(sync_session, report) == 1

    report.sql = "SELECT 1 AS metric"
    report.grace_period = 0
    sync_session.commit()

    with freeze_time("2020-01-01T00:31:00Z"):
        with _patch_settings():
            _run(report.id, sync_session)
            _run(report.id, sync_session)
        sync_session.commit()

    report.sql = "SELECT 'first'"
    report.grace_period = 10
    sync_session.commit()

    with freeze_time("2020-01-01T00:32:00Z"):
        with _patch_settings():
            with pytest.raises((AlertQueryError, AlertQueryInvalidTypeError)):
                _run(report.id, sync_session)
        sync_session.commit()
        assert get_notification_error_sent_count(sync_session, report) == 2


@pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
def test_prune_log_soft_time_out(sync_session, create_report_email_dashboard):
    """PruneReportSchedule Command: soft timeout.

    Upstream patched ``ReportScheduleDAO.bulk_delete_logs`` to raise
    ``SoftTimeLimitExceeded``; the port deletes logs inline via
    ``session.query(ReportExecutionLog)...delete()`` for schedules that set
    ``log_retention``, so the timeout is injected on that ``Query.delete`` call
    (``SoftTimeLimitExceeded`` is not a ``SQLAlchemyError`` and therefore
    propagates unswallowed, exactly as upstream).
    """
    from celery.exceptions import SoftTimeLimitExceeded
    from sqlalchemy.orm import Query

    # give the schedule a retention so the prune command reaches the delete
    report = create_report_email_dashboard
    report.log_retention = 30
    sync_session.commit()

    with patch.object(Query, "delete", side_effect=SoftTimeLimitExceeded()):
        with pytest.raises(SoftTimeLimitExceeded) as excinfo:
            PruneReportScheduleLogCommand().run(sync_session)
    assert str(excinfo.value) == "SoftTimeLimitExceeded()"


@patch("superset.commands.report_execute.logger")
@patch("superset.commands.report_execute.create_notification")
def test__send_with_client_errors(notification_mock, logger_mock):
    """BaseReportState._send: notification param exception → client errors.

    The port's ``SupersetException.message`` default is
    ``"An unexpected error occurred"`` (upstream defaulted to ``""``), so the
    logged ``SupersetError`` carries that message — otherwise identical.
    """
    notification_content = "I am some content"
    recipients = ["test@foo.com"]
    notification_mock.return_value.send.side_effect = NotificationParamException()
    with _patch_settings():
        with pytest.raises(ReportScheduleClientErrorsException) as excinfo:
            BaseReportState._send(BaseReportState, notification_content, recipients)

    assert excinfo.errisinstance(SupersetException)
    logger_mock.warning.assert_called_with(
        "SupersetError(message='An unexpected error occurred', error_type=<SupersetErrorType.REPORT_NOTIFICATION_ERROR: 'REPORT_NOTIFICATION_ERROR'>, level=<ErrorLevel.WARNING: 'warning'>, extra=None)"  # noqa: E501
    )


@patch("superset.commands.report_execute.logger")
@patch("superset.commands.report_execute.create_notification")
def test__send_with_multiple_errors(notification_mock, logger_mock):
    """BaseReportState._send: multiple errors → system errors (500)."""
    notification_content = "I am some content"
    recipients = ["test@foo.com", "test2@bar.com"]
    notification_mock.return_value.send.side_effect = [
        NotificationParamException(),
        NotificationError(),
    ]
    with _patch_settings():
        with pytest.raises(ReportScheduleSystemErrorsException) as excinfo:
            BaseReportState._send(BaseReportState, notification_content, recipients)

    assert excinfo.errisinstance(SupersetException)
    logger_mock.warning.assert_has_calls(
        [
            call(
                "SupersetError(message='An unexpected error occurred', error_type=<SupersetErrorType.REPORT_NOTIFICATION_ERROR: 'REPORT_NOTIFICATION_ERROR'>, level=<ErrorLevel.WARNING: 'warning'>, extra=None)"  # noqa: E501
            ),
            call(
                "SupersetError(message='An unexpected error occurred', error_type=<SupersetErrorType.REPORT_NOTIFICATION_ERROR: 'REPORT_NOTIFICATION_ERROR'>, level=<ErrorLevel.ERROR: 'error'>, extra=None)"  # noqa: E501
            ),
        ]
    )


@patch("superset.commands.report_execute.logger")
@patch("superset.commands.report_execute.create_notification")
def test__send_with_server_errors(notification_mock, logger_mock):
    """BaseReportState._send: notification error → system errors (500)."""
    notification_content = "I am some content"
    recipients = ["test@foo.com"]
    notification_mock.return_value.send.side_effect = NotificationError()
    with _patch_settings():
        with pytest.raises(ReportScheduleSystemErrorsException) as excinfo:
            BaseReportState._send(BaseReportState, notification_content, recipients)

    assert excinfo.errisinstance(SupersetException)
    logger_mock.warning.assert_called_with(
        "SupersetError(message='An unexpected error occurred', error_type=<SupersetErrorType.REPORT_NOTIFICATION_ERROR: 'REPORT_NOTIFICATION_ERROR'>, level=<ErrorLevel.ERROR: 'error'>, extra=None)"  # noqa: E501
    )
