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
from pytz import all_timezones

from superset.schemas.base import ModelStruct, UserRef

# Valid report format values — mirrors ReportDataFormat enum
# (superset/models/reports.py)
_REPORT_DATA_FORMATS = ("PDF", "PNG", "CSV", "TEXT")
# Valid recipient type values — mirrors ReportRecipientType enum
_RECIPIENT_TYPES = ("Email", "Slack", "SlackV2")
# Valid validator type values — mirrors ReportScheduleValidatorType enum
_VALIDATOR_TYPES = ("not null", "operator")
# Valid schedule type values — mirrors ReportScheduleType enum
_SCHEDULE_TYPES = ("Alert", "Report")
# Valid creation method values — mirrors ReportCreationMethod enum
_CREATION_METHODS = ("charts", "dashboards", "alerts_reports")
# Valid timezone values — mirrors pytz.all_timezones.
_VALID_TIMEZONES = frozenset(all_timezones)


class ReportRecipientConfigJSON(msgspec.Struct, forbid_unknown_fields=True):
    """Nested config for a report recipient.

    Matches ReportRecipientConfigJSONSchema.
    """

    target: str | msgspec.UnsetType = msgspec.UNSET
    ccTarget: str | msgspec.UnsetType = msgspec.UNSET  # noqa: N815
    bccTarget: str | msgspec.UnsetType = msgspec.UNSET  # noqa: N815


class ReportRecipientSchema(msgspec.Struct, forbid_unknown_fields=True):
    # OneOf(ReportRecipientType)
    type: Annotated[str, Meta(pattern=r"^(Email|Slack|SlackV2)$")]
    # No ``required=True`` on the nested field — a recipient without config_json is
    # valid; the model column keeps its default ``'{}'``.
    recipient_config_json: ReportRecipientConfigJSON | msgspec.UnsetType = msgspec.UNSET


class ValidatorConfigJSON(msgspec.Struct, forbid_unknown_fields=True):
    """Nested config for an alert validator.

    Validates ``op`` against the six comparison operators and ``threshold`` as
    a float. Neither ``op`` nor ``threshold`` accept null — the original schema
    rejects null at input time (HTTP 422) instead of allowing null to propagate to
    execution-time failure.  The fields are optional (may be absent), but explicit
    ``null`` values are rejected by the ``msgspec.UnsetType`` guard (no ``| None`` leg).
    """

    op: Annotated[str, Meta(pattern=r"^(<|<=|>|>=|==|!=)$")] | msgspec.UnsetType = (
        msgspec.UNSET
    )
    threshold: float | msgspec.UnsetType = msgspec.UNSET


