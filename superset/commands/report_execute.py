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
# mypy: ignore-errors
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

Screenshot/webdriver support is wired to ``ChartScreenshot`` and
``DashboardScreenshot`` from ``superset.utils.screenshots``.
CSV/DataFrame data generation uses ``get_chart_csv_data`` and
``get_chart_dataframe`` from ``superset.utils.csv``.
Notification sending via email and Slack is fully wired.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Optional, Union
from uuid import UUID, uuid3

import pandas as pd
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from superset.commands.report_alert import AlertCommand
from superset.commands.report_exceptions import (
    ReportScheduleAlertGracePeriodError,
    ReportScheduleClientErrorsException,
    ReportScheduleCsvFailedError,
    ReportScheduleCsvTimeout,
    ReportScheduleDataFrameFailedError,
    ReportScheduleDataFrameTimeout,
    ReportScheduleExecuteUnexpectedError,
    ReportScheduleNotFoundError,
    ReportSchedulePreviousWorkingError,
    ReportScheduleScreenshotFailedError,
    ReportScheduleScreenshotTimeout,
    ReportScheduleStateNotFoundError,
    ReportScheduleSystemErrorsException,
    ReportScheduleUnexpectedError,
    ReportScheduleWorkingTimeoutError,
)
from superset.errors import ErrorLevel, SupersetError, SupersetErrorType
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
from superset.models.security import User
from superset.reports.notifications import create_notification
from superset.reports.notifications.base import NotificationContent
from superset.reports.notifications.exceptions import (
    NotificationError,
    NotificationParamException,
    SlackV1NotificationError,
)
from superset.tasks.utils import get_executor
from superset.utils import json
from superset.utils.core import override_user
from superset.utils.csv import get_chart_csv_data, get_chart_dataframe
from superset.utils.decorators import transaction
from superset.utils.pdf import build_pdf_from_screenshots
from superset.utils.screenshots import ChartScreenshot, DashboardScreenshot

logger = logging.getLogger(__name__)

REPORT_SCHEDULE_ERROR_NOTIFICATION_MARKER = "Notification sent with error"


def _get_settings() -> Any:
    """Load SupersetSettings lazily to avoid circular imports."""
    from superset.config import SupersetSettings

    return SupersetSettings()  # type: ignore[call-arg]


def _recipients_string_to_list(address_string: str | None) -> list[str]:
    """Split a comma/semicolon/whitespace separated string into a list.

    1:1 port of ``superset_old/utils/core.py::recipients_string_to_list``.
    """
    import re

    address_string_list: list[str] = []
    if isinstance(address_string, str):
        address_string_list = re.split(r",|\s|;", address_string)
    return [x.strip() for x in address_string_list if x.strip()]


# ---------------------------------------------------------------------------
# Base report state
# ---------------------------------------------------------------------------


