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
import inspect
from typing import Any

from superset.models.reports import ReportRecipients
from superset.reports.notifications.base import BaseNotification, NotificationContent
from superset.reports.notifications.email import EmailNotification  # noqa: F401
from superset.reports.notifications.slack import (  # noqa: F401
    SlackNotification,
    SlackV2Notification,
)


def _build_notification_config() -> dict[str, Any]:
    """Build the ``current_app.config`` equivalent consumed by the notification
    plugins (SMTP/email/Slack/feature-flags) from ``SupersetSettings``.

    Upstream Superset reads these values from the global ``current_app.config``
    inside each notification class.  In Liteset the notification classes read
    them from ``self._config``, so the factory must materialise the dict from
    :class:`~superset.config.SupersetSettings` and inject it.
    """
    from superset.config import SupersetSettings

    settings = SupersetSettings()  # type: ignore[call-arg]
    return {
        # SMTP / email
        "SMTP_HOST": settings.smtp_host,
        "SMTP_PORT": settings.smtp_port,
        "SMTP_USER": settings.smtp_user,
        "SMTP_PASSWORD": settings.smtp_password,
        "SMTP_MAIL_FROM": settings.smtp_mail_from,
        "SMTP_STARTTLS": settings.smtp_starttls,
        "SMTP_SSL": settings.smtp_ssl,
        "SMTP_SSL_SERVER_AUTH": settings.smtp_ssl_server_auth,
        "EMAIL_REPORTS_SUBJECT_PREFIX": settings.email_reports_subject_prefix,
        "EMAIL_REPORTS_CTA": settings.email_reports_cta,
        "EMAIL_HEADER_MUTATOR": settings.email_header_mutator,
        # Slack
        "SLACK_API_TOKEN": settings.slack_api_token,
        "SLACK_PROXY": settings.slack_proxy,
        "SLACK_API_RATE_LIMIT_RETRY_COUNT": settings.slack_api_rate_limit_retry_count,
        # Feature flags
        "FEATURE_FLAGS": settings.feature_flags,
    }


def create_notification(
    recipient: ReportRecipients, notification_content: NotificationContent
) -> BaseNotification:
    """
    Notification polymorphic factory
    Returns the Notification class for the recipient type
    """
    for plugin in BaseNotification.plugins:
        if plugin.type == recipient.type:
            kwargs: dict[str, Any] = {}
            # Inject the config/logs_context only for plugins whose __init__
            # accepts them (EmailNotification, SlackNotification,
            # SlackV2Notification). Upstream relies on the global
            # ``current_app.config``/``g.logs_context``; Liteset passes them in.
            params = inspect.signature(plugin.__init__).parameters
            if "config" in params:
                kwargs["config"] = _build_notification_config()
            if "logs_context" in params:
                header_data = notification_content.header_data or {}
                kwargs["logs_context"] = {
                    "execution_id": header_data.get("execution_id"),
                }
            return plugin(recipient, notification_content, **kwargs)
    raise Exception(  # noqa: TRY002
        "Recipient type not supported"
    )
