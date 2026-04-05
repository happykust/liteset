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


class ReportRecipientConfigJSON(msgspec.Struct):
    """Nested config for a report recipient.

    Matches ReportRecipientConfigJSONSchema.
    """

    target: str | None = None
    ccTarget: str | None = None  # noqa: N815
    bccTarget: str | None = None  # noqa: N815


class ReportRecipientSchema(msgspec.Struct):
    type: str  # "Email" | "Slack"
    recipient_config_json: ReportRecipientConfigJSON


class ReportSchedulePostSchema(msgspec.Struct):
    name: Annotated[str, Meta(min_length=1)]
    type: str  # "Report" | "Alert"
    description: str = ""
    crontab: str = "0 * * * *"
    timezone: str = "UTC"
    sql: str = ""
    chart: int | None = None
    dashboard: int | None = None
    database: int | None = None
    owners: list[int] = []
    recipients: list[ReportRecipientSchema] = []
    validator_type: str | None = None
    validator_config_json: dict[str, Any] | None = None
    log_retention: int = 90
    grace_period: int = 14400
    email_subject: str | None = None
    context_markdown: str | None = None
    creation_method: str | None = None
    working_timeout: int = 3600
    selected_tabs: list[int] | None = None
    report_format: str | None = None
    active: bool = True
    force_screenshot: bool = False
    custom_width: int | None = None
    custom_height: int | None = None
    extra: dict[str, Any] = {}


class ReportSchedulePutSchema(msgspec.Struct):
    name: str | None | msgspec.UnsetType = msgspec.UNSET
    type: str | None | msgspec.UnsetType = msgspec.UNSET
    description: str | None | msgspec.UnsetType = msgspec.UNSET
    crontab: str | None | msgspec.UnsetType = msgspec.UNSET
    timezone: str | None | msgspec.UnsetType = msgspec.UNSET
    sql: str | None | msgspec.UnsetType = msgspec.UNSET
    chart: int | None | msgspec.UnsetType = msgspec.UNSET
    dashboard: int | None | msgspec.UnsetType = msgspec.UNSET
    database: int | None | msgspec.UnsetType = msgspec.UNSET
    owners: list[int] | None | msgspec.UnsetType = msgspec.UNSET
    recipients: list[ReportRecipientSchema] | None | msgspec.UnsetType = msgspec.UNSET
    validator_type: str | None | msgspec.UnsetType = msgspec.UNSET
    validator_config_json: dict[str, Any] | None | msgspec.UnsetType = msgspec.UNSET
    log_retention: int | None | msgspec.UnsetType = msgspec.UNSET
    grace_period: int | None | msgspec.UnsetType = msgspec.UNSET
    email_subject: str | None | msgspec.UnsetType = msgspec.UNSET
    context_markdown: str | None | msgspec.UnsetType = msgspec.UNSET
    creation_method: str | None | msgspec.UnsetType = msgspec.UNSET
    working_timeout: int | None | msgspec.UnsetType = msgspec.UNSET
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
    """Chart reference embedded in report detail response."""

    id: int
    slice_name: str | None = None
    viz_type: str | None = None


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
