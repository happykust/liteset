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
from __future__ import annotations

import logging
import os
import re
import smtplib
import ssl
import textwrap
from dataclasses import dataclass
from datetime import datetime
from email.mime.application import MIMEApplication
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid, parseaddr
from typing import Any, Callable, Optional

import nh3
from pytz import timezone

from superset.exceptions import SupersetErrorsException
from superset.i18n import gettext as __
from superset.models.reports import ReportRecipientType
from superset.reports.notifications.base import (
    BaseNotification,
    HeaderDataType,
    NotificationContent,
)
from superset.reports.notifications.exceptions import NotificationError
from superset.utils import json
from superset.utils.decorators import statsd_gauge
from superset.utils.feature_flags import feature_flag_manager

logger = logging.getLogger(__name__)

TABLE_TAGS = {"table", "th", "tr", "td", "thead", "tbody", "tfoot"}
TABLE_ATTRIBUTES = {"colspan", "rowspan", "halign", "border", "class"}

ALLOWED_TAGS = {
    "a",
    "abbr",
    "acronym",
    "b",
    "blockquote",
    "br",
    "code",
    "div",
    "em",
    "i",
    "li",
    "ol",
    "p",
    "strong",
    "ul",
}.union(TABLE_TAGS)

ALLOWED_TABLE_ATTRIBUTES = dict.fromkeys(TABLE_TAGS, TABLE_ATTRIBUTES)
ALLOWED_ATTRIBUTES = {
    "a": {"href", "title"},
    "abbr": {"title"},
    "acronym": {"title"},
    **ALLOWED_TABLE_ATTRIBUTES,
}


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
# Helper: send_mime_email  (ported 1:1 from superset.utils.core)
# ---------------------------------------------------------------------------
def _send_mime_email(
    e_from: str,
    e_to: list[str],
    mime_msg: MIMEMultipart,
    config: dict[str, Any],
    dryrun: bool = False,
) -> None:
    smtp_host = config["SMTP_HOST"]
    smtp_port = config["SMTP_PORT"]
    smtp_user = config["SMTP_USER"]
    smtp_password = config["SMTP_PASSWORD"]
    smtp_starttls = config["SMTP_STARTTLS"]
    smtp_ssl = config["SMTP_SSL"]
    smtp_ssl_server_auth = config["SMTP_SSL_SERVER_AUTH"]

    if dryrun:
        logger.info("Dryrun enabled, email notification content is below:")
        logger.info(mime_msg.as_string())
        return

    # Default ssl context is SERVER_AUTH using the default system
    # root CA certificates
    ssl_context = ssl.create_default_context() if smtp_ssl_server_auth else None
    smtp = (
        smtplib.SMTP_SSL(smtp_host, smtp_port, context=ssl_context)
        if smtp_ssl
        else smtplib.SMTP(smtp_host, smtp_port)
    )
    if smtp_starttls:
        smtp.starttls(context=ssl_context)
    if smtp_user and smtp_password:
        smtp.login(smtp_user, smtp_password)
    logger.debug("Sent an email to %s", str(e_to))
    smtp.sendmail(e_from, e_to, mime_msg.as_string())
    smtp.quit()


