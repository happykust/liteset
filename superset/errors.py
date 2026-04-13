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
"""Error types and structures -- Flask-free port of superset_old/errors.py."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional


class SupersetErrorType(str, Enum):
    """
    Types of errors that can exist within Superset.

    Kept in sync with superset_old/errors.py and the frontend Query.ts.
    """

    # Frontend errors
    FRONTEND_CSRF_ERROR = "FRONTEND_CSRF_ERROR"
    FRONTEND_NETWORK_ERROR = "FRONTEND_NETWORK_ERROR"
    FRONTEND_TIMEOUT_ERROR = "FRONTEND_TIMEOUT_ERROR"

    # DB Engine errors
    GENERIC_DB_ENGINE_ERROR = "GENERIC_DB_ENGINE_ERROR"
    COLUMN_DOES_NOT_EXIST_ERROR = "COLUMN_DOES_NOT_EXIST_ERROR"
    TABLE_DOES_NOT_EXIST_ERROR = "TABLE_DOES_NOT_EXIST_ERROR"
    SCHEMA_DOES_NOT_EXIST_ERROR = "SCHEMA_DOES_NOT_EXIST_ERROR"
    CONNECTION_INVALID_USERNAME_ERROR = "CONNECTION_INVALID_USERNAME_ERROR"
    CONNECTION_INVALID_PASSWORD_ERROR = "CONNECTION_INVALID_PASSWORD_ERROR"  # noqa: S105
    CONNECTION_INVALID_HOSTNAME_ERROR = "CONNECTION_INVALID_HOSTNAME_ERROR"
    CONNECTION_PORT_CLOSED_ERROR = "CONNECTION_PORT_CLOSED_ERROR"
    CONNECTION_INVALID_PORT_ERROR = "CONNECTION_INVALID_PORT_ERROR"
    CONNECTION_HOST_DOWN_ERROR = "CONNECTION_HOST_DOWN_ERROR"
    CONNECTION_ACCESS_DENIED_ERROR = "CONNECTION_ACCESS_DENIED_ERROR"
    CONNECTION_UNKNOWN_DATABASE_ERROR = "CONNECTION_UNKNOWN_DATABASE_ERROR"
    CONNECTION_DATABASE_PERMISSIONS_ERROR = "CONNECTION_DATABASE_PERMISSIONS_ERROR"
    CONNECTION_MISSING_PARAMETERS_ERROR = "CONNECTION_MISSING_PARAMETERS_ERROR"
    OBJECT_DOES_NOT_EXIST_ERROR = "OBJECT_DOES_NOT_EXIST_ERROR"
    SYNTAX_ERROR = "SYNTAX_ERROR"
    CONNECTION_DATABASE_TIMEOUT = "CONNECTION_DATABASE_TIMEOUT"

    # Viz errors
    VIZ_GET_DF_ERROR = "VIZ_GET_DF_ERROR"
    UNKNOWN_DATASOURCE_TYPE_ERROR = "UNKNOWN_DATASOURCE_TYPE_ERROR"
    FAILED_FETCHING_DATASOURCE_INFO_ERROR = "FAILED_FETCHING_DATASOURCE_INFO_ERROR"

    # Security access errors
    TABLE_SECURITY_ACCESS_ERROR = "TABLE_SECURITY_ACCESS_ERROR"
    DATASOURCE_SECURITY_ACCESS_ERROR = "DATASOURCE_SECURITY_ACCESS_ERROR"
    DATABASE_SECURITY_ACCESS_ERROR = "DATABASE_SECURITY_ACCESS_ERROR"
    QUERY_SECURITY_ACCESS_ERROR = "QUERY_SECURITY_ACCESS_ERROR"
    MISSING_OWNERSHIP_ERROR = "MISSING_OWNERSHIP_ERROR"
    USER_ACTIVITY_SECURITY_ACCESS_ERROR = "USER_ACTIVITY_SECURITY_ACCESS_ERROR"
    DASHBOARD_SECURITY_ACCESS_ERROR = "DASHBOARD_SECURITY_ACCESS_ERROR"
    CHART_SECURITY_ACCESS_ERROR = "CHART_SECURITY_ACCESS_ERROR"
    OAUTH2_REDIRECT = "OAUTH2_REDIRECT"
    OAUTH2_REDIRECT_ERROR = "OAUTH2_REDIRECT_ERROR"

    # Other errors
    BACKEND_TIMEOUT_ERROR = "BACKEND_TIMEOUT_ERROR"
    DATABASE_NOT_FOUND_ERROR = "DATABASE_NOT_FOUND_ERROR"
    TABLE_NOT_FOUND_ERROR = "TABLE_NOT_FOUND_ERROR"

    # Sql Lab errors
    MISSING_TEMPLATE_PARAMS_ERROR = "MISSING_TEMPLATE_PARAMS_ERROR"
    INVALID_TEMPLATE_PARAMS_ERROR = "INVALID_TEMPLATE_PARAMS_ERROR"
    RESULTS_BACKEND_NOT_CONFIGURED_ERROR = "RESULTS_BACKEND_NOT_CONFIGURED_ERROR"
    DML_NOT_ALLOWED_ERROR = "DML_NOT_ALLOWED_ERROR"
    INVALID_CTAS_QUERY_ERROR = "INVALID_CTAS_QUERY_ERROR"
    INVALID_CVAS_QUERY_ERROR = "INVALID_CVAS_QUERY_ERROR"
    SQLLAB_TIMEOUT_ERROR = "SQLLAB_TIMEOUT_ERROR"
    RESULTS_BACKEND_ERROR = "RESULTS_BACKEND_ERROR"
    ASYNC_WORKERS_ERROR = "ASYNC_WORKERS_ERROR"
    ADHOC_SUBQUERY_NOT_ALLOWED_ERROR = "ADHOC_SUBQUERY_NOT_ALLOWED_ERROR"
    INVALID_SQL_ERROR = "INVALID_SQL_ERROR"
    RESULT_TOO_LARGE_ERROR = "RESULT_TOO_LARGE_ERROR"

    # Generic errors
    GENERIC_COMMAND_ERROR = "GENERIC_COMMAND_ERROR"
    GENERIC_BACKEND_ERROR = "GENERIC_BACKEND_ERROR"

    # API errors
    INVALID_PAYLOAD_FORMAT_ERROR = "INVALID_PAYLOAD_FORMAT_ERROR"
    INVALID_PAYLOAD_SCHEMA_ERROR = "INVALID_PAYLOAD_SCHEMA_ERROR"
    MARSHMALLOW_ERROR = "MARSHMALLOW_ERROR"

    # Report errors
    REPORT_NOTIFICATION_ERROR = "REPORT_NOTIFICATION_ERROR"


class ErrorLevel(str, Enum):
    """
    Levels of errors that can exist within Superset.
    """

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


# User-facing issue codes displayed in the UI as "Issue <code> - <message>".
# Ported 1:1 from ``superset_old/errors.py::ISSUE_CODES``; the numbers are
# load-bearing because they appear in Cypress snapshots and the frontend
# error documentation links point at them.
ISSUE_CODES: dict[int, str] = {
    1000: "The datasource is too large to query.",
    1001: "The database is under an unusual load.",
    1002: "The database returned an unexpected error.",
    1003: (
        "There is a syntax error in the SQL query. "
        "Perhaps there was a misspelling or a typo."
    ),
    1004: "The column was deleted or renamed in the database.",
    1005: "The table was deleted or renamed in the database.",
    1006: "One or more parameters specified in the query are missing.",
    1007: "The hostname provided can't be resolved.",
    1008: "The port is closed.",
    1009: "The host might be down, and can't be reached on the provided port.",
    1010: "Superset encountered an error while running a command.",
    1011: "Superset encountered an unexpected error.",
    1012: "The username provided when connecting to a database is not valid.",
    1013: "The password provided when connecting to a database is not valid.",
    1014: "Either the username or the password is wrong.",
    1015: "Either the database is spelled incorrectly or does not exist.",
    1016: "The schema was deleted or renamed in the database.",
    1017: "User doesn't have the proper permissions.",
    1018: "One or more parameters needed to configure a database are missing.",
    1019: "The submitted payload has the incorrect format.",
    1020: "The submitted payload has the incorrect schema.",
    1021: "Results backend needed for asynchronous queries is not configured.",
    1022: "Database does not allow data manipulation.",
    1023: (
        "The CTAS (create table as select) doesn't have a "
        "SELECT statement at the end. Please make sure your query has a "
        "SELECT as its last statement. Then, try running your query again."
    ),
    1024: "CVAS (create view as select) query has more than one statement.",
    1025: "CVAS (create view as select) query is not a SELECT statement.",
    1026: "Query is too complex and takes too long to run.",
    1027: "The database is currently running too many queries.",
    1028: "One or more parameters specified in the query are malformed.",
    1029: "The object does not exist in the given database.",
    1030: "The query has a syntax error.",
    1031: "The results backend no longer has the data from the query.",
    1032: "The query associated with the results was deleted.",
    1033: (
        "The results stored in the backend were stored in a "
        "different format, and no longer can be deserialized."
    ),
    1034: "The port number is invalid.",
    1035: "Failed to start remote query on a worker.",
    1036: "The database was deleted.",
    1037: "Custom SQL fields cannot contain sub-queries.",
    1040: "The submitted payload failed validation.",
    1041: "The result size exceeds the allowed limit.",
}


ERROR_TYPES_TO_ISSUE_CODES_MAPPING: dict[SupersetErrorType, list[int]] = {
    SupersetErrorType.ADHOC_SUBQUERY_NOT_ALLOWED_ERROR: [1037],
    SupersetErrorType.BACKEND_TIMEOUT_ERROR: [1000, 1001],
    SupersetErrorType.GENERIC_DB_ENGINE_ERROR: [1002],
    SupersetErrorType.COLUMN_DOES_NOT_EXIST_ERROR: [1003, 1004],
    SupersetErrorType.TABLE_DOES_NOT_EXIST_ERROR: [1003, 1005],
    SupersetErrorType.SCHEMA_DOES_NOT_EXIST_ERROR: [1003, 1016],
    SupersetErrorType.MISSING_TEMPLATE_PARAMS_ERROR: [1006],
    SupersetErrorType.INVALID_TEMPLATE_PARAMS_ERROR: [1028],
    SupersetErrorType.RESULTS_BACKEND_NOT_CONFIGURED_ERROR: [1021],
    SupersetErrorType.DML_NOT_ALLOWED_ERROR: [1022],
    SupersetErrorType.CONNECTION_INVALID_HOSTNAME_ERROR: [1007],
    SupersetErrorType.CONNECTION_PORT_CLOSED_ERROR: [1008],
    SupersetErrorType.CONNECTION_HOST_DOWN_ERROR: [1009],
    SupersetErrorType.GENERIC_COMMAND_ERROR: [1010],
    SupersetErrorType.GENERIC_BACKEND_ERROR: [1011],
    SupersetErrorType.CONNECTION_INVALID_USERNAME_ERROR: [1012],
    SupersetErrorType.CONNECTION_INVALID_PASSWORD_ERROR: [1013],
    SupersetErrorType.CONNECTION_ACCESS_DENIED_ERROR: [1014, 1015],
    SupersetErrorType.CONNECTION_UNKNOWN_DATABASE_ERROR: [1015],
    SupersetErrorType.CONNECTION_DATABASE_PERMISSIONS_ERROR: [1017],
    SupersetErrorType.CONNECTION_MISSING_PARAMETERS_ERROR: [1018],
    SupersetErrorType.INVALID_PAYLOAD_FORMAT_ERROR: [1019],
    SupersetErrorType.INVALID_PAYLOAD_SCHEMA_ERROR: [1020],
    SupersetErrorType.INVALID_CTAS_QUERY_ERROR: [1023],
    SupersetErrorType.INVALID_CVAS_QUERY_ERROR: [1024, 1025],
    SupersetErrorType.INVALID_SQL_ERROR: [1003],
    SupersetErrorType.SQLLAB_TIMEOUT_ERROR: [1026, 1027],
    SupersetErrorType.OBJECT_DOES_NOT_EXIST_ERROR: [1029],
    SupersetErrorType.SYNTAX_ERROR: [1030],
    SupersetErrorType.RESULTS_BACKEND_ERROR: [1031, 1032, 1033],
    SupersetErrorType.CONNECTION_INVALID_PORT_ERROR: [1034],
    SupersetErrorType.ASYNC_WORKERS_ERROR: [1035],
    SupersetErrorType.DATABASE_NOT_FOUND_ERROR: [1011, 1036],
    SupersetErrorType.CONNECTION_DATABASE_TIMEOUT: [1001, 1009],
    SupersetErrorType.MARSHMALLOW_ERROR: [1040],
    SupersetErrorType.RESULT_TOO_LARGE_ERROR: [1041],
}


@dataclass
class SupersetError:
    """
    An error that is returned to a client.
    """

    message: str
    error_type: SupersetErrorType
    level: ErrorLevel
    extra: Optional[dict[str, Any]] = None

    def __post_init__(self) -> None:
        """Attach ``issue_codes`` to ``extra`` based on ``error_type``.

        Mirrors ``superset_old/errors.py::SupersetError.__post_init__``.
        The frontend uses these codes to render "Issue <code>" links, and
        test fixtures snapshot them — so they must be present on any
        client-facing error response.
        """
        issue_codes = ERROR_TYPES_TO_ISSUE_CODES_MAPPING.get(self.error_type)
        if issue_codes:
            self.extra = self.extra or {}
            if "issue_codes" not in self.extra:
                self.extra["issue_codes"] = [
                    {
                        "code": code,
                        "message": f"Issue {code} - {ISSUE_CODES[code]}",
                    }
                    for code in issue_codes
                ]

    def to_dict(self) -> dict[str, Any]:
        rv: dict[str, Any] = {
            "message": self.message,
            "error_type": self.error_type,
        }
        if self.extra:
            rv["extra"] = self.extra
        return rv