class ReportSchedulePostSchema(msgspec.Struct, forbid_unknown_fields=True):
    """POST body. ``forbid_unknown_fields`` mirrors Marshmallow 3.x's default
    ``Meta.unknown = RAISE`` — unknown keys (e.g. ``custom_height``) → 422."""

    # ``name`` Length(1, 150) — matches original ``validate=[Length(1, 150)]``.
    name: Annotated[str, Meta(min_length=1, max_length=150)]
    type: str  # "Report" | "Alert"
    # ``crontab`` Length(1, 1000). Cron *format* is validated in
    # :meth:`CreateReportScheduleCommand.validate`.
    crontab: Annotated[str, Meta(min_length=1, max_length=1000)]
    # ``description`` has ``allow_none=True`` — null is accepted and the nullable
    # ``Column(Text)`` stores NULL.  Clients that send ``{"description": null}``
    # must not receive HTTP 400.
    description: str | None = None
    timezone: str = "UTC"
    sql: str = ""
    chart: int | None = None
    dashboard: int | None = None
    # No allow_none, so explicit null is rejected at decode time; absent is
    # omitted from the loaded dict (UNSET → filter_unset). Key presence drives
    # the "Database reference is not allowed on a report" rule.
    database: int | msgspec.UnsetType = msgspec.UNSET
    owners: list[int] = []
    recipients: list[ReportRecipientSchema] = []
    # ``validator_type`` has no ``allow_none=True`` — Marshmallow rejects explicit
    # ``null`` with "Field may not be null." (HTTP 422).  Use UnsetType so that
    # absent → UNSET (filtered before the command) and explicit null → decode
    # error (422).  The PUT schema uses ``str | None | msgspec.UnsetType``
    # because the PUT schema DOES carry ``allow_none=True``.
    validator_type: str | msgspec.UnsetType = msgspec.UNSET
    # ``validator_config_json`` has no ``allow_none=True`` in the POST schema.
    # Absent → not in output dict (column default='{}' applies); explicit null → HTTP
    # 422.  Use UnsetType so absent → UNSET → filtered by filter_unset (DAO never
    # touches the column) and explicit null → 422.
    validator_config_json: ValidatorConfigJSON | msgspec.UnsetType = msgspec.UNSET
    # ``log_retention`` Range(min=1).
    log_retention: Annotated[int, Meta(ge=1)] = 90
    # ``grace_period`` Range(min=1).
    grace_period: Annotated[int, Meta(ge=1)] = 14400
    # ``ReportSchedule.email_subject`` is String(255); without the cap an
    # over-long value reaches the INSERT and the driver error (carrying the
    # statement and its parameters) surfaces in the 422 body.
    email_subject: Annotated[str, Meta(max_length=255)] | None = None
    context_markdown: str | None = None
    # ``creation_method`` has no ``allow_none=True`` in the POST schema; absent →
    # server_default='alerts_reports' applies; explicit null → HTTP 422.
    creation_method: str | msgspec.UnsetType = msgspec.UNSET
    # ``working_timeout`` Range(min=1).
    working_timeout: Annotated[int, Meta(ge=1)] = 3600
    selected_tabs: list[int] | None = None
    # ``report_format`` dump_default=ReportDataFormat.PNG — matches original
    # ``dump_default=ReportDataFormat.PNG``.  Sending the default ensures the
    # column default is not accidentally defeated by a None write.
    report_format: str = "PNG"
    active: bool = True
    force_screenshot: bool = False
    # NOTE: the original POST schema has ``custom_width`` but NO ``custom_height`` —
    # sending it is rejected with 422 (Marshmallow unknown-field RAISE).
    custom_width: int | None = None
    extra: dict[str, Any] = {}

    def __post_init__(self) -> None:
        if self.type not in _SCHEDULE_TYPES:
            raise msgspec.ValidationError(f"'type' must be one of {_SCHEDULE_TYPES}")
        if self.report_format not in _REPORT_DATA_FORMATS:
            raise msgspec.ValidationError(
                f"'report_format' must be one of {_REPORT_DATA_FORMATS}"
            )
        if (
            not isinstance(self.validator_type, msgspec.UnsetType)
            and self.validator_type not in _VALIDATOR_TYPES
        ):
            raise msgspec.ValidationError(
                f"'validator_type' must be one of {_VALIDATOR_TYPES}"
            )
        if self.timezone not in _VALID_TIMEZONES:
            raise msgspec.ValidationError("'timezone' is not a valid timezone string")
        if (
            not isinstance(self.creation_method, msgspec.UnsetType)
            and self.creation_method not in _CREATION_METHODS
        ):
            raise msgspec.ValidationError(
                f"'creation_method' must be one of {_CREATION_METHODS}"
            )


