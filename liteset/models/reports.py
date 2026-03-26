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

Pure SQLAlchemy -- no Flask dependencies.
"""
from __future__ import annotations

import enum
import uuid as uuid_mod
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
from sqlalchemy.orm import relationship
from sqlalchemy_utils import UUIDType

from liteset.models.helpers import (
    AuditMixinNullable,
    Base,
    ExtraJSONMixin,
    MediumText,
    metadata,
)


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
    ),
    Column(
        "report_schedule_id",
        Integer,
        ForeignKey("report_schedule.id", ondelete="CASCADE"),
    ),
)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ReportSchedule(AuditMixinNullable, ExtraJSONMixin, Base):
    """A scheduled report or alert."""

    __tablename__ = "report_schedule"
    __table_args__ = (UniqueConstraint("name", "type"),)

    id = Column(Integer, primary_key=True)
    type = Column(String(50))
    name = Column(String(150), nullable=False)
    description = Column(Text)
    context_markdown = Column(Text)
    active = Column(Boolean, default=True, index=True)
    crontab = Column(String(1000))
    creation_method = Column(
        String(255),
        server_default=ReportCreationMethod.ALERTS_REPORTS.value,
    )
    timezone = Column(String(100), default="UTC")
    report_format = Column(
        String(50), default=ReportDataFormat.VISUALIZATION.value
    )
    sql = Column(Text)
    chart_id = Column(
        Integer, ForeignKey("slices.id"), nullable=True
    )
    dashboard_id = Column(
        Integer, ForeignKey("dashboards.id"), nullable=True
    )
    database_id = Column(
        Integer, ForeignKey("dbs.id"), nullable=True
    )
    last_eval_dttm = Column(DateTime)
    last_state = Column(String(50))
    last_value = Column(Float)
    last_value_row_json = Column(Text)
    validator_type = Column(String(100))
    validator_config_json = Column(Text)
    log_retention = Column(Integer, default=90)
    grace_period = Column(Integer, default=0)
    working_timeout = Column(Integer, default=3600)
    force_screenshot = Column(Boolean, default=False)
    custom_width = Column(Integer, nullable=True)
    custom_height = Column(Integer, nullable=True)
    email_subject = Column(String(255), nullable=True)

    # -- relationships --------------------------------------------------------

    chart = relationship(
        "Slice",
        foreign_keys=[chart_id],
    )
    dashboard = relationship(
        "Dashboard",
        foreign_keys=[dashboard_id],
    )
    database = relationship(
        "Database",
        foreign_keys=[database_id],
    )
    owners = relationship(
        "User",
        secondary=report_schedule_user,
        passive_deletes=True,
    )
    recipients = relationship(
        "ReportRecipients",
        backref="report_schedule",
        cascade="all, delete-orphan",
    )
    logs = relationship(
        "ReportExecutionLog",
        backref="report_schedule",
        cascade="all, delete-orphan",
    )


class ReportRecipients(Base, AuditMixinNullable):
    """A recipient of a report schedule."""

    __tablename__ = "report_recipient"

    id = Column(Integer, primary_key=True)
    type = Column(String(50))
    recipient_config_json = Column(Text)
    report_schedule_id = Column(
        Integer,
        ForeignKey("report_schedule.id"),
        nullable=False,
    )


class ReportExecutionLog(Base):
    """Execution log entry for a report schedule."""

    __tablename__ = "report_execution_log"

    id = Column(Integer, primary_key=True)
    uuid = Column(
        UUIDType(binary=True), default=uuid_mod.uuid4
    )
    scheduled_dttm = Column(DateTime)
    start_dttm = Column(DateTime)
    end_dttm = Column(DateTime)
    value = Column(Float)
    value_row_json = Column(Text)
    state = Column(String(50))
    error_message = Column(Text)
    report_schedule_id = Column(
        Integer,
        ForeignKey("report_schedule.id"),
        nullable=False,
    )
