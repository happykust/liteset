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

from datetime import datetime

import pandas as pd
import pytest
from pytz import timezone

from superset.models.reports import ReportRecipients, ReportRecipientType
from superset.reports.notifications.base import NotificationContent
from superset.reports.notifications.email import EmailNotification
from superset.utils.feature_flags import feature_flag_manager


def test_render_description_with_html() -> None:
    content = NotificationContent(
        name="test alert",
        embedded_data=pd.DataFrame(
            {
                "A": [1, 2, 3],
                "B": [4, 5, 6],
                "C": ["111", "222", '<a href="http://www.example.com">333</a>'],
            }
        ),
        description='<p>This is <a href="#">a test</a> alert</p><br />',
        header_data={
            "notification_format": "PNG",
            "notification_type": "Alert",
            "owners": [1],
            "notification_source": None,
            "chart_id": None,
            "dashboard_id": None,
            "slack_channels": None,
            "execution_id": "test-execution-id",
        },
    )
    email_body = (
        EmailNotification(
            recipient=ReportRecipients(type=ReportRecipientType.EMAIL),
            content=content,
            config={"SMTP_MAIL_FROM": "superset@example.com"},
        )
        ._get_content()
        .body
    )
    assert (
        '<p>This is <a href="#" rel="noopener noreferrer">a test</a> alert</p><br>'
        in email_body
    )
    assert '<td>&lt;a href="http://www.example.com"&gt;333&lt;/a&gt;</td>' in email_body


def test_email_subject_with_datetime(monkeypatch: pytest.MonkeyPatch) -> None:
    def _is_enabled(feature: str, default: bool = False) -> bool:
        return feature == "DATE_FORMAT_IN_EMAIL_SUBJECT"

    monkeypatch.setattr(feature_flag_manager, "is_feature_enabled", _is_enabled)

    now = datetime.now(timezone("UTC"))

    datetime_pattern = "%Y-%m-%d"

    content = NotificationContent(
        name=f"test alert {datetime_pattern}",
        embedded_data=pd.DataFrame(
            {
                "A": [1, 2, 3],
            }
        ),
        description='<p>This is <a href="#">a test</a> alert</p><br />',
        header_data={
            "notification_format": "PNG",
            "notification_type": "Alert",
            "owners": [1],
            "notification_source": None,
            "chart_id": None,
            "dashboard_id": None,
            "slack_channels": None,
            "execution_id": "test-execution-id",
        },
    )
    subject = EmailNotification(
        recipient=ReportRecipients(type=ReportRecipientType.EMAIL), content=content
    )._get_subject()
    assert datetime_pattern not in subject
    assert now.strftime(datetime_pattern) in subject
