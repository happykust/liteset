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

Ported 1:1 from ``superset_old/commands/report/exceptions.py``.
These are used by the Celery-based report execution pipeline
(:mod:`superset.commands.report_execute`).
"""
from __future__ import annotations

from superset.exceptions import CommandException, SupersetErrorsException


class ReportScheduleNotFoundError(CommandException):
    status_code = 404
    message = "Report Schedule not found."


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


# ---------------------------------------------------------------------------
# Alert-specific exceptions
# ---------------------------------------------------------------------------


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
