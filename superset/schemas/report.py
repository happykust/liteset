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
"""msgspec Structs for the Report Schedule API."""

from __future__ import annotations

from typing import Annotated, Any

import msgspec
from msgspec import Meta

from superset.schemas.base import ModelStruct, UserRef

# Valid report format values — mirrors ReportDataFormat enum (superset/models/reports.py)
_REPORT_DATA_FORMATS = ("PDF", "PNG", "CSV", "TEXT")
# Valid recipient type values — mirrors ReportRecipientType enum
_RECIPIENT_TYPES = ("Email", "Slack", "SlackV2")
# Valid validator type values — mirrors ReportScheduleValidatorType enum
_VALIDATOR_TYPES = ("not null", "operator")
# Valid schedule type values — mirrors ReportScheduleType enum
_SCHEDULE_TYPES = ("Alert", "Report")


class ReportRecipientConfigJSON(msgspec.Struct):
    """Nested config for a report recipient.

    Matches ReportRecipientConfigJSONSchema.
    """

    target: str | None = None
    ccTarget: str | None = None  # noqa: N815
    bccTarget: str | None = None  # noqa: N815


class ReportRecipientSchema(msgspec.Struct):
    # OneOf(ReportRecipientType) — matches original ReportRecipientSchema field
    # validator ``validate.OneOf(choices=tuple(key.value for key in ReportRecipientType))``.
    type: Annotated[str, Meta(pattern=r"^(Email|Slack|SlackV2)$")]
    recipient_config_json: ReportRecipientConfigJSON


class ReportSchedulePostSchema(msgspec.Struct):
    # ``name`` Length(1, 150) — matches original ``validate=[Length(1, 150)]``.
    name: Annotated[str, Meta(min_length=1, max_length=150)]
    type: str  # "Report" | "Alert"
    description: str = ""
    # ``crontab`` Length(1, 1000) — matches original ``validate=[validate_crontab,
    # Length(1, 1000)]``.  Cron *format* is validated in
    # :meth:`CreateReportScheduleCommand.validate`.
    crontab: Annotated[str, Meta(min_length=1, max_length=1000)] = "0 * * * *"
    timezone: str = "UTC"
    sql: str = ""
    chart: int | None = None
    dashboard: int | None = None
    database: int | None = None
    owners: list[int] = []
    recipients: list[ReportRecipientSchema] = []
    validator_type: str | None = None
    validator_config_json: dict[str, Any] | None = None
    # ``log_retention`` Range(min=1) — matches original
    # ``validate=[Range(min=1, error="Value must be greater than 0")]``.
    log_retention: Annotated[int, Meta(ge=1)] = 90
    # ``grace_period`` Range(min=1) — matches original.
    grace_period: Annotated[int, Meta(ge=1)] = 14400
    email_subject: str | None = None
    context_markdown: str | None = None
    creation_method: str | None = None
    # ``working_timeout`` Range(min=1) — matches original.
    working_timeout: Annotated[int, Meta(ge=1)] = 3600
    selected_tabs: list[int] | None = None
    # ``report_format`` dump_default=ReportDataFormat.PNG — matches original
    # ``dump_default=ReportDataFormat.PNG``.  Sending the default ensures the
    # column default is not accidentally defeated by a None write.
    report_format: str = "PNG"
    active: bool = True
    force_screenshot: bool = False
    custom_width: int | None = None
    custom_height: int | None = None
    extra: dict[str, Any] = {}


