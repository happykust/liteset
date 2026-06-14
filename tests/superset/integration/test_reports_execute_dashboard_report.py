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
"""Flask-free port of the dashboard-report execution command integration tests.

Drives the real synchronous :class:`ExecuteReportScheduleCommand`
(``superset.commands.report_execute``) against a tabbed dashboard, owner user
and report schedule built through the sync Celery ``Session``
(:func:`superset.db.session.get_sync_session`) — the same session the command
takes. Screenshots and the SMTP send are mocked; the permalink, executor
resolution, notification content (header data + inline images) and the
permalink URL are exercised for real.

Differences from the upstream test (all faithful 1:1 in intent):

* ``AsyncExecuteReportScheduleCommand`` → ``ExecuteReportScheduleCommand``
  (the port keeps the original behaviour but takes the sync ``session`` as a
  4th constructor argument).
* The mock targets the port's module paths:
  ``superset.commands.report_execute.DashboardScreenshot`` and
  ``superset.reports.notifications.email._send_email_smtp`` (the port's
  module-level helper that receives the ``images`` / ``header_data`` kwargs the
  upstream ``send_email_smtp`` received).
* The expected permalink is computed through the async
  ``CreateDashboardPermalinkCommand`` (the port made it async); the
  deterministic-uuid layout matches the sync ``_create_dashboard_permalink``
  used inside execution, so the same dashboard+state+user reuses the key.
* ``ALERT_REPORT_TABS`` / ``ALERT_REPORTS_NOTIFICATION_DRY_RUN`` are applied by
  patching the execute command's settings accessor instead of mutating Flask
  ``app.config``.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from superset.commands.dashboard.permalink.create import (
    CreateDashboardPermalinkCommand,
)
from superset.commands.report_exceptions import ReportScheduleUnexpectedError
from superset.commands.report_execute import ExecuteReportScheduleCommand
from superset.config import SupersetSettings
from superset.db.daos.key_value import AsyncKeyValueDAO
from superset.db.session import get_sync_session
from superset.models.dashboard import Dashboard
from superset.models.reports import (
    ReportRecipients,
    ReportRecipientType,
    ReportSchedule,
    ReportScheduleType,
    ReportSourceFormat,
)
from superset.models.security import User
from superset.utils import json
from superset.utils.urls import get_url_path

# Minimal tabbed position_json (the execution path only reads it for the
# dashboard digest; tab ids in the report state are not re-validated at execute
# time).
TABBED_POSITION_JSON = {
    "DASHBOARD_VERSION_KEY": "v2",
    "ROOT_ID": {"children": ["GRID_ID"], "id": "ROOT_ID", "type": "ROOT"},
    "GRID_ID": {
        "children": ["TABS-L1B"],
        "id": "GRID_ID",
        "parents": ["ROOT_ID"],
        "type": "GRID",
    },
    "TABS-L1B": {
        "children": ["TAB-L1B"],
        "id": "TABS-L1B",
        "meta": {},
        "parents": ["ROOT_ID", "GRID_ID"],
        "type": "TABS",
    },
    "TAB-L1B": {
        "children": ["TABS-L2B"],
        "id": "TAB-L1B",
        "meta": {"text": "Tab L1B"},
        "parents": ["ROOT_ID", "GRID_ID", "TABS-L1B"],
        "type": "TAB",
    },
    "TABS-L2B": {
        "children": ["TAB-L2BB"],
        "id": "TABS-L2B",
        "meta": {},
        "parents": ["ROOT_ID", "GRID_ID", "TABS-L1B", "TAB-L1B"],
        "type": "TABS",
    },
    "TAB-L2BB": {
        "children": [],
        "id": "TAB-L2BB",
        "meta": {"text": "Tab L2BB"},
        "parents": ["ROOT_ID", "GRID_ID", "TABS-L1B", "TAB-L1B", "TABS-L2B"],
        "type": "TAB",
    },
}


def _settings_with_tabs() -> SupersetSettings:
    settings = SupersetSettings()  # type: ignore[call-arg]
    settings.feature_flags = {**settings.feature_flags, "ALERT_REPORT_TABS": True}
    settings.alert_reports_notification_dry_run = False
    return settings


def _build_report(session, extra: dict, name: str) -> ReportSchedule:
    """Create an owner user, tabbed dashboard and a dashboard report schedule."""
    suffix = uuid4().hex[:8]
    owner = User(
        username=f"report_owner_{suffix}",
        first_name="Report",
        last_name="Owner",
        email=f"report_owner_{suffix}@example.com",
    )
    session.add(owner)
    session.flush()

    dashboard = Dashboard(
        dashboard_title=f"Tabbed report dash {suffix}",
        slug=f"tabbed-report-{suffix}",
        position_json=json.dumps(TABBED_POSITION_JSON),
    )
    session.add(dashboard)
    session.flush()

    report = ReportSchedule(
        type=ReportScheduleType.REPORT,
        name=name,
        crontab="0 9 * * *",
        dashboard_id=dashboard.id,
        owners=[owner],
        recipients=[
            ReportRecipients(
                type=ReportRecipientType.EMAIL,
                recipient_config_json=json.dumps({"target": "target@example.com"}),
            )
        ],
        grace_period=14400,
        working_timeout=3600,
    )
    report.extra = {"dashboard": extra}
    session.add(report)
    session.commit()
    return report


def _cleanup(report_id: int, dashboard_id: int, owner_id: int) -> None:
    session = get_sync_session()
    report = session.query(ReportSchedule).filter_by(id=report_id).one_or_none()
    if report:
        session.delete(report)
    session.commit()
    dash = session.query(Dashboard).filter_by(id=dashboard_id).one_or_none()
    if dash:
        session.delete(dash)
    owner = session.query(User).filter_by(id=owner_id).one_or_none()
    if owner:
        session.delete(owner)
    session.commit()
    session.close()


async def _expected_permalink_key(
    db_session: AsyncSession,
    dashboard: Dashboard,
    state: dict,
    user_id: int,
) -> str:
    """Compute the permalink key the same way the execution path does."""
    return await CreateDashboardPermalinkCommand(
        AsyncKeyValueDAO(db_session),
        dashboard.id,
        state,
        dashboard_uuid=str(dashboard.uuid),
        user_id=user_id,
    ).run()


@patch("superset.reports.notifications.email._send_email_smtp")
@patch("superset.commands.report_execute.DashboardScreenshot")
async def test_report_for_dashboard_with_tabs(
    dashboard_screenshot_mock: MagicMock,
    send_email_smtp_mock: MagicMock,
    db_session: AsyncSession,
) -> None:
    dashboard_screenshot_mock.get_screenshot.return_value = b"test-image"

    session = get_sync_session()
    report = _build_report(
        session,
        {"active_tabs": ["TAB-L1B", "TAB-L2BB"]},
        "test report tabbed dashboard",
    )
    dashboard_id = report.dashboard_id
    owner_id = report.owners[0].id
    report_id = report.id
    try:
        with patch(
            "superset.commands.report_execute._get_settings",
            return_value=_settings_with_tabs(),
        ):
            ExecuteReportScheduleCommand(
                str(uuid4()), report_id, datetime.utcnow(), session
            ).run()

            dashboard = session.query(Dashboard).filter_by(id=dashboard_id).one()
            dashboard_state = report.extra.get("dashboard", {})
            permalink_key = await _expected_permalink_key(
                db_session, dashboard, dashboard_state, owner_id
            )
            expected_url = get_url_path(
                "Superset.dashboard_permalink", key=permalink_key
            )

            assert dashboard_screenshot_mock.call_count == 1
            called_url = dashboard_screenshot_mock.call_args.args[0]

            assert called_url == expected_url
            assert send_email_smtp_mock.call_count == 1
            assert len(send_email_smtp_mock.call_args.kwargs["images"]) == 1
    finally:
        _cleanup(report_id, dashboard_id, owner_id)


@patch("superset.reports.notifications.email._send_email_smtp")
@patch("superset.commands.report_execute.DashboardScreenshot")
async def test_report_with_header_data(
    dashboard_screenshot_mock: MagicMock,
    send_email_smtp_mock: MagicMock,
    db_session: AsyncSession,
) -> None:
    dashboard_screenshot_mock.get_screenshot.return_value = b"test-image"

    session = get_sync_session()
    report = _build_report(
        session,
        {"active_tabs": ["TAB-L1B", "TAB-L2BB"]},
        "test report tabbed dashboard",
    )
    dashboard_id = report.dashboard_id
    owner_id = report.owners[0].id
    report_id = report.id
    try:
        with patch(
            "superset.commands.report_execute._get_settings",
            return_value=_settings_with_tabs(),
        ):
            ExecuteReportScheduleCommand(
                str(uuid4()), report_id, datetime.utcnow(), session
            ).run()

            dashboard = session.query(Dashboard).filter_by(id=dashboard_id).one()
            dashboard_state = report.extra.get("dashboard", {})
            permalink_key = await _expected_permalink_key(
                db_session, dashboard, dashboard_state, owner_id
            )

            assert dashboard_screenshot_mock.call_count == 1
            url = dashboard_screenshot_mock.call_args.args[0]

            assert url.endswith(f"/superset/dashboard/p/{permalink_key}/")
            assert send_email_smtp_mock.call_count == 1
            header_data = send_email_smtp_mock.call_args.kwargs["header_data"]
            assert header_data.get("dashboard_id") == dashboard_id
            assert header_data.get("notification_format") == report.report_format
            assert (
                header_data.get("notification_source")
                == ReportSourceFormat.DASHBOARD
            )
            assert header_data.get("notification_type") == report.type
            assert len(send_email_smtp_mock.call_args.kwargs["header_data"]) == 8
    finally:
        _cleanup(report_id, dashboard_id, owner_id)


@patch("superset.reports.notifications.email._send_email_smtp")
@patch("superset.commands.report_execute.DashboardScreenshot")
async def test_report_schedule_stale_data_error_preserves_cause(
    dashboard_screenshot_mock: MagicMock,
    send_email_smtp_mock: MagicMock,
    db_session: AsyncSession,
) -> None:
    """When the session ``commit`` raises ``StaleDataError`` during logging we
    surface ``ReportScheduleUnexpectedError`` while preserving the original
    ``StaleDataError`` as the cause.
    """
    dashboard_screenshot_mock.get_screenshot.return_value = b"test-image"

    session = get_sync_session()
    report = _build_report(session, {}, "test stale data error")
    dashboard_id = report.dashboard_id
    owner_id = report.owners[0].id
    report_id = report.id
    try:
        with patch.object(
            session, "commit", side_effect=StaleDataError("test stale data")
        ):
            with pytest.raises(ReportScheduleUnexpectedError) as exc_info:
                ExecuteReportScheduleCommand(
                    str(uuid4()), report_id, datetime.utcnow(), session
                ).run()

            assert exc_info.value.__cause__ is not None
            assert isinstance(exc_info.value.__cause__, StaleDataError)
            assert str(exc_info.value.__cause__) == "test stale data"
    finally:
        # The StaleDataError left the sync session mid-transaction; reset it
        # before cleanup re-uses the thread-local scoped session.
        session.rollback()
        _cleanup(report_id, dashboard_id, owner_id)
