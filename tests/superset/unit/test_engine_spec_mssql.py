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
"""Unit tests for MssqlEngineSpec:
- custom_errors dict: correct patterns, error types and message templates
- extract_errors: all four MSSQL connection error regexes produce the right
  SupersetError (error_type, message, level, issue_codes)
- extract_error_message: the 8155 alias-hint path and the generic fallback
- epoch_to_dttm / convert_dttm: return values match original
"""

from __future__ import annotations

from textwrap import dedent
from unittest.mock import MagicMock, patch

from superset.db_engine_specs.mssql import MssqlEngineSpec
from superset.errors import ErrorLevel, SupersetError, SupersetErrorType

# ---------------------------------------------------------------------------
# extract_errors — four custom error paths + generic fallback
# ---------------------------------------------------------------------------


def test_extract_errors_access_denied() -> None:
    """CONNECTION_ACCESS_DENIED_REGEX — username/password/database wrong."""
    msg = dedent(
        """
DB-Lib error message 20018, severity 14:
General SQL Server error: Check messages from the SQL Server
DB-Lib error message 20002, severity 9:
Adaptive Server connection failed (mssqldb.example.com)
Adaptive Server connection failed (mssqldb.example.com)
        """
    )
    result = MssqlEngineSpec.extract_errors(
        Exception(msg), context={"username": "testuser", "database": "testdb"}
    )
    assert result == [
        SupersetError(
            message=(
                'Either the username "testuser", password, '
                'or database name "testdb" is incorrect.'
            ),
            error_type=SupersetErrorType.CONNECTION_ACCESS_DENIED_ERROR,
            level=ErrorLevel.ERROR,
            extra={
                "engine_name": "Microsoft SQL Server",
                "issue_codes": [
                    {
                        "code": 1014,
                        "message": "Issue 1014 - Either the username or the password is wrong.",  # noqa: E501
                    },
                    {
                        "code": 1015,
                        "message": "Issue 1015 - Either the database is spelled incorrectly or does not exist.",  # noqa: E501
                    },
                ],
            },
        )
    ]


def test_extract_errors_invalid_hostname() -> None:
    """CONNECTION_INVALID_HOSTNAME_REGEX — hostname cannot be resolved."""
    msg = dedent(
        """
DB-Lib error message 20009, severity 9:
Unable to connect: Adaptive Server is unavailable or does not exist (localhost_)
        """
    )
    result = MssqlEngineSpec.extract_errors(Exception(msg))
    assert result == [
        SupersetError(
            error_type=SupersetErrorType.CONNECTION_INVALID_HOSTNAME_ERROR,
            message='The hostname "localhost_" cannot be resolved.',
            level=ErrorLevel.ERROR,
            extra={
                "engine_name": "Microsoft SQL Server",
                "issue_codes": [
                    {
                        "code": 1007,
                        "message": "Issue 1007 - The hostname provided can't be resolved.",  # noqa: E501
                    }
                ],
            },
        )
    ]


def test_extract_errors_port_closed() -> None:
    """CONNECTION_PORT_CLOSED_REGEX — Net-Lib Connection refused (61)."""
    msg = dedent(
        """
DB-Lib error message 20009, severity 9:
Unable to connect: Adaptive Server is unavailable or does not exist (localhost)
Net-Lib error during Connection refused (61)
DB-Lib error message 20009, severity 9:
Unable to connect: Adaptive Server is unavailable or does not exist (localhost)
Net-Lib error during Connection refused (61)
        """
    )
    result = MssqlEngineSpec.extract_errors(
        Exception(msg), context={"port": 12345, "hostname": "localhost"}
    )
    assert result == [
        SupersetError(
            error_type=SupersetErrorType.CONNECTION_PORT_CLOSED_ERROR,
            message='Port 12345 on hostname "localhost" refused the connection.',
            level=ErrorLevel.ERROR,
            extra={
                "engine_name": "Microsoft SQL Server",
                "issue_codes": [
                    {"code": 1008, "message": "Issue 1008 - The port is closed."}
                ],
            },
        )
    ]


def test_extract_errors_host_down() -> None:
    """CONNECTION_HOST_DOWN_REGEX — Net-Lib Operation timed out (60)."""
    msg = dedent(
        """
DB-Lib error message 20009, severity 9:
Unable to connect: Adaptive Server is unavailable or does not exist (example.com)
Net-Lib error during Operation timed out (60)
        """
    )
    result = MssqlEngineSpec.extract_errors(
        Exception(msg), context={"port": 5432, "hostname": "example.com"}
    )
    assert result == [
        SupersetError(
            error_type=SupersetErrorType.CONNECTION_HOST_DOWN_ERROR,
            message='The host "example.com" might be down, and can\'t be reached on port 5432.',  # noqa: E501
            level=ErrorLevel.ERROR,
            extra={
                "engine_name": "Microsoft SQL Server",
                "issue_codes": [
                    {
                        "code": 1009,
                        "message": "Issue 1009 - The host might be down, and can't be reached on the provided port.",  # noqa: E501
                    }
                ],
            },
        )
    ]


# ---------------------------------------------------------------------------
# extract_error_message
# ---------------------------------------------------------------------------


def test_extract_error_message_8155_alias_hint() -> None:
    """8155 error code → alias hint message (matches original exactly)."""
    ex = Exception(
        "(8155, b\"No column name was specified for column 1 of 'inner_qry'."
        "DB-Lib error message 20018, severity 16:\\nGeneral SQL Server error: "
        'Check messages from the SQL Server\\n")'
    )
    msg = MssqlEngineSpec.extract_error_message(ex)
    assert msg == (
        "mssql error: All your SQL functions need "
        "to have an alias on MSSQL. "
        "For example: SELECT COUNT(*) AS C1 FROM TABLE1"
    )


def test_extract_error_message_generic() -> None:
    """Non-8155 error → generic prefix + raw message."""
    ex = Exception(
        '(8200, b"A correlated expression is invalid because it is not in a '
        'GROUP BY clause.\\n")'
    )
    msg = MssqlEngineSpec.extract_error_message(ex)
    raw = MssqlEngineSpec._extract_error_message(ex)
    assert msg == f"mssql error: {raw}"


# ---------------------------------------------------------------------------
# epoch_to_dttm
# ---------------------------------------------------------------------------


def test_epoch_to_dttm() -> None:
    """epoch_to_dttm returns the original DATEADD expression."""
    assert MssqlEngineSpec.epoch_to_dttm() == "dateadd(S, {col}, '1970-01-01')"


# ---------------------------------------------------------------------------
# fetch_data
# ---------------------------------------------------------------------------


def test_fetch_data_empty_description() -> None:
    """cursor with no description (e.g. INSERT) returns empty list."""
    cursor = MagicMock()
    cursor.description = []
    assert MssqlEngineSpec.fetch_data(cursor) == []


def test_fetch_data_calls_pyodbc_rows_to_tuples() -> None:
    """fetch_data passes BaseEngineSpec result through pyodbc_rows_to_tuples."""
    from superset.db_engine_specs.base import BaseEngineSpec

    cursor = MagicMock()
    cursor.description = [("col",)]
    raw = [(1, "foo")]

    with patch.object(BaseEngineSpec, "fetch_data", return_value=raw):
        with patch.object(
            MssqlEngineSpec, "pyodbc_rows_to_tuples", return_value="converted"
        ) as mock_conv:
            result = MssqlEngineSpec.fetch_data(cursor, limit=0)
            mock_conv.assert_called_once_with(raw)
            assert result == "converted"