class BaseReportState:
    """Base class for report execution states.

    Ported 1:1 from ``:BaseReportState`` in
    ``superset_old/commands/report/execute.py``.
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

    def update_report_schedule_slack_v2(self) -> None:
        """Upgrade all Slack recipients to v2 (channel names → ids).

        1:1 port of
        ``superset_old/commands/report/execute.py::update_report_schedule_slack_v2``.
        V2 uses channel **ids** instead of names, so for every ``SLACK``
        recipient we resolve the configured channel name(s) to id(s) via
        ``get_channels_with_search`` (ported as
        :func:`superset.controllers.report._get_slack_channels`), validate that
        every requested channel was found, rewrite ``recipient_config_json`` to
        ``{"target": "<ids>"}``, and flip the type to ``SLACKV2``. On any
        failure the recipient is reverted to ``SLACK`` and ``UpdateFailedError``
        is raised.
        """
        from superset.controllers.report import _get_slack_channels

        recipient: Any = None
        try:
            for recipient in self._report_schedule.recipients:
                if recipient.type == ReportRecipientType.SLACK:
                    recipient.type = ReportRecipientType.SLACKV2
                    slack_recipients = json.loads(recipient.recipient_config_json)
                    # V1 method allowed a leading ``#`` in the channel name
                    channel_names = (slack_recipients["target"] or "").replace("#", "")
                    # ensure existing reports can also fetch ids from private
                    # channels (exact match on both public and private)
                    channels = _get_slack_channels(
                        search_string=channel_names,
                        types=["private_channel", "public_channel"],
                        exact_match=True,
                    )
                    channels_list = _recipients_string_to_list(channel_names)
                    if len(channels_list) != len(channels):
                        missing_channels = set(channels_list) - {
                            channel["name"] for channel in channels
                        }
                        msg = (
                            "Could not find the following channels: "
                            f"{', '.join(missing_channels)}"
                        )
                        raise UpdateFailedError(msg)
                    channel_ids = ",".join(channel["id"] for channel in channels)
                    recipient.recipient_config_json = json.dumps(
                        {
                            "target": channel_ids,
                        }
                    )
        except Exception as ex:
            # Revert to v1 to preserve configuration (requires manual fix)
            if recipient is not None:
                recipient.type = ReportRecipientType.SLACK
            msg = f"Failed to update slack recipients to v2: {ex!s}"
            logger.exception(msg)
            raise UpdateFailedError(msg) from ex

    # ------------------------------------------------------------------
    # URL helpers
    # ------------------------------------------------------------------

    def _get_url(
        self,
        user_friendly: bool = False,
        result_format: str | None = None,
        **kwargs: Any,
    ) -> str:
        """Get the URL for this report schedule: chart or dashboard.

        1:1 port of ``superset_old/commands/report/execute.py::_get_url``.
        Uses :func:`superset.utils.urls.get_url_path` (the Liteset replacement
        for Flask ``url_for``).  For dashboards with stateful tab anchors and
        the ``ALERT_REPORT_TABS`` feature enabled, returns a permalink URL.

        :param result_format: If ``"csv"`` or ``"json"``, return the chart
            data API endpoint instead of the explore view.
        """
        from superset.utils.urls import get_url_path

        force = "true" if self._report_schedule.force_screenshot else "false"
        if self._report_schedule.chart:
            if result_format in {"csv", "json"}:
                return get_url_path(
                    "ChartDataRestApi.get_data",
                    pk=self._report_schedule.chart_id,
                    format=result_format,
                    type="post_processed",
                    force=force,
                )
            return get_url_path(
                "ExploreView.root",
                user_friendly=user_friendly,
                form_data=json.dumps({"slice_id": self._report_schedule.chart_id}),
                force=force,
                **kwargs,
            )
        # If we need to render dashboard in a specific state, use stateful
        # permalink — 1:1 with execute.py:239-243.
        if (
            dashboard_state := self._report_schedule.extra.get("dashboard")
        ) and self._is_feature_enabled("ALERT_REPORT_TABS"):
            return self._get_tab_url(dashboard_state, user_friendly=user_friendly)

        dashboard = self._report_schedule.dashboard
        dashboard_id_or_slug = (
            dashboard.uuid if dashboard and dashboard.uuid else dashboard.id
        )
        return get_url_path(
            "Superset.dashboard",
            user_friendly=user_friendly,
            dashboard_id_or_slug=dashboard_id_or_slug,
            force=force,
            **kwargs,
        )

    @staticmethod
    def _is_feature_enabled(name: str) -> bool:
        """Check a feature flag from settings (sync-context safe)."""
        settings = _get_settings()
        return bool(settings.feature_flags.get(name, False))

    def get_dashboard_urls(
        self, user_friendly: bool = False, **kwargs: Any
    ) -> list[str]:
        """Retrieve the URL(s) for the dashboard tabs, or the single dashboard
        URL when no tabs are configured.

        1:1 port of ``execute.py::get_dashboard_urls``.
        """
        from superset.utils.urls import get_url_path

        force = "true" if self._report_schedule.force_screenshot else "false"
        if (
            dashboard_state := self._report_schedule.extra.get("dashboard")
        ) and self._is_feature_enabled("ALERT_REPORT_TABS"):
            if anchor := dashboard_state.get("anchor"):
                try:
                    anchor_list: list[str] = json.loads(anchor)
                    return self._get_tabs_urls(anchor_list, user_friendly=user_friendly)
                except json.JSONDecodeError:
                    logger.debug("Anchor value is not a list, Fall back to single tab")
            return [self._get_tab_url(dashboard_state)]

        dashboard = self._report_schedule.dashboard
        dashboard_id_or_slug = (
            dashboard.uuid if dashboard and dashboard.uuid else dashboard.id
        )

        return [
            get_url_path(
                "Superset.dashboard",
                user_friendly=user_friendly,
                dashboard_id_or_slug=dashboard_id_or_slug,
                force=force,
                **kwargs,
            )
        ]

    def _get_tab_url(
        self, dashboard_state: dict[str, Any], user_friendly: bool = False
    ) -> str:
        """Build a single stateful dashboard-tab permalink URL.

        1:1 port of ``execute.py::_get_tab_url``. The async
        ``CreateDashboardPermalinkCommand`` is reimplemented synchronously
        here (see :meth:`_create_dashboard_permalink`) so it can run inside
        the synchronous Celery execution context.
        """
        from superset.utils.urls import get_url_path

        permalink_key = self._create_dashboard_permalink(
            dashboard_uuid=str(self._report_schedule.dashboard.uuid),
            state=dashboard_state,
        )
        return get_url_path(
            "Superset.dashboard_permalink",
            key=permalink_key,
            user_friendly=user_friendly,
        )

    def _get_tabs_urls(
        self, tab_anchors: list[str], user_friendly: bool = False
    ) -> list[str]:
        """Build permalink URLs for multiple dashboard tabs.

        1:1 port of ``execute.py::_get_tabs_urls``.
        """
        return [
            self._get_tab_url(
                {
                    "anchor": tab_anchor,
                    "dataMask": None,
                    "activeTabs": None,
                    "urlParams": None,
                },
                user_friendly=user_friendly,
            )
            for tab_anchor in tab_anchors
        ]

    def _create_dashboard_permalink(
        self, dashboard_uuid: str, state: dict[str, Any]
    ) -> str:
        """Get-or-create a dashboard permalink synchronously.

        Replicates ``CreateDashboardPermalinkCommand.run`` (the async command
        at ``superset/commands/dashboard/permalink/create.py``) against the
        synchronous Celery ``Session``: deterministic upsert keyed by
        ``uuid3(salt, (user_id, payload))`` so the same state for the same user
        reuses the same row, then hashids-encodes the integer id.
        """
        from superset.key_value.shared_entries import NAMESPACE, RESOURCE
        from superset.key_value.types import KeyValueResource, SharedKey
        from superset.key_value.utils import (
            encode_permalink_key,
            get_deterministic_uuid,
            random_key,
        )
        from superset.models.key_value import KeyValueEntry
        from superset.utils.core import get_user_id

        # --- get-or-create the permalink hashing salt (sync) ---
        salt_uuid = uuid3(NAMESPACE, SharedKey.DASHBOARD_PERMALINK_SALT.value)
        salt_entry = (
            self._session.query(KeyValueEntry)
            .filter(
                KeyValueEntry.resource == RESOURCE.value,
                KeyValueEntry.uuid == salt_uuid,
            )
            .one_or_none()
        )
        if salt_entry is None:
            salt = random_key(48)
            self._session.add(
                KeyValueEntry(
                    resource=RESOURCE.value,
                    uuid=salt_uuid,
                    value=json.dumps(salt).encode("utf-8"),
                    created_on=datetime.utcnow(),
                )
            )
            self._session.flush()
        else:
            salt = json.loads(salt_entry.value.decode("utf-8"))

        # --- deterministic upsert of the permalink entry ---
        user_id = get_user_id()
        payload = {"dashboardId": dashboard_uuid, "state": state}
        deterministic_uuid = get_deterministic_uuid(salt, (user_id, payload))
        encoded = json.dumps(payload).encode("utf-8")

        existing = (
            self._session.query(KeyValueEntry)
            .filter(
                KeyValueEntry.resource == KeyValueResource.DASHBOARD_PERMALINK.value,
                KeyValueEntry.uuid == deterministic_uuid,
            )
            .one_or_none()
        )
        if existing is None:
            entry = KeyValueEntry(
                resource=KeyValueResource.DASHBOARD_PERMALINK.value,
                uuid=deterministic_uuid,
                value=encoded,
                created_on=datetime.utcnow(),
            )
            self._session.add(entry)
            self._session.flush()
        else:
            existing.value = encoded
            entry = existing

        if entry.id is None:
            raise RuntimeError("Permalink entry missing autogenerated id")
        return encode_permalink_key(key=int(entry.id), salt=salt)

    # ------------------------------------------------------------------
    # Content generation
    # ------------------------------------------------------------------

    def _find_user(self, username: str) -> User | None:
        """Find a user by username using the sync session."""
        return self._session.query(User).filter(User.username == username).one_or_none()

    @staticmethod
    def _get_auth_cookies(user: User | None) -> dict[str, str] | None:
        """Get authentication cookies for the given user.

        Uses the Liteset ``machine_auth_provider_factory`` which is
        initialised by ``superset.app.on_startup`` (and must be wired up
        equivalently inside the Celery worker — see the worker boot
        module).  Falls back to ``superset_old`` for transitional
        compatibility while the Celery worker is still on Flask.
        """
        try:
            from superset.extensions import machine_auth_provider_factory

            return machine_auth_provider_factory.instance.get_auth_cookies(user)
        except (ImportError, AttributeError, RuntimeError):
            try:
                from superset_old.extensions import (  # type: ignore[import-not-found]
                    machine_auth_provider_factory as _legacy_factory,
                )

                return _legacy_factory.instance.get_auth_cookies(user)
            except (ImportError, AttributeError):
                logger.warning(
                    "machine_auth_provider_factory not available; "
                    "CSV/DataFrame data fetching may fail."
                )
                return None

    def _get_screenshots(self) -> list[bytes]:
        """Get chart or dashboard screenshots.

        :raises: ReportScheduleScreenshotFailedError
        :raises: ReportScheduleScreenshotTimeout
        """
        settings = _get_settings()

        _, username = get_executor(
            executors=settings.alert_reports_executors,
            model=self._report_schedule,
        )
        user = self._find_user(username)

        max_width = settings.alert_reports_max_custom_screenshot_width

        if self._report_schedule.chart:
            url = self._get_url()

            window_width, window_height = settings.webdriver_window["slice"]
            width = min(
                max_width,
                self._report_schedule.custom_width or window_width,
            )
            height = self._report_schedule.custom_height or window_height
            window_size = (width, height)

            screenshots: list[Union[ChartScreenshot, DashboardScreenshot]] = [
                ChartScreenshot(
                    url,
                    self._report_schedule.chart.digest,
                    window_size=window_size,
                    thumb_size=settings.webdriver_window["slice"],
                )
            ]
        else:
            urls = self.get_dashboard_urls()

            window_width, window_height = settings.webdriver_window["dashboard"]
            width = min(
                max_width,
                self._report_schedule.custom_width or window_width,
            )
            height = self._report_schedule.custom_height or window_height
            window_size = (width, height)

            screenshots = [
                DashboardScreenshot(
                    url,
                    self._report_schedule.dashboard.digest,
                    window_size=window_size,
                    thumb_size=settings.webdriver_window["dashboard"],
                )
                for url in urls
            ]

        try:
            images: list[bytes] = []
            for screenshot in screenshots:
                if image := screenshot.get_screenshot(user=user):
                    images.append(image)
        except SoftTimeLimitExceeded as ex:
            logger.warning("A timeout occurred while taking a screenshot.")
            raise ReportScheduleScreenshotTimeout() from ex
        except Exception as ex:
            raise ReportScheduleScreenshotFailedError(
                f"Failed taking a screenshot {ex!s}"
            ) from ex
        if not images:
            raise ReportScheduleScreenshotFailedError()
        return images

    def _get_pdf(self) -> bytes:
        """Get chart or dashboard as PDF.

        :raises: ReportScheduleScreenshotFailedError
        """
        screenshots = self._get_screenshots()
        return build_pdf_from_screenshots(screenshots)

    def _get_csv_data(self) -> bytes:
        """Get chart data as CSV bytes.

        :raises: ReportScheduleCsvFailedError
        :raises: ReportScheduleCsvTimeout
        """
        settings = _get_settings()
        url = self._get_url(result_format="csv")
        _, username = get_executor(
            executors=settings.alert_reports_executors,
            model=self._report_schedule,
        )
        user = self._find_user(username)

        auth_cookies = self._get_auth_cookies(user)

        if self._report_schedule.chart.query_context is None:
            logger.warning("No query context found, taking a screenshot to generate it")
            self._update_query_context()

        try:
            logger.info(
                "Getting chart from %s as user %s",
                url,
                username,
            )
            csv_data = get_chart_csv_data(chart_url=url, auth_cookies=auth_cookies)
        except SoftTimeLimitExceeded as ex:
            raise ReportScheduleCsvTimeout() from ex
        except Exception as ex:
            raise ReportScheduleCsvFailedError(f"Failed generating csv {ex!s}") from ex
        if not csv_data:
            raise ReportScheduleCsvFailedError()
        return csv_data

    def _get_embedded_data(self) -> pd.DataFrame:
        """Return data as a Pandas dataframe, to embed in notifications
        as a table.

        :raises: ReportScheduleDataFrameFailedError
        :raises: ReportScheduleDataFrameTimeout
        """
        settings = _get_settings()
        url = self._get_url(result_format="json")
        _, username = get_executor(
            executors=settings.alert_reports_executors,
            model=self._report_schedule,
        )
        user = self._find_user(username)

        auth_cookies = self._get_auth_cookies(user)

        if self._report_schedule.chart.query_context is None:
            logger.warning("No query context found, taking a screenshot to generate it")
            self._update_query_context()

        try:
            logger.info(
                "Getting chart from %s as user %s",
                url,
                username,
            )
            dataframe = get_chart_dataframe(url, auth_cookies)
        except SoftTimeLimitExceeded as ex:
            raise ReportScheduleDataFrameTimeout() from ex
        except Exception as ex:
            raise ReportScheduleDataFrameFailedError(
                f"Failed generating dataframe {ex!s}"
            ) from ex
        if dataframe is None:
            raise ReportScheduleCsvFailedError()
        return dataframe

    def _update_query_context(self) -> None:
        """Update chart query context.

        To load CSV data from the endpoint the chart must have been saved
        with its query context.  For charts without saved query context we
        get a screenshot to force the chart to produce and save the query
        context.
        """
        try:
            self._get_screenshots()
        except (
            ReportScheduleScreenshotFailedError,
            ReportScheduleScreenshotTimeout,
        ) as ex:
            raise ReportScheduleCsvFailedError(
                "Unable to fetch data because the chart has no query context "
                "saved, and an error occurred when fetching it via a screenshot. "
                "Please try loading the chart and saving it again."
            ) from ex

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

    def _get_notification_content(self) -> NotificationContent:  # noqa: C901
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
                screenshot_data = self._get_screenshots()
                if not screenshot_data:
                    error_text = "Unexpected missing screenshot"
            elif self._report_schedule.report_format == ReportDataFormat.PDF:
                pdf_data = self._get_pdf()
                if not pdf_data:
                    error_text = "Unexpected missing pdf"
            elif (
                self._report_schedule.chart
                and self._report_schedule.report_format == ReportDataFormat.DATA
            ):
                csv_data = self._get_csv_data()
                if not csv_data:
                    error_text = "Unexpected missing csv file"
            if error_text:
                return NotificationContent(
                    name=self._report_schedule.name,
                    text=error_text,
                    header_data=header_data,
                    url=url,
                )

        if (
            self._report_schedule.chart
            and self._report_schedule.report_format == ReportDataFormat.TEXT
        ):
            embedded_data = self._get_embedded_data()

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
            screenshots=screenshot_data,
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
        notification_errors: list[SupersetError] = []
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
                    # The slack notification should be sent with the v2 api.
                    # Resolve channel names → ids and persist the SLACKV2 type
                    # before retrying — 1:1 with execute.py:603-611.
                    logger.info(
                        "Attempting to upgrade the report to Slackv2: %s", str(ex)
                    )
                    self.update_report_schedule_slack_v2()
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
                    SupersetError(
                        message=ex.message,
                        error_type=SupersetErrorType.REPORT_NOTIFICATION_ERROR,
                        level=(
                            ErrorLevel.ERROR
                            if ex.status_code >= 500
                            else ErrorLevel.WARNING
                        ),
                    )
                )
        if notification_errors:
            # log all errors but raise based on the most severe
            for error in notification_errors:
                logger.warning(str(error))

            if any(error.level == ErrorLevel.ERROR for error in notification_errors):
                _exc = ReportScheduleSystemErrorsException(
                    message=";".join(e.message for e in notification_errors)
                )
                _exc.errors = notification_errors
                raise _exc
            if any(error.level == ErrorLevel.WARNING for error in notification_errors):
                _exc = ReportScheduleClientErrorsException(
                    message=";".join(e.message for e in notification_errors)
                )
                _exc.errors = notification_errors
                raise _exc

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
            # Join the structured per-error messages when present; the
            # ``errors`` payload may be ``SupersetError`` objects OR dicts.
            # Fall back to ``str(first_ex)`` when the list is empty so the
            # ERROR log row never gets a BLANK error_message (the message
            # string carries the joined reasons even when ``errors`` is unset).
            if isinstance(first_ex, SupersetErrorsException) and first_ex.errors:
                error_message = ";".join(
                    e.get("message", str(e))
                    if isinstance(e, dict)
                    else str(getattr(e, "message", e))
                    for e in first_ex.errors
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
                        [error.message for error in second_ex.errors]
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

    @transaction()
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

    @transaction()
    def run(self) -> None:
        try:
            self.validate()
            if not self._model:
                raise ReportScheduleExecuteUnexpectedError()

            # Resolve the executor user and run the whole state machine under
            # ``override_user`` so permalink creation, RLS and audit fields all
            # share the executor context — 1:1 with
            # ``superset_old/commands/report/execute.py:943-956``.
            settings = _get_settings()
            _, username = get_executor(
                executors=settings.alert_reports_executors,
                model=self._model,
            )
            user = (
                self._session.query(User)
                .filter(User.username == username)
                .one_or_none()
            )
            with override_user(user):
                logger.info(
                    "Running report schedule %s as user %s",
                    self._execution_id,
                    username,
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
