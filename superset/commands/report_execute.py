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
"""Report schedule execution command and state machine.

Ported 1:1 from ``superset_old/commands/report/execute.py``.
Runs **synchronously** inside a Celery worker using a plain
:class:`~sqlalchemy.orm.Session` from
:func:`superset.db.session.get_sync_session`.

Execution flow:
1. ``AsyncExecuteReportScheduleCommand.run()`` loads the report,
   resolves the executor user, and delegates to
   ``ReportScheduleStateMachine``.
2. The state machine picks the correct state handler based on the
   report's ``last_state``.
3. State handlers call ``AlertCommand`` for ALERT type reports,
   generate notification content, and send via email/Slack handlers.

Screenshot/webdriver support is stubbed (TODO) since it requires a
browser runtime.  Notification sending via email and Slack is fully
wired.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Optional
from uuid import UUID

from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from superset.commands.report_alert import AlertCommand
from superset.commands.report_exceptions import (
    ReportScheduleAlertGracePeriodError,
    ReportScheduleClientErrorsException,
    ReportScheduleExecuteUnexpectedError,
    ReportScheduleNotFoundError,
    ReportSchedulePreviousWorkingError,
    ReportScheduleScreenshotFailedError,
    ReportScheduleStateNotFoundError,
    ReportScheduleSystemErrorsException,
    ReportScheduleUnexpectedError,
    ReportScheduleWorkingTimeoutError,
)
from superset.exceptions import (
    CommandException,
    SupersetErrorsException,
    SupersetException,
    UpdateFailedError,
)
from superset.models.reports import (
    ReportDataFormat,
    ReportExecutionLog,
    ReportRecipients,
    ReportRecipientType,
    ReportSchedule,
    ReportScheduleType,
    ReportSourceFormat,
    ReportState,
)
from superset.reports.notifications import create_notification
from superset.reports.notifications.base import NotificationContent
from superset.reports.notifications.exceptions import (
    NotificationError,
    NotificationParamException,
    SlackV1NotificationError,
)
from superset.utils import json

logger = logging.getLogger(__name__)

REPORT_SCHEDULE_ERROR_NOTIFICATION_MARKER = "Notification sent with error"


def _get_settings() -> Any:
    """Load SupersetSettings lazily to avoid circular imports."""
    from superset.config import SupersetSettings

    return SupersetSettings()  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Base report state
# ---------------------------------------------------------------------------


class BaseReportState:
    """Base class for report execution states.

    Ported 1:1 from ``superset_old/commands/report/execute.py::BaseReportState``.
    """

    current_states: list[ReportState] = []
    initial: bool = False

    def __init__(
        self,
        report_schedule: ReportSchedule,
        scheduled_dttm: datetime,
        execution_id: UUID,
        session: Session,
    ) -> None:
        self._report_schedule = report_schedule
        self._scheduled_dttm = scheduled_dttm
        self._start_dttm = datetime.utcnow()
        self._execution_id = execution_id
        self._session = session

    # ------------------------------------------------------------------
    # State / log helpers
    # ------------------------------------------------------------------

    def update_report_schedule_and_log(
        self,
        state: ReportState,
        error_message: Optional[str] = None,
    ) -> None:
        """Update the report schedule state and reflect the change in the
        execution log."""
        self.update_report_schedule(state)
        self.create_log(error_message)

    def update_report_schedule(self, state: ReportState) -> None:
        """Update the report schedule state.

        When the report state is WORKING we must ensure that the values
        from the last execution run are cleared to ensure that they are
        not propagated to the execution log.
        """
        if state == ReportState.WORKING:
            self._report_schedule.last_value = None
            self._report_schedule.last_value_row_json = None

        self._report_schedule.last_state = state
        self._report_schedule.last_eval_dttm = datetime.utcnow()

    def create_log(self, error_message: Optional[str] = None) -> None:
        """Create a Report execution log entry."""
        try:
            log = ReportExecutionLog(
                scheduled_dttm=self._scheduled_dttm,
                start_dttm=self._start_dttm,
                end_dttm=datetime.utcnow(),
                value=self._report_schedule.last_value,
                value_row_json=self._report_schedule.last_value_row_json,
                state=self._report_schedule.last_state,
                error_message=error_message,
                report_schedule=self._report_schedule,
                uuid=self._execution_id,
            )
            self._session.add(log)
            self._session.commit()
        except StaleDataError as ex:
            # Report schedule was modified or deleted by another process
            self._session.rollback()
            logger.warning(
                "Report schedule (execution %s) was modified or deleted "
                "during execution. This can occur when a report is deleted "
                "while running.",
                self._execution_id,
            )
            raise ReportScheduleUnexpectedError(
                "Report schedule was modified or deleted by another process "
                "during execution"
            ) from ex

    # ------------------------------------------------------------------
    # URL helpers
    # ------------------------------------------------------------------

    def _get_url(self, user_friendly: bool = False, **kwargs: Any) -> str:
        """Get the URL for this report schedule: chart or dashboard.

        Uses the settings-based base URL instead of Flask url_for.
        """
        settings = _get_settings()
        base_url = settings.webdriver_baseurl_user_friendly.rstrip("/")

        force = "true" if self._report_schedule.force_screenshot else "false"
        if self._report_schedule.chart:
            return (
                f"{base_url}/explore/?form_data="
                f'{json.dumps({"slice_id": self._report_schedule.chart_id})}'
                f"&force={force}"
            )
        # Dashboard URL
        dashboard = self._report_schedule.dashboard
        if dashboard:
            dashboard_id_or_slug = dashboard.id
            return f"{base_url}/superset/dashboard/{dashboard_id_or_slug}/?force={force}"
        return base_url

    # ------------------------------------------------------------------
    # Content generation
    # ------------------------------------------------------------------

    def _get_screenshots(self) -> list[bytes]:
        """Get chart or dashboard screenshots.

        TODO: Screenshot/webdriver support requires a browser runtime
        (Selenium/Playwright). Stubbed for now.
        """
        raise ReportScheduleScreenshotFailedError(
            "Screenshot generation is not yet implemented in the Litestar backend. "
            "This feature requires webdriver integration."
        )

    def _get_log_data(self) -> dict[str, Any]:
        chart_id = None
        dashboard_id = None
        report_source = None
        slack_channels = None
        if self._report_schedule.chart:
            report_source = ReportSourceFormat.CHART
            chart_id = self._report_schedule.chart_id
        else:
            report_source = ReportSourceFormat.DASHBOARD
            dashboard_id = self._report_schedule.dashboard_id

        if self._report_schedule.recipients:
            slack_channels = [
                recipient.recipient_config_json
                for recipient in self._report_schedule.recipients
                if recipient.type
                in [ReportRecipientType.SLACK, ReportRecipientType.SLACKV2]
            ]

        log_data: dict[str, Any] = {
            "notification_type": self._report_schedule.type,
            "notification_source": report_source,
            "notification_format": self._report_schedule.report_format,
            "chart_id": chart_id,
            "dashboard_id": dashboard_id,
            "owners": self._report_schedule.owners,
            "slack_channels": slack_channels,
            "execution_id": str(self._execution_id),
        }
        return log_data

    def _get_notification_content(self) -> NotificationContent:
        """Get a notification content composed by a title and data.

        :raises: ReportScheduleScreenshotFailedError
        """
        settings = _get_settings()
        csv_data = None
        screenshot_data: list[bytes] = []
        pdf_data = None
        embedded_data = None
        error_text = None
        header_data = self._get_log_data()
        url = self._get_url(user_friendly=True)

        if (
            settings.feature_flags.get("ALERTS_ATTACH_REPORTS", False)
            or self._report_schedule.type == ReportScheduleType.REPORT
        ):
            if self._report_schedule.report_format == ReportDataFormat.VISUALIZATION:
                # TODO: Screenshot generation not yet implemented
                error_text = (
                    "Screenshot generation is not yet available. "
                    "Please use CSV or TEXT format."
                )
            elif self._report_schedule.report_format == ReportDataFormat.DATA:
                # TODO: CSV data generation requires chart endpoint access
                error_text = (
                    "CSV data generation via chart endpoint is not yet available."
                )

            if error_text:
                return NotificationContent(
                    name=self._report_schedule.name,
                    text=error_text,
                    header_data=header_data,
                    url=url,
                )

        if self._report_schedule.email_subject:
            name = self._report_schedule.email_subject
        else:
            if self._report_schedule.chart:
                name = (
                    f"{self._report_schedule.name}: "
                    f"{self._report_schedule.chart.slice_name}"
                )
            else:
                name = (
                    f"{self._report_schedule.name}: "
                    f"{self._report_schedule.dashboard.dashboard_title}"
                    if self._report_schedule.dashboard
                    else self._report_schedule.name
                )

        return NotificationContent(
            name=name,
            url=url,
            screenshots=screenshot_data if screenshot_data else None,
            pdf=pdf_data,
            description=self._report_schedule.description,
            csv=csv_data,
            embedded_data=embedded_data,
            header_data=header_data,
        )

    # ------------------------------------------------------------------
    # Notification sending
    # ------------------------------------------------------------------

    def _send(
        self,
        notification_content: NotificationContent,
        recipients: list[ReportRecipients],
    ) -> None:
        """Send a notification to all recipients.

        :raises: CommandException
        """
        settings = _get_settings()
        notification_errors: list[dict[str, Any]] = []
        for recipient in recipients:
            notification = create_notification(recipient, notification_content)
            try:
                try:
                    if settings.alert_reports_notification_dry_run:
                        logger.info(
                            "Would send notification for alert %s, to %s. "
                            "ALERT_REPORTS_NOTIFICATION_DRY_RUN is enabled, "
                            "set it to False to send notifications.",
                            self._report_schedule.name,
                            recipient.recipient_config_json,
                        )
                    else:
                        notification.send()
                except SlackV1NotificationError as ex:
                    # The slack notification should be sent with the v2 api
                    logger.info(
                        "Attempting to upgrade the report to Slackv2: %s", str(ex)
                    )
                    # Upgrade recipient to v2
                    recipient.type = ReportRecipientType.SLACKV2
                    notification = create_notification(recipient, notification_content)
                    notification.send()
            except (
                UpdateFailedError,
                NotificationParamException,
                NotificationError,
                SupersetException,
            ) as ex:
                # collect errors but keep processing them
                notification_errors.append(
                    {
                        "message": ex.message,
                        "error_type": "REPORT_NOTIFICATION_ERROR",
                        "level": "error" if ex.status_code >= 500 else "warning",
                    }
                )
        if notification_errors:
            # log all errors but raise based on the most severe
            for error in notification_errors:
                logger.warning(str(error))

            if any(error["level"] == "error" for error in notification_errors):
                raise ReportScheduleSystemErrorsException(
                    message=";".join(e["message"] for e in notification_errors)
                )
            if any(error["level"] == "warning" for error in notification_errors):
                raise ReportScheduleClientErrorsException(
                    message=";".join(e["message"] for e in notification_errors)
                )

    def send(self) -> None:
        """Create the notification content and send to all recipients.

        :raises: CommandException
        """
        notification_content = self._get_notification_content()
        self._send(notification_content, self._report_schedule.recipients)

    def send_error(self, name: str, message: str) -> None:
        """Create and send an error notification to all owners.

        :raises: CommandException
        """
        header_data = self._get_log_data()
        url = self._get_url(user_friendly=True)
        logger.info(
            "header_data in notifications for alerts and reports %s, taskid, %s",
            header_data,
            self._execution_id,
        )
        notification_content = NotificationContent(
            name=name, text=message, header_data=header_data, url=url
        )

        # filter recipients to recipients who are also owners
        owner_recipients = [
            ReportRecipients(
                type=ReportRecipientType.EMAIL,
                recipient_config_json=json.dumps({"target": owner.email}),
            )
            for owner in self._report_schedule.owners
        ]

        self._send(notification_content, owner_recipients)

    # ------------------------------------------------------------------
    # Grace period checks
    # ------------------------------------------------------------------

    def is_in_grace_period(self) -> bool:
        """Check if an alert is in its grace period."""
        last_success = self._find_last_success_log()
        return (
            last_success is not None
            and self._report_schedule.grace_period
            and datetime.utcnow()
            - timedelta(seconds=self._report_schedule.grace_period)
            < last_success.end_dttm
        )

    def is_in_error_grace_period(self) -> bool:
        """Check if an alert/report on error is in its notification
        grace period."""
        last_error_log = self._find_last_error_notification()
        if not last_error_log:
            return False
        return (
            last_error_log is not None
            and self._report_schedule.grace_period
            and datetime.utcnow()
            - timedelta(seconds=self._report_schedule.grace_period)
            < last_error_log.end_dttm
        )

    def is_on_working_timeout(self) -> bool:
        """Check if an alert is in a working timeout."""
        last_working = self._find_last_entered_working_log()
        if not last_working:
            return False
        return (
            self._report_schedule.working_timeout is not None
            and self._report_schedule.last_eval_dttm is not None
            and datetime.utcnow()
            - timedelta(seconds=self._report_schedule.working_timeout)
            > last_working.end_dttm
        )

    # ------------------------------------------------------------------
    # DAO-style queries (inline, using self._session)
    # ------------------------------------------------------------------

    def _find_last_success_log(self) -> ReportExecutionLog | None:
        return (
            self._session.query(ReportExecutionLog)
            .filter(
                ReportExecutionLog.state == ReportState.SUCCESS,
                ReportExecutionLog.report_schedule == self._report_schedule,
            )
            .order_by(ReportExecutionLog.end_dttm.desc())
            .first()
        )

    def _find_last_entered_working_log(self) -> ReportExecutionLog | None:
        return (
            self._session.query(ReportExecutionLog)
            .filter(
                ReportExecutionLog.state == ReportState.WORKING,
                ReportExecutionLog.report_schedule == self._report_schedule,
                ReportExecutionLog.error_message.is_(None),
            )
            .order_by(ReportExecutionLog.end_dttm.desc())
            .first()
        )

    def _find_last_error_notification(self) -> ReportExecutionLog | None:
        last_error_email_log = (
            self._session.query(ReportExecutionLog)
            .filter(
                ReportExecutionLog.error_message
                == REPORT_SCHEDULE_ERROR_NOTIFICATION_MARKER,
                ReportExecutionLog.report_schedule == self._report_schedule,
            )
            .order_by(ReportExecutionLog.end_dttm.desc())
            .first()
        )
        if not last_error_email_log:
            return None
        # Checks that only errors have occurred since the last email
        report_from_last_email = (
            self._session.query(ReportExecutionLog)
            .filter(
                ReportExecutionLog.state.notin_(
                    [ReportState.ERROR, ReportState.WORKING]
                ),
                ReportExecutionLog.report_schedule == self._report_schedule,
                ReportExecutionLog.end_dttm < last_error_email_log.end_dttm,
            )
            .order_by(ReportExecutionLog.end_dttm.desc())
            .first()
        )
        return last_error_email_log if not report_from_last_email else None

    def next(self) -> None:
        raise NotImplementedError()


# ---------------------------------------------------------------------------
# Concrete state handlers
# ---------------------------------------------------------------------------


class ReportNotTriggeredErrorState(BaseReportState):
    """Handle Not triggered and Error state.

    Next final states: Not Triggered, Success, Error.
    """

    current_states = [ReportState.NOOP, ReportState.ERROR]
    initial = True

    def next(self) -> None:  # noqa: C901
        self.update_report_schedule_and_log(ReportState.WORKING)
        try:
            # If it's an alert check if the alert is triggered
            if self._report_schedule.type == ReportScheduleType.ALERT:
                if not AlertCommand(
                    self._report_schedule, self._execution_id, self._session
                ).run():
                    self.update_report_schedule_and_log(ReportState.NOOP)
                    return
            self.send()
            self.update_report_schedule_and_log(ReportState.SUCCESS)
        except (SupersetErrorsException, Exception) as first_ex:
            error_message = str(first_ex)
            if isinstance(first_ex, SupersetErrorsException):
                error_message = ";".join(
                    [str(error) for error in first_ex.errors]
                )

            try:
                self.update_report_schedule_and_log(
                    ReportState.ERROR, error_message=error_message
                )
            except ReportScheduleUnexpectedError as logging_ex:
                # Logging failed (likely StaleDataError), but we still want to
                # raise the original error so the root cause remains visible
                logger.warning(
                    "Failed to log error for report schedule (execution %s) "
                    "due to database issue",
                    self._execution_id,
                    exc_info=True,
                )
                # Re-raise the original exception, not the logging failure
                raise first_ex from logging_ex

            # TODO (dpgaspar) convert this logic to a new state eg: ERROR_ON_GRACE
            if not self.is_in_error_grace_period():
                second_error_message = REPORT_SCHEDULE_ERROR_NOTIFICATION_MARKER
                try:
                    self.send_error(
                        f"Error occurred for {self._report_schedule.type}:"
                        f" {self._report_schedule.name}",
                        str(first_ex),
                    )

                except SupersetErrorsException as second_ex:
                    second_error_message = ";".join(
                        [str(error) for error in second_ex.errors]
                    )
                except ReportScheduleUnexpectedError:
                    # send_error failed due to logging issue, log and continue
                    # to raise the original error
                    logger.warning(
                        "Failed to send error notification due to database issue",
                        exc_info=True,
                    )
                except Exception as second_ex:  # noqa: BLE001
                    second_error_message = str(second_ex)
                finally:
                    try:
                        self.update_report_schedule_and_log(
                            ReportState.ERROR, error_message=second_error_message
                        )
                    except ReportScheduleUnexpectedError:
                        # Logging failed again, log it but don't let it
                        # hide first_ex
                        logger.warning(
                            "Failed to log final error state due to database issue",
                            exc_info=True,
                        )
            raise


class ReportWorkingState(BaseReportState):
    """Handle Working state.

    Next states: Error, Working.
    """

    current_states = [ReportState.WORKING]

    def next(self) -> None:
        if self.is_on_working_timeout():
            exception_timeout = ReportScheduleWorkingTimeoutError()
            self.update_report_schedule_and_log(
                ReportState.ERROR,
                error_message=str(exception_timeout),
            )
            raise exception_timeout
        exception_working = ReportSchedulePreviousWorkingError()
        self.update_report_schedule_and_log(
            ReportState.WORKING,
            error_message=str(exception_working),
        )
        raise exception_working


class ReportSuccessState(BaseReportState):
    """Handle Success, Grace state.

    Next states: Grace, Not triggered, Success.
    """

    current_states = [ReportState.SUCCESS, ReportState.GRACE]

    def next(self) -> None:
        if self._report_schedule.type == ReportScheduleType.ALERT:
            if self.is_in_grace_period():
                self.update_report_schedule_and_log(
                    ReportState.GRACE,
                    error_message=str(ReportScheduleAlertGracePeriodError()),
                )
                return
            self.update_report_schedule_and_log(ReportState.WORKING)
            try:
                if not AlertCommand(
                    self._report_schedule, self._execution_id, self._session
                ).run():
                    self.update_report_schedule_and_log(ReportState.NOOP)
                    return
            except Exception as ex:
                self.send_error(
                    f"Error occurred for {self._report_schedule.type}:"
                    f" {self._report_schedule.name}",
                    str(ex),
                )
                self.update_report_schedule_and_log(
                    ReportState.ERROR,
                    error_message=REPORT_SCHEDULE_ERROR_NOTIFICATION_MARKER,
                )
                raise

        try:
            self.send()
            self.update_report_schedule_and_log(ReportState.SUCCESS)
        except Exception as ex:  # noqa: BLE001
            try:
                self.update_report_schedule_and_log(
                    ReportState.ERROR, error_message=str(ex)
                )
            except ReportScheduleUnexpectedError as logging_ex:
                # Logging failed (likely StaleDataError), but we still want to
                # raise the original error so the root cause remains visible
                logger.warning(
                    "Failed to log error for report schedule (execution %s) "
                    "due to database issue",
                    self._execution_id,
                    exc_info=True,
                )
                # Re-raise the original exception, not the logging failure
                raise ex from logging_ex
            raise


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


class ReportScheduleStateMachine:
    """Simple state machine for Alerts/Reports states.

    Ported 1:1 from ``superset_old/commands/report/execute.py``.
    """

    states_cls = [ReportWorkingState, ReportNotTriggeredErrorState, ReportSuccessState]

    def __init__(
        self,
        task_uuid: UUID,
        report_schedule: ReportSchedule,
        scheduled_dttm: datetime,
        session: Session,
    ):
        self._execution_id = task_uuid
        self._report_schedule = report_schedule
        self._scheduled_dttm = scheduled_dttm
        self._session = session

    def run(self) -> None:
        for state_cls in self.states_cls:
            if (self._report_schedule.last_state is None and state_cls.initial) or (
                self._report_schedule.last_state in state_cls.current_states
            ):
                state_cls(
                    self._report_schedule,
                    self._scheduled_dttm,
                    self._execution_id,
                    self._session,
                ).next()
                break
        else:
            raise ReportScheduleStateNotFoundError()


# ---------------------------------------------------------------------------
# Top-level execution command (called from Celery task)
# ---------------------------------------------------------------------------


class ExecuteReportScheduleCommand:
    """Execute all types of report schedules.

    - On reports: takes chart or dashboard screenshots and sends
      configured notifications.
    - On alerts: uses ``AlertCommand`` and sends configured notifications.

    Ported 1:1 from
    ``superset_old/commands/report/execute.py::AsyncExecuteReportScheduleCommand``.
    """

    def __init__(
        self,
        task_id: str,
        model_id: int,
        scheduled_dttm: datetime,
        session: Session,
    ):
        self._model_id = model_id
        self._model: Optional[ReportSchedule] = None
        self._scheduled_dttm = scheduled_dttm
        self._execution_id = UUID(task_id)
        self._session = session

    def run(self) -> None:
        try:
            self.validate()
            if not self._model:
                raise ReportScheduleExecuteUnexpectedError()

            logger.info(
                "Running report schedule %s (id=%s)",
                self._execution_id,
                self._model_id,
            )
            ReportScheduleStateMachine(
                self._execution_id,
                self._model,
                self._scheduled_dttm,
                self._session,
            ).run()
        except CommandException:
            raise
        except Exception as ex:
            raise ReportScheduleUnexpectedError(str(ex)) from ex

    def validate(self) -> None:
        """Validate/populate model exists."""
        logger.info(
            "session is validated: id %s, executionid: %s",
            self._model_id,
            self._execution_id,
        )
        self._model = (
            self._session.query(ReportSchedule)
            .filter_by(id=self._model_id)
            .one_or_none()
        )
        if not self._model:
            raise ReportScheduleNotFoundError()
