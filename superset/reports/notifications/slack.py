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
from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from io import IOBase
from typing import Any, Union

import backoff
from slack_sdk import WebClient
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
from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler

from superset.models.reports import ReportRecipientType
from superset.reports.notifications.base import (
    BaseNotification,
    NotificationContent,
)
from superset.reports.notifications.exceptions import (
    NotificationAuthorizationException,
    NotificationMalformedException,
    NotificationParamException,
    NotificationUnprocessableException,
    SlackV1NotificationError,
)
from superset.reports.notifications.slack_mixin import SlackMixin
from superset.utils import json
from superset.utils.feature_flags import feature_flag_manager

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helper: recipients_string_to_list  (ported from superset.utils.core)
# ---------------------------------------------------------------------------
def _recipients_string_to_list(address_string: str | None) -> list[str]:
    """
    Returns the list of target recipients for alerts and reports.

    Strips values and converts a comma/semicolon separated
    string into a list.
    """
    address_string_list: list[str] = []
    if isinstance(address_string, str):
        address_string_list = re.split(r",|\s|;", address_string)
    return [x.strip() for x in address_string_list if x.strip()]


# ---------------------------------------------------------------------------
# Helper: get_slack_client (ported from superset.utils.slack, no Flask)
# ---------------------------------------------------------------------------
def _get_slack_client(config: dict[str, Any]) -> WebClient:
    """Build a Slack WebClient from config dict (no Flask dependency)."""
    token: Any = config.get("SLACK_API_TOKEN")
    if callable(token):
        token = token()
    proxy: str | None = config.get("SLACK_PROXY")
    client = WebClient(token=token, proxy=proxy)

    max_retry_count: int = config.get("SLACK_API_RATE_LIMIT_RETRY_COUNT", 2)
    rate_limit_handler = RateLimitErrorRetryHandler(max_retry_count=max_retry_count)
    client.retry_handlers.append(rate_limit_handler)

    logger.debug("Slack client configured with %d rate limit retries", max_retry_count)

    return client


# ---------------------------------------------------------------------------
# Helper: should_use_v2_api (ported from superset.utils.slack, no Flask)
# ---------------------------------------------------------------------------
def _should_use_v2_api(config: dict[str, Any]) -> bool:
    """Check if Slack V2 API should be used (no Flask dependency)."""
    if not feature_flag_manager.is_feature_enabled("ALERT_REPORT_SLACK_V2"):
        return False
    try:
        client = _get_slack_client(config)
        client.conversations_list()
        logger.info("Slack API v2 is available")
        return True
    except SlackApiError:
        # use the v1 api but warn with a deprecation message
        logger.warning(
            "Your current Slack scopes are missing `channels:read`. Please add "
            "this to your Slack app in order to continue using the v1 API. Support "
            "for the old Slack API will be removed in Superset version 6.0.0."
        )
        return False