# ---------------------------------------------------------------------------
# Helper: send_email_smtp  (ported 1:1 from superset.utils.core)
# ---------------------------------------------------------------------------
def _send_email_smtp(
    to: str,
    subject: str,
    html_content: str,
    config: dict[str, Any],
    files: list[str] | None = None,
    data: dict[str, str] | None = None,
    pdf: dict[str, bytes] | None = None,
    images: dict[str, bytes] | None = None,
    dryrun: bool = False,
    cc: str | None = None,
    bcc: str | None = None,
    mime_subtype: str = "mixed",
    header_data: HeaderDataType | None = None,
) -> None:
    """
    Send an email with html content, eg:
    send_email_smtp(
        'test@example.com', 'foo', '<b>Foo</b> bar',['/dev/null'], dryrun=True)
    """
    smtp_mail_from = config["SMTP_MAIL_FROM"]
    smtp_mail_to = _recipients_string_to_list(to)

    msg = MIMEMultipart(mime_subtype)
    msg["Subject"] = subject
    msg["From"] = smtp_mail_from
    msg["To"] = ", ".join(smtp_mail_to)

    msg.preamble = "This is a multi-part message in MIME format."

    recipients = smtp_mail_to
    if cc:
        smtp_mail_cc = _recipients_string_to_list(cc)
        msg["Cc"] = ", ".join(smtp_mail_cc)
        recipients = recipients + smtp_mail_cc

    smtp_mail_bcc: list[str] = []
    if bcc:
        # don't add bcc in header
        smtp_mail_bcc = _recipients_string_to_list(bcc)
        recipients = recipients + smtp_mail_bcc

    msg["Date"] = formatdate(localtime=True)
    mime_text = MIMEText(html_content, "html")
    msg.attach(mime_text)

    # Attach files by reading them from disk
    for fname in files or []:
        basename = os.path.basename(fname)
        with open(fname, "rb") as f:
            msg.attach(
                MIMEApplication(
                    f.read(),
                    Content_Disposition=f"attachment; filename='{basename}'",
                    Name=basename,
                )
            )

    # Attach any files passed directly
    for name, body in (data or {}).items():
        msg.attach(
            MIMEApplication(
                body, Content_Disposition=f"attachment; filename='{name}'", Name=name
            )
        )

    for name, body_pdf in (pdf or {}).items():
        msg.attach(
            MIMEApplication(
                body_pdf,
                Content_Disposition=f"attachment; filename='{name}'",
                Name=name,
            )
        )

    # Attach any inline images, which may be required for display in
    # HTML content (inline)
    for msgid, imgdata in (images or {}).items():
        formatted_time = formatdate(localtime=True)
        file_name = f"{subject} {formatted_time}"
        image = MIMEImage(imgdata, name=file_name)
        image.add_header("Content-ID", f"<{msgid}>")
        image.add_header("Content-Disposition", "inline")
        msg.attach(image)

    msg_mutator: Callable[..., MIMEMultipart] | None = config.get(
        "EMAIL_HEADER_MUTATOR"
    )
    if msg_mutator is not None:
        # the base notification returns the message without any editing.
        new_msg = msg_mutator(msg, **(header_data or {}))
    else:
        new_msg = msg
    new_to = new_msg["To"].split(", ") if "To" in new_msg else []
    new_cc = new_msg["Cc"].split(", ") if "Cc" in new_msg else []
    new_recipients = new_to + new_cc + smtp_mail_bcc
    if set(new_recipients) != set(recipients):
        recipients = new_recipients
    _send_mime_email(smtp_mail_from, recipients, new_msg, config, dryrun=dryrun)


@dataclass
class EmailContent:
    body: str
    header_data: Optional[HeaderDataType] = None
    data: Optional[dict[str, Any]] = None
    pdf: Optional[dict[str, bytes]] = None
    images: Optional[dict[str, bytes]] = None