class ReportSchedulePutSchema(msgspec.Struct):
    # ``name`` Length(1, 150) — matches original ``validate=[Length(1, 150)]``.
    name: Annotated[str, Meta(min_length=1, max_length=150)] | None | msgspec.UnsetType = msgspec.UNSET
    type: str | None | msgspec.UnsetType = msgspec.UNSET
    description: str | None | msgspec.UnsetType = msgspec.UNSET
    # ``crontab`` Length(1, 1000) — matches original.
    crontab: Annotated[str, Meta(min_length=1, max_length=1000)] | None | msgspec.UnsetType = msgspec.UNSET
    timezone: str | None | msgspec.UnsetType = msgspec.UNSET
    sql: str | None | msgspec.UnsetType = msgspec.UNSET
    chart: int | None | msgspec.UnsetType = msgspec.UNSET
    dashboard: int | None | msgspec.UnsetType = msgspec.UNSET
    database: int | None | msgspec.UnsetType = msgspec.UNSET
    owners: list[int] | None | msgspec.UnsetType = msgspec.UNSET
    recipients: list[ReportRecipientSchema] | None | msgspec.UnsetType = msgspec.UNSET
    validator_type: str | None | msgspec.UnsetType = msgspec.UNSET
    validator_config_json: dict[str, Any] | None | msgspec.UnsetType = msgspec.UNSET
    # ``log_retention`` Range(min=0) for PUT — matches original PUT schema
    # ``validate=[Range(min=0, error="Value must be 0 or greater")]``.
    log_retention: Annotated[int, Meta(ge=0)] | None | msgspec.UnsetType = msgspec.UNSET
    # ``grace_period`` Range(min=1) for PUT — matches original.
    grace_period: Annotated[int, Meta(ge=1)] | None | msgspec.UnsetType = msgspec.UNSET
    email_subject: str | None | msgspec.UnsetType = msgspec.UNSET
    context_markdown: str | None | msgspec.UnsetType = msgspec.UNSET
    creation_method: str | None | msgspec.UnsetType = msgspec.UNSET
    # ``working_timeout`` Range(min=1) for PUT — matches original.
    working_timeout: Annotated[int, Meta(ge=1)] | None | msgspec.UnsetType = msgspec.UNSET
    selected_tabs: list[int] | None | msgspec.UnsetType = msgspec.UNSET
    report_format: str | None | msgspec.UnsetType = msgspec.UNSET
    active: bool | None | msgspec.UnsetType = msgspec.UNSET
    force_screenshot: bool | None | msgspec.UnsetType = msgspec.UNSET
    custom_width: int | None | msgspec.UnsetType = msgspec.UNSET
    custom_height: int | None | msgspec.UnsetType = msgspec.UNSET
    extra: dict[str, Any] | None | msgspec.UnsetType = msgspec.UNSET


# ---------------------------------------------------------------------------
# Detail result Structs for GET /{pk}
# ---------------------------------------------------------------------------


class ChartRef(ModelStruct):
    """Chart reference embedded in report detail response.

    ``datasource_id`` / ``datasource_type`` mirror the original
    ``show_select_columns`` (superset_old/reports/api.py:128-131) so the GET
    detail response exposes the chart's datasource reference.
    """

    id: int
    slice_name: str | None = None
    viz_type: str | None = None
    datasource_id: int | None = None
    datasource_type: str | None = None


class ReportDashboardRef(ModelStruct):
    """Dashboard reference embedded in report detail response."""

    id: int
    dashboard_title: str | None = None


class ReportDatabaseRef(ModelStruct):
    """Database reference embedded in report detail response."""

    id: int
    database_name: str | None = None


class RecipientRef(ModelStruct):
    """Recipient reference in report detail response."""

    id: int
    type: str | None = None
    recipient_config_json: str | None = None


class ReportDetailResult(ModelStruct):
    """Full report schedule detail returned by GET /api/v1/report/{pk}."""

    # ``id`` is in upstream's ``show_columns`` so it appears inside ``result``
    # (not only the FAB envelope) — the Alerts/Reports edit modal reads it.
    id: int | None = None
    name: str = ""
    type: str = ""
    description: str | None = None
    crontab: str | None = None
    timezone: str = "UTC"
    active: bool = True
    chart_id: int | None = None
    dashboard_id: int | None = None
    database_id: int | None = None
    sql: str | None = None
    validator_type: str | None = None
    validator_config_json: str | None = None
    log_retention: int | None = None
    grace_period: int | None = None
    force_screenshot: bool = False
    custom_width: int | None = None
    custom_height: int | None = None
    last_eval_dttm: str | None = None
    last_state: str | None = None
    context_markdown: str | None = None
    creation_method: str | None = None
    extra: dict[str, Any] | None = None
    last_value: float | None = None
    last_value_row_json: str | None = None
    report_format: str | None = None
    working_timeout: int | None = None
    email_subject: str | None = None
    chart: ChartRef | None = None
    dashboard: ReportDashboardRef | None = None
    database: ReportDatabaseRef | None = None
    owners: list[UserRef] = []
    recipients: list[RecipientRef] = []