class ReportSchedulePutSchema(msgspec.Struct, forbid_unknown_fields=True):
    """PUT body. ``forbid_unknown_fields`` mirrors Marshmallow 3.x's default
    ``Meta.unknown = RAISE`` — unknown keys (``selected_tabs``,
    ``custom_height``) → 422."""

    # ``name`` Length(1, 150). No ``allow_none=True`` in the PUT schema — null must
    # be rejected so that ``{"name": null}`` returns 422 instead of hitting the NOT
    # NULL DB constraint.
    name: Annotated[str, Meta(min_length=1, max_length=150)] | msgspec.UnsetType = (
        msgspec.UNSET
    )
    # ``type`` has no allow_none in the PUT schema — null must be rejected, so
    # the type annotation excludes None.
    type: str | msgspec.UnsetType = msgspec.UNSET
    description: str | None | msgspec.UnsetType = msgspec.UNSET
    # ``crontab`` Length(1, 1000). No ``allow_none=True`` in the PUT schema — null
    # must be rejected so that ``{"crontab": null}`` returns 422 instead of hitting
    # the NOT NULL DB constraint.
    crontab: Annotated[str, Meta(min_length=1, max_length=1000)] | msgspec.UnsetType = (
        msgspec.UNSET
    )
    # ``timezone`` has no allow_none in the PUT schema — null must be rejected, so
    # the type annotation excludes None.
    timezone: str | msgspec.UnsetType = msgspec.UNSET
    sql: str | None | msgspec.UnsetType = msgspec.UNSET
    chart: int | None | msgspec.UnsetType = msgspec.UNSET
    dashboard: int | None | msgspec.UnsetType = msgspec.UNSET
    # ``database``/``owners``/``recipients`` have no ``allow_none=True`` in the PUT
    # schema — explicit null → 422. Allowing None for ``database`` would also skip
    # the ``if ... and database_id:`` alert guard and NULL the FK on update.
    database: int | msgspec.UnsetType = msgspec.UNSET
    owners: list[int] | msgspec.UnsetType = msgspec.UNSET
    recipients: list[ReportRecipientSchema] | msgspec.UnsetType = msgspec.UNSET
    validator_type: str | None | msgspec.UnsetType = msgspec.UNSET
    # ``validator_config_json`` has no ``allow_none=True`` in the PUT schema;
    # absent → not in output dict; explicit null → HTTP 422. Use UnsetType without a
    # None leg so absent → UNSET → filtered by filter_unset and explicit null →
    # DecodeError (422). Allowing None would write NULL to the column and crash alert
    # execution via ``json.loads(None)``.
    validator_config_json: ValidatorConfigJSON | msgspec.UnsetType = msgspec.UNSET
    # ``log_retention`` Range(min=0) for PUT. No ``None`` leg — explicit null must be
    # rejected at decode time (422), not written through to the column.
    log_retention: Annotated[int, Meta(ge=0)] | msgspec.UnsetType = msgspec.UNSET
    # ``grace_period`` Range(min=1) for PUT (no allow_none).
    grace_period: Annotated[int, Meta(ge=1)] | msgspec.UnsetType = msgspec.UNSET
    email_subject: str | None | msgspec.UnsetType = msgspec.UNSET
    context_markdown: str | None | msgspec.UnsetType = msgspec.UNSET
    creation_method: str | None | msgspec.UnsetType = msgspec.UNSET
    # ``working_timeout`` Range(min=1) for PUT — matches original.
    working_timeout: Annotated[int, Meta(ge=1)] | None | msgspec.UnsetType = (
        msgspec.UNSET
    )
    # NOTE: the original PUT schema has NO ``selected_tabs`` and NO ``custom_height`` —
    # sending either is rejected with 422 (Marshmallow unknown-field RAISE).
    # ``report_format`` has no allow_none in the PUT schema — null must be rejected,
    # so the type annotation excludes None.
    report_format: str | msgspec.UnsetType = msgspec.UNSET
    # ``active`` / ``force_screenshot`` have no ``allow_none=True`` in the PUT schema
    # — explicit null is a 422 upstream.  ``active=None`` additionally tripped
    # UpdateReportScheduleCommand's ``not self._data["active"]`` guard, silently
    # resetting WORKING→NOOP.
    active: bool | msgspec.UnsetType = msgspec.UNSET
    force_screenshot: bool | msgspec.UnsetType = msgspec.UNSET
    custom_width: int | None | msgspec.UnsetType = msgspec.UNSET
    # ``extra`` has no ``allow_none=True`` in the PUT schema; absent → not in output
    # dict; explicit null → HTTP 422. Use UnsetType without a None leg so absent →
    # UNSET → filtered by filter_unset and explicit null → DecodeError (422).
    # Allowing None would write the string ``"null"`` to the JSON column and
    # silently return ``{}`` on the next read (data corruption).
    extra: dict[str, Any] | msgspec.UnsetType = msgspec.UNSET

    def _validate_non_nullable_enums(self) -> None:
        """Validate fields that do not allow None (null rejected at decode time)."""
        _type = self.type
        if not isinstance(_type, msgspec.UnsetType):
            if _type not in _SCHEDULE_TYPES:
                raise msgspec.ValidationError(
                    f"'type' must be one of {_SCHEDULE_TYPES}"
                )
        _report_format = self.report_format
        if not isinstance(_report_format, msgspec.UnsetType):
            if _report_format not in _REPORT_DATA_FORMATS:
                raise msgspec.ValidationError(
                    f"'report_format' must be one of {_REPORT_DATA_FORMATS}"
                )
        _timezone = self.timezone
        if not isinstance(_timezone, msgspec.UnsetType):
            if _timezone not in _VALID_TIMEZONES:
                raise msgspec.ValidationError(
                    "'timezone' is not a valid timezone string"
                )

    def _validate_nullable_enums(self) -> None:
        """Validate fields that allow None (allow_none=True in original schema)."""
        _validator_type = self.validator_type
        if (
            not isinstance(_validator_type, msgspec.UnsetType)
            and _validator_type is not None
        ):
            if _validator_type not in _VALIDATOR_TYPES:
                raise msgspec.ValidationError(
                    f"'validator_type' must be one of {_VALIDATOR_TYPES}"
                )
        _creation_method = self.creation_method
        if (
            not isinstance(_creation_method, msgspec.UnsetType)
            and _creation_method is not None
        ):
            if _creation_method not in _CREATION_METHODS:
                raise msgspec.ValidationError(
                    f"'creation_method' must be one of {_CREATION_METHODS}"
                )

    def __post_init__(self) -> None:
        # Validate OneOf constraints only when the field was actually provided (not
        # UNSET). ``type``, ``timezone``, and ``report_format`` do NOT have
        # ``allow_none=True`` in the PUT schema, so their type annotations already
        # exclude None — msgspec rejects null at decode time.
        # ``validator_type`` and ``creation_method`` DO have ``allow_none=True``,
        # so the ``is not None`` guard is retained for those two fields only.
        self._validate_non_nullable_enums()
        self._validate_nullable_enums()


# Detail result Structs for GET /{pk}


class ChartRef(ModelStruct):
    """Chart reference embedded in report detail response.

    ``datasource_id`` / ``datasource_type`` expose the chart's datasource reference.
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

    # ``id`` appears inside ``result`` (not only the FAB envelope) — the
    # Alerts/Reports edit modal reads it.
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
    # ``show_columns`` includes custom_width but NOT custom_height.
    custom_width: int | None = None
    last_eval_dttm: str | None = None
    last_state: str | None = None
    context_markdown: str | None = None
    creation_method: str | None = None
    extra: dict[str, Any] | None = None
    last_value: float | None = None
    last_value_row_json: str | None = None
    report_format: str | None = None
    working_timeout: int | None = None
    # ``ReportSchedule.email_subject`` is String(255); without the cap an
    # over-long value reaches the INSERT and the driver error (carrying the
    # statement and its parameters) surfaces in the 422 body.
    email_subject: Annotated[str, Meta(max_length=255)] | None = None
    chart: ChartRef | None = None
    dashboard: ReportDashboardRef | None = None
    database: ReportDatabaseRef | None = None
    owners: list[UserRef] = []
    recipients: list[RecipientRef] = []