class EmailNotification(BaseNotification):
    """
    Sends an email notification for a report recipient
    """

    type = ReportRecipientType.EMAIL
    now = datetime.now(timezone("UTC"))

    def __init__(
        self,
        recipient: Any,
        content: NotificationContent,
        *,
        config: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(recipient, content)
        self._config: dict[str, Any] = config or {}

    @property
    def _name(self) -> str:
        """Include date format in the name if feature flag is enabled"""
        return (
            self._parse_name(self._content.name)
            if feature_flag_manager.is_feature_enabled("DATE_FORMAT_IN_EMAIL_SUBJECT")
            else self._content.name
        )

    def _get_smtp_domain(self) -> str:
        return parseaddr(self._config["SMTP_MAIL_FROM"])[1].split("@")[1]

    def _error_template(self, text: str) -> str:
        call_to_action = self._get_call_to_action()
        return __(
            """
            <p>Your report/alert was unable to be generated because of the following error: %(text)s</p>
            <p>Please check your dashboard/chart for errors.</p>
            <p><b><a href="%(url)s">%(call_to_action)s</a></b></p>
            """,  # noqa: E501
            text=text,
            url=self._content.url,
            call_to_action=call_to_action,
        )

    def _get_content(self) -> EmailContent:
        if self._content.text:
            return EmailContent(body=self._error_template(self._content.text))
        # Get the domain from the 'From' address ..
        # and make a message id without the < > in the end

        domain = self._get_smtp_domain()
        images = {}

        if self._content.screenshots:
            images = {
                make_msgid(domain)[1:-1]: screenshot
                for screenshot in self._content.screenshots
            }

        # Strip any malicious HTML from the description
        description = nh3.clean(
            self._content.description or "",
            tags=ALLOWED_TAGS,
            attributes=ALLOWED_ATTRIBUTES,
        )

        # Strip malicious HTML from embedded data, allowing only table elements
        if self._content.embedded_data is not None:
            df = self._content.embedded_data
            html_table = nh3.clean(
                df.to_html(na_rep="", index=True, escape=True),
                # pandas will escape the HTML in cells already, so passing
                # more allowed tags here will not work
                tags=TABLE_TAGS,
                attributes=ALLOWED_TABLE_ATTRIBUTES,
            )
        else:
            html_table = ""

        img_tags = []
        for msgid in images.keys():
            img_tags.append(
                f"""<div class="image">
                    <img width="1000" src="cid:{msgid}">
                </div>
                """
            )
        img_tag = "".join(img_tags)
        call_to_action = self._get_call_to_action()
        body = textwrap.dedent(
            f"""
            <html>
              <head>
                <style type="text/css">
                  table, th, td {{
                    border-collapse: collapse;
                    border-color: rgb(200, 212, 227);
                    color: rgb(42, 63, 95);
                    padding: 4px 8px;
                  }}
                  .image{{
                      margin-bottom: 18px;
                      min-width: 1000px;
                  }}
                </style>
              </head>
              <body>
                <div>{description}</div>
                <br>
                <b><a href="{self._content.url}">{call_to_action}</a></b><p></p>
                {html_table}
                {img_tag}
              </body>
            </html>
            """
        )
        csv_data = None
        if self._content.csv:
            csv_data = {f"{self._name}.csv": self._content.csv}

        pdf_data = None
        if self._content.pdf:
            pdf_data = {f"{self._name}.pdf": self._content.pdf}

        return EmailContent(
            body=body,
            images=images,
            pdf=pdf_data,
            data=csv_data,
            header_data=self._content.header_data,
        )

    def _get_subject(self) -> str:
        return __(
            "%(prefix)s %(title)s",
            prefix=self._config.get("EMAIL_REPORTS_SUBJECT_PREFIX", "[Report] "),
            title=self._name,
        )

    def _parse_name(self, name: str) -> str:
        """If user add a date format to the subject, parse it to the real date
        This feature is hidden behind a feature flag `DATE_FORMAT_IN_EMAIL_SUBJECT`
        by default it is disabled
        """
        return self.now.strftime(name)

    def _get_call_to_action(self) -> str:
        return __(self._config.get("EMAIL_REPORTS_CTA", "Explore in Superset"))

    def _get_to(self) -> str:
        return json.loads(self._recipient.recipient_config_json)["target"]

    def _get_cc(self) -> str:
        # To accommodate backward compatibility
        return json.loads(self._recipient.recipient_config_json).get("ccTarget", "")

    def _get_bcc(self) -> str:
        # To accommodate backward compatibility
        return json.loads(self._recipient.recipient_config_json).get("bccTarget", "")

    @statsd_gauge("reports.email.send")
    def send(self) -> None:
        subject = self._get_subject()
        content = self._get_content()
        to = self._get_to()
        cc = self._get_cc()
        bcc = self._get_bcc()

        try:
            _send_email_smtp(
                to,
                subject,
                content.body,
                self._config,
                files=[],
                data=content.data,
                pdf=content.pdf,
                images=content.images,
                mime_subtype="related",
                dryrun=False,
                cc=cc,
                bcc=bcc,
                header_data=content.header_data,
            )
            logger.info(
                "Report sent to email, task_id: %s, notification content is %s",
                content.header_data.get("execution_id")
                if content.header_data
                else None,
                content.header_data,
            )
        except SupersetErrorsException as ex:
            raise NotificationError(
                ";".join([error.get("message", str(error)) for error in ex.errors])
            ) from ex
        except Exception as ex:
            raise NotificationError(str(ex)) from ex
