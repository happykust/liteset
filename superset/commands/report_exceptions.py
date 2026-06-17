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
"""Report schedule execution exceptions.

Used by the Celery-based report execution pipeline
(:mod:`superset.commands.report_execute`).
"""

from __future__ import annotations

import math
from typing import Any

from superset.exceptions import (
    CommandException,
    CommandInvalidError,
    CreateFailedError,
    ForbiddenError,
    SupersetErrorsException,
)


class ReportScheduleNotFoundError(CommandException):
    status_code = 404
    message = "Report Schedule not found."


class ReportScheduleCreateFailedError(CreateFailedError):
    message = "Report Schedule could not be created."


class ReportScheduleUpdateFailedError(CreateFailedError):
    message = "Report Schedule could not be updated."


class ReportScheduleDeleteFailedError(CommandException):
    message = "Report Schedule delete failed."


class ReportScheduleForbiddenError(ForbiddenError):
    message = "Changing this report is forbidden"


class ReportScheduleExecuteUnexpectedError(CommandException):
    message = "Report Schedule execution got an unexpected error."


class ReportScheduleUnexpectedError(CommandException):
    message = "Report schedule unexpected error"


class ReportScheduleStateNotFoundError(CommandException):
    message = "Report Schedule state not found"


class ReportSchedulePreviousWorkingError(CommandException):
    status_code = 429
    message = "Report Schedule is still working, refusing to re-compute."


class ReportScheduleWorkingTimeoutError(CommandException):
    status_code = 408
    message = "Report Schedule reached a working timeout."


class ReportScheduleAlertGracePeriodError(CommandException):
    status_code = 429
    message = "Alert fired during grace period."


class ReportScheduleScreenshotFailedError(CommandException):
    message = "Report Schedule execution failed when generating a screenshot."


class ReportScheduleScreenshotTimeout(CommandException):
    status_code = 408
    message = "A timeout occurred while taking a screenshot."


class ReportSchedulePdfFailedError(CommandException):
    message = "Report Schedule execution failed when generating a pdf."


class ReportScheduleCsvFailedError(CommandException):
    message = "Report Schedule execution failed when generating a csv."


class ReportScheduleCsvTimeout(CommandException):
    status_code = 408
    message = "A timeout occurred while generating a csv."


class ReportScheduleDataFrameFailedError(CommandException):
    message = "Report Schedule execution failed when generating a dataframe."


class ReportScheduleDataFrameTimeout(CommandException):
    status_code = 408
    message = "A timeout occurred while generating a dataframe."


class ReportScheduleSystemErrorsException(CommandException, SupersetErrorsException):
    errors: list[dict[str, object]] = []
    message = "Report schedule system error"


class ReportScheduleClientErrorsException(CommandException, SupersetErrorsException):
    status_code = 400
    errors: list[dict[str, object]] = []
    message = "Report schedule client error"


class AlertQueryError(CommandException):
    status_code = 400
    message = "Alert found an error while executing a query."


class AlertQueryTimeout(CommandException):
    status_code = 408
    message = "A timeout occurred while executing the query."


class AlertQueryInvalidTypeError(CommandException):
    status_code = 422
    message = "Alert query returned a non-number value."


class AlertQueryMultipleRowsError(CommandException):
    status_code = 422
    message = "Alert query returned more than one row."


class AlertQueryMultipleColumnsError(CommandException):
    status_code = 422
    message = "Alert query returned more than one column."


class AlertValidatorConfigError(CommandException):
    status_code = 422
    message = "Alert validator config error."


class ReportSchedulePruneLogError(CommandException):
    message = "An error occurred while pruning logs "


class ReportScheduleValidationError(Exception):
    """
    Per-field validation error; ``field_name`` drives the
    ``{field: [messages]}`` response shape.
    """

    def __init__(self, message: str | list[str], field_name: str = "_schema") -> None:
        self.messages: list[str] = (
            [message] if isinstance(message, str) else list(message)
        )
        self.field_name = field_name
        super().__init__("; ".join(self.messages))


class DatabaseNotFoundValidationError(ReportScheduleValidationError):
    def __init__(self) -> None:
        super().__init__("Database does not exist", field_name="database")


class DashboardNotFoundValidationError(ReportScheduleValidationError):
    def __init__(self) -> None:
        super().__init__("Dashboard does not exist", field_name="dashboard")


class ChartNotFoundValidationError(ReportScheduleValidationError):
    def __init__(self) -> None:
        super().__init__("Chart does not exist", field_name="chart")


class ReportScheduleAlertRequiredDatabaseValidationError(ReportScheduleValidationError):
    def __init__(self) -> None:
        super().__init__("Database is required for alerts", field_name="database")


class ReportScheduleOnlyChartOrDashboardError(ReportScheduleValidationError):
    def __init__(self) -> None:
        super().__init__("Choose a chart or dashboard not both", field_name="chart")


class ReportScheduleEitherChartOrDashboardError(ReportScheduleValidationError):
    def __init__(self) -> None:
        super().__init__(
            "Must choose either a chart or a dashboard", field_name="chart"
        )


class ChartNotSavedValidationError(ReportScheduleValidationError):
    def __init__(self) -> None:
        super().__init__(
            "Please save your chart first, then try creating a new email report.",
            field_name="chart",
        )


class DashboardNotSavedValidationError(ReportScheduleValidationError):
    def __init__(self) -> None:
        super().__init__(
            "Please save your dashboard first, then try creating a new email report.",
            field_name="dashboard",
        )


class ReportScheduleNameUniquenessValidationError(ReportScheduleValidationError):
    def __init__(self, report_type: str, name: str) -> None:
        message = f'A report named "{name}" already exists'
        if report_type == "Alert":
            message = f'An alert named "{name}" already exists'
        super().__init__([message], field_name="name")


class ReportScheduleFrequencyNotAllowed(ReportScheduleValidationError):  # noqa: N818
    def __init__(
        self,
        report_type: str = "Report",
        minimum_interval: int = 120,
    ) -> None:
        interval_in_minutes = math.ceil(minimum_interval / 60)
        super().__init__(
            f"{report_type} schedule frequency exceeding limit."
            " Please configure a schedule with a minimum interval of"
            f" {interval_in_minutes} minutes per execution.",
            field_name="crontab",
        )


class ReportScheduleCreationMethodUniquenessValidationError(CommandException):
    status_code = 409
    message = "Resource already has an attached report."


class ReportScheduleInvalidError(CommandInvalidError):
    """
    Aggregates per-field validation errors; ``normalized_messages``
    produces the ``{field: [msg]}`` response.
    """

    status_code = 422
    message = "Report Schedule parameters are invalid."

    def __init__(
        self, exceptions: list[ReportScheduleValidationError] | None = None
    ) -> None:
        self._invalid_exceptions: list[ReportScheduleValidationError] = exceptions or []
        super().__init__(
            message=self.message,
            exceptions=list(self._invalid_exceptions),
        )

    def normalized_messages(self) -> dict[str, Any]:
        errors: dict[str, list[str]] = {}
        for exc in self._invalid_exceptions:
            errors.setdefault(exc.field_name, []).extend(exc.messages)
        return errors
