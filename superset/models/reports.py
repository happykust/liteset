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
"""Report / alert schedule models.

Pure SQLAlchemy -- no legacy WSGI dependencies.
"""

from __future__ import annotations

import enum
import uuid
import uuid as uuid_mod
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from superset.models.helpers import (
    AuditMixinNullable,
    Base,
    BinaryUUID,
    ExtraJSONMixin,
    MediumText,
    metadata,
)

if TYPE_CHECKING:
    from superset.models.core import Database
    from superset.models.dashboard import Dashboard
    from superset.models.security import User
    from superset.models.slice import Slice

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ReportScheduleType(str, enum.Enum):
    """Type of report schedule."""

    ALERT = "Alert"
    REPORT = "Report"


class ReportScheduleValidatorType(str, enum.Enum):
    """Validator used for alert evaluation."""

    NOT_NULL = "not null"
    OPERATOR = "operator"


class ReportRecipientType(str, enum.Enum):
    """Channel used to deliver reports."""

    EMAIL = "Email"
    SLACK = "Slack"
    SLACKV2 = "SlackV2"


class ReportState(str, enum.Enum):
    """Current state of a report execution."""

    SUCCESS = "Success"
    WORKING = "Working"
    ERROR = "Error"
    NOOP = "Not triggered"
    GRACE = "On Grace"


class ReportDataFormat(str, enum.Enum):
    """Format of the data attached to a report."""

    PDF = "PDF"
    VISUALIZATION = "PNG"
    DATA = "CSV"
    TEXT = "TEXT"


class ReportCreationMethod(str, enum.Enum):
    """How the report schedule was created."""

    CHARTS = "charts"
    DASHBOARDS = "dashboards"
    ALERTS_REPORTS = "alerts_reports"


class ReportSourceFormat(str, enum.Enum):
    """Source format of the report content."""

    CHART = "chart"
    DASHBOARD = "dashboard"


# ---------------------------------------------------------------------------
# Association tables
# ---------------------------------------------------------------------------

report_schedule_user = Table(
    "report_schedule_user",
    metadata,
    Column("id", Integer, primary_key=True),
    Column(
        "user_id",
        Integer,
        ForeignKey("ab_user.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "report_schedule_id",
        Integer,
        ForeignKey("report_schedule.id", ondelete="CASCADE"),
        nullable=False,
    ),
    UniqueConstraint("user_id", "report_schedule_id"),
)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ReportSchedule(AuditMixinNullable, ExtraJSONMixin, Base):
    """A scheduled report or alert."""

    __tablename__ = "report_schedule"
    __table_args__ = (UniqueConstraint("name", "type"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    type: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    context_markdown: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool | None] = mapped_column(Boolean, default=True, index=True)
    crontab: Mapped[str] = mapped_column(String(1000), nullable=False)
    creation_method: Mapped[str | None] = mapped_column(
        String(255),
        server_default=ReportCreationMethod.ALERTS_REPORTS.value,
    )
    timezone: Mapped[str] = mapped_column(String(100), default="UTC", nullable=False)
    report_format: Mapped[str | None] = mapped_column(
        String(50), default=ReportDataFormat.VISUALIZATION.value
    )
    sql: Mapped[str | None] = mapped_column(MediumText())
    chart_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("slices.id"), nullable=True
    )
    dashboard_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("dashboards.id"), nullable=True
    )
    database_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("dbs.id"), nullable=True
    )
    last_eval_dttm: Mapped[datetime | None] = mapped_column(DateTime)
    last_state: Mapped[str | None] = mapped_column(String(50), default=ReportState.NOOP)
    last_value: Mapped[float | None] = mapped_column(Float)
    last_value_row_json: Mapped[str | None] = mapped_column(MediumText())
    validator_type: Mapped[str | None] = mapped_column(String(100))
    validator_config_json: Mapped[str | None] = mapped_column(
        MediumText(), default="{}"
    )
    log_retention: Mapped[int | None] = mapped_column(Integer, default=90)
    grace_period: Mapped[int | None] = mapped_column(Integer, default=60 * 60 * 4)
    working_timeout: Mapped[int | None] = mapped_column(Integer, default=3600)
    force_screenshot: Mapped[bool | None] = mapped_column(Boolean, default=False)
    custom_width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    custom_height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    email_subject: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # -- relationships --------------------------------------------------------

    chart: Mapped[Slice | None] = relationship(
        "Slice",
        foreign_keys=[chart_id],
    )
    dashboard: Mapped[Dashboard | None] = relationship(
        "Dashboard",
        foreign_keys=[dashboard_id],
    )
    database: Mapped[Database | None] = relationship(
        "Database",
        foreign_keys=[database_id],
    )
    owners: Mapped[list[User]] = relationship(
        "User",
        secondary=report_schedule_user,
        passive_deletes=True,
    )
    recipients: Mapped[list[ReportRecipients]] = relationship(
        "ReportRecipients",
        backref="report_schedule",
        cascade="all, delete-orphan",
    )
    logs: Mapped[list[ReportExecutionLog]] = relationship(
        "ReportExecutionLog",
        backref="report_schedule",
        cascade="all, delete-orphan",
    )

    @property
    def crontab_humanized(self) -> str:
        try:
            from cron_descriptor import get_description

            return get_description(self.crontab) if self.crontab else ""
        except Exception:
            return str(self.crontab) if self.crontab else ""


class ReportRecipients(Base, AuditMixinNullable):
    """A recipient of a report schedule."""

    __tablename__ = "report_recipient"
    __table_args__ = (
        Index("ix_report_recipient_report_schedule_id", "report_schedule_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    type: Mapped[str] = mapped_column(String(50), nullable=False)
    recipient_config_json: Mapped[str | None] = mapped_column(
        MediumText(), default="{}"
    )
    report_schedule_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("report_schedule.id"),
        nullable=False,
    )


class ReportExecutionLog(Base):
    """Execution log entry for a report schedule."""

    __tablename__ = "report_execution_log"
    __table_args__ = (
        Index("ix_report_execution_log_report_schedule_id", "report_schedule_id"),
        Index("ix_report_execution_log_start_dttm", "start_dttm"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    uuid: Mapped[uuid.UUID | None] = mapped_column(BinaryUUID(), default=uuid_mod.uuid4)
    scheduled_dttm: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    start_dttm: Mapped[datetime | None] = mapped_column(DateTime)
    end_dttm: Mapped[datetime | None] = mapped_column(DateTime)
    value: Mapped[float | None] = mapped_column(Float)
    value_row_json: Mapped[str | None] = mapped_column(MediumText())
    state: Mapped[str] = mapped_column(String(50), nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)
    report_schedule_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("report_schedule.id"),
        nullable=False,
    )