# ===========================================================================
# SlackNotification (v1) -- Deprecated: Remove in Superset 6.0.0
# ===========================================================================
class SlackNotification(SlackMixin, BaseNotification):
    """
    Sends a slack notification for a report recipient
    """

    type = ReportRecipientType.SLACK

    def __init__(
        self,
        recipient: Any,
        content: NotificationContent,
        *,
        config: dict[str, Any] | None = None,
        logs_context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(recipient, content)
        self._config: dict[str, Any] = config or {}
        self._logs_context: dict[str, Any] = logs_context or {}

    def _get_channel(self) -> str:
        """
        Get the recipient's channel(s).
        Note Slack SDK uses "channel" to refer to one or more
        channels. Multiple channels are demarcated by a comma.
        :returns: The comma separated list of channel(s)
        """
        recipient_str = json.loads(self._recipient.recipient_config_json)["target"]

        return ",".join(_recipients_string_to_list(recipient_str))

    def _get_inline_files(
        self,
    ) -> tuple[Union[str, None], Sequence[Union[str, IOBase, bytes]]]:
        if self._content.csv:
            return ("csv", [self._content.csv])
        if self._content.screenshots:
            return ("png", self._content.screenshots)
        if self._content.pdf:
            return ("pdf", [self._content.pdf])
        return (None, [])

    @backoff.on_exception(backoff.expo, SlackApiError, factor=10, base=2, max_tries=5)
    def send(self) -> None:
        file_type, files = self._get_inline_files()
        title = self._content.name
        body = self._get_body(content=self._content)

        # see if the v2 api will work
        if _should_use_v2_api(self._config):
            # if we can fetch channels, then raise an error and use the v2 api
            raise SlackV1NotificationError

        try:
            client = _get_slack_client(self._config)
            channel = self._get_channel()
            # files_upload returns SlackResponse as we run it in sync mode.
            if files:
                for file in files:
                    client.files_upload(
                        channels=channel,
                        file=file,
                        initial_comment=body,
                        title=title,
                        filetype=file_type,
                    )
            else:
                client.chat_postMessage(channel=channel, text=body)
            logger.info(
                "Report sent to slack",
                extra={
                    "execution_id": self._logs_context.get("execution_id"),
                },
            )
        except (
            BotUserAccessError,
            SlackRequestError,
            SlackClientConfigurationError,
        ) as ex:
            raise NotificationParamException(str(ex)) from ex
        except SlackObjectFormationError as ex:
            raise NotificationMalformedException(str(ex)) from ex
        except SlackTokenRotationError as ex:
            raise NotificationAuthorizationException(str(ex)) from ex
        except (SlackClientNotConnectedError, SlackApiError) as ex:
            raise NotificationUnprocessableException(str(ex)) from ex
        except SlackClientError as ex:
            # this is the base class for all slack client errors
            # keep it last so that it doesn't interfere with @backoff
            raise NotificationUnprocessableException(str(ex)) from ex


# ===========================================================================
# SlackV2Notification
# ===========================================================================
class SlackV2Notification(SlackMixin, BaseNotification):
    """
    Sends a slack notification for a report recipient with the slack upload v2 API
    """

    type = ReportRecipientType.SLACKV2

    def __init__(
        self,
        recipient: Any,
        content: NotificationContent,
        *,
        config: dict[str, Any] | None = None,
        logs_context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(recipient, content)
        self._config: dict[str, Any] = config or {}
        self._logs_context: dict[str, Any] = logs_context or {}

    def _get_channels(self) -> list[str]:
        """
        Get the recipient's channel(s).
        :returns: A list of channel ids: "EID676L"
        :raises NotificationParamException or SlackApiError: If the recipient
                is not found
        """
        recipient_str = json.loads(self._recipient.recipient_config_json)["target"]

        return _recipients_string_to_list(recipient_str)

    def _get_inline_files(
        self,
    ) -> tuple[Union[str, None], Sequence[Union[str, IOBase, bytes]]]:
        if self._content.csv:
            return ("csv", [self._content.csv])
        if self._content.screenshots:
            return ("png", self._content.screenshots)
        if self._content.pdf:
            return ("pdf", [self._content.pdf])
        return (None, [])

    @backoff.on_exception(backoff.expo, SlackApiError, factor=10, base=2, max_tries=5)
    def send(self) -> None:
        try:
            client = _get_slack_client(self._config)
            title = self._content.name
            body = self._get_body(content=self._content)

            channels = self._get_channels()

            if not channels:
                raise NotificationParamException("No recipients saved in the report")

            file_type, files = self._get_inline_files()
            file_name = f"{title}.{file_type}"

            # files_upload returns SlackResponse as we run it in sync mode.
            for channel in channels:
                if len(files) > 0:
                    for file in files:
                        client.files_upload_v2(
                            channel=channel,
                            file=file,
                            initial_comment=body,
                            title=title,
                            filename=file_name,
                        )
                else:
                    client.chat_postMessage(channel=channel, text=body)

            logger.info(
                "Report sent to slack",
                extra={
                    "execution_id": self._logs_context.get("execution_id"),
                },
            )
        except (
            BotUserAccessError,
            SlackRequestError,
            SlackClientConfigurationError,
        ) as ex:
            raise NotificationParamException(str(ex)) from ex
        except SlackObjectFormationError as ex:
            raise NotificationMalformedException(str(ex)) from ex
        except SlackTokenRotationError as ex:
            raise NotificationAuthorizationException(str(ex)) from ex
        except (SlackClientNotConnectedError, SlackApiError) as ex:
            raise NotificationUnprocessableException(str(ex)) from ex
        except SlackClientError as ex:
            # this is the base class for all slack client errors
            # keep it last so that it doesn't interfere with @backoff
            raise NotificationUnprocessableException(str(ex)) from ex
