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
"""SIP-40 compatible exception hierarchy for Superset.

Maps to Superset exceptions for frontend compatibility.
Ref: superset_old/exceptions.py, superset_old/views/error_handling.py

The hierarchy is ported 1:1 from the original Flask codebase so that
every ``except SomeException`` in business-logic modules keeps working.
The Litestar-specific exception handlers live at the bottom of this file.
"""

from __future__ import annotations

import logging
from typing import Any

from litestar import MediaType, Request, Response

logger = logging.getLogger(__name__)


# ======================================================================
# Base exception
# ======================================================================


class SupersetException(Exception):  # noqa: N818
    """Base exception for all Superset errors."""

    status_code: int = 500
    message: str = "An unexpected error occurred"

    def __init__(
        self,
        message: str = "",
        exception: Exception | None = None,
        error_type: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        if message:
            self.message = message
        self.extra: dict[str, Any] = extra if extra is not None else {}
        self._exception = exception
        self._error_type = error_type
        super().__init__(self.message)

    @property
    def error_type(self) -> str:
        return self._error_type or type(self).__name__

    def to_sip40(self) -> dict[str, Any]:
        """Convert to SIP-40 error dict for JSON response."""
        return {
            "message": self.message,
            "error_type": self.error_type,
            "level": "error",
            "extra": self.extra,
        }


# ======================================================================
# Generic / mid-level exceptions
# ======================================================================


class SupersetErrorException(SupersetException):
    """Exceptions with a single SupersetError-style payload."""

    def __init__(
        self,
        message: str = "",
        error_type: str | None = None,
        extra: dict[str, Any] | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message=message, error_type=error_type, extra=extra)
        if status_code is not None:
            self.status_code = status_code


class SupersetGenericErrorException(SupersetErrorException):
    """Exceptions that are too generic to have their own type."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(
            message=message,
            error_type="GENERIC_BACKEND_ERROR",
            status_code=status_code,
        )


class SupersetErrorFromParamsException(SupersetErrorException):
    """Exceptions that are constructed from explicit error_type / level params."""

    def __init__(
        self,
        error_type: str,
        message: str,
        level: str = "error",
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message=message, error_type=error_type, extra=extra)

    def to_sip40(self) -> dict[str, Any]:
        payload = super().to_sip40()
        return payload


class SupersetErrorsException(SupersetException):
    """Exceptions with multiple SIP-40 error payloads."""

    def __init__(
        self,
        errors: list[dict[str, Any]] | None = None,
        status_code: int | None = None,
        message: str = "",
    ) -> None:
        self.errors = errors or []
        if status_code is not None:
            self.status_code = status_code
        super().__init__(message=message or str(self.errors))

    def to_sip40(self) -> dict[str, Any]:
        if self.errors:
            return (
                self.errors[0]
                if len(self.errors) == 1
                else {
                    "message": self.message,
                    "error_type": self.error_type,
                    "level": "error",
                    "extra": {"errors": self.errors},
                }
            )
        return super().to_sip40()


# ======================================================================
# Specific exception classes  (ported 1:1 from original)
# ======================================================================


class SupersetSyntaxErrorException(SupersetErrorsException):
    status_code = 422


class SupersetTimeoutException(SupersetErrorFromParamsException):
    status_code = 408


class SupersetGenericDBErrorException(SupersetErrorFromParamsException):
    status_code = 400

    def __init__(
        self,
        message: str,
        level: str = "error",
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            error_type="GENERIC_DB_ENGINE_ERROR",
            message=message,
            level=level,
            extra=extra,
        )


class SupersetTemplateParamsErrorException(SupersetErrorFromParamsException):
    status_code = 400

    def __init__(
        self,
        message: str,
        error_type: str,
        level: str = "error",
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            error_type=error_type,
            message=message,
            level=level,
            extra=extra,
        )


class SupersetSecurityException(SupersetException):
    status_code = 403
    message = "Access denied"

    def __init__(
        self,
        message: str = "",
        payload: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        self.payload = payload
        super().__init__(message=message or self.__class__.message, **kwargs)


class SupersetVizException(SupersetErrorsException):
    status_code = 400


class NoDataException(SupersetException):
    status_code = 400


class NullValueException(SupersetException):
    status_code = 400


class SupersetTemplateException(SupersetException):
    status_code = 422


class SpatialException(SupersetException):
    pass


class CertificateException(SupersetException):
    message = "Invalid certificate"


class DatabaseNotFound(SupersetException):
    status_code = 400


class MissingUserContextException(SupersetException):
    status_code = 422


class SupersetValidationException(SupersetException):
    status_code = 422
    message = "Validation error"

    def __init__(
        self,
        message: str = "",
        extra: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message=message, extra=extra, **kwargs)


class SupersetNotFoundError(SupersetException):
    status_code = 404
    message = "Resource not found"


class QueryObjectValidationError(SupersetException):
    status_code = 400


class AdvancedDataTypeResponseError(SupersetException):
    status_code = 400


class InvalidPostProcessingError(SupersetException):
    status_code = 400


class CacheLoadError(SupersetException):
    status_code = 404


class QueryClauseValidationException(SupersetException):
    """Exception raised when a SQL clause is invalid."""

    status_code = 400


class DashboardImportException(SupersetException):
    pass


class DatasetInvalidPermissionEvaluationException(SupersetException):
    """
    When a dataset can't compute its permission name.
    """


class SerializationError(SupersetException):
    pass


class InvalidPayloadFormatError(SupersetErrorException):
    status_code = 400

    def __init__(self, message: str = "Request payload has incorrect format") -> None:
        super().__init__(
            message=message,
            error_type="INVALID_PAYLOAD_FORMAT_ERROR",
        )


class InvalidPayloadSchemaError(SupersetErrorException):
    status_code = 422

    def __init__(self, messages: dict[str, Any] | None = None) -> None:
        super().__init__(
            message="An error happened when validating the request",
            error_type="INVALID_PAYLOAD_SCHEMA_ERROR",
            extra={"messages": messages or {}},
        )


class SupersetCancelQueryException(SupersetException):
    status_code = 422


class QueryNotFoundException(SupersetException):
    status_code = 404


class ColumnNotFoundException(SupersetException):
    status_code = 404


class SupersetMarshmallowValidationError(SupersetErrorException):
    """
    Exception to be raised for Marshmallow validation errors.
    """

    status_code = 422

    def __init__(
        self,
        messages: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            message="The schema of the submitted payload is invalid.",
            error_type="MARSHMALLOW_ERROR",
            extra={"messages": messages or {}, "payload": payload or {}},
        )


class SupersetParseError(SupersetValidationException):
    """Exception raised when we fail to parse SQL."""

    status_code = 422

    def __init__(
        self,
        sql: str,
        engine: str | None = None,
        message: str | None = None,
        highlight: str | None = None,
        line: int | None = None,
        column: int | None = None,
    ) -> None:
        if message is None:
            parts = ["Error parsing"]
            if highlight:
                parts.append(f" near '{highlight}'")
            if line:
                parts.append(f" at line {line}")
                if column:
                    parts.append(f":{column}")
            message = "".join(parts)

        super().__init__(
            message=message,
            extra={"sql": sql, "engine": engine, "line": line, "column": column},
        )

    def __str__(self) -> str:
        return self.message


class OAuth2RedirectError(SupersetErrorException):
    """
    Exception used to start OAuth2 dance for personal tokens.

    The exception requires 3 parameters:

    - The URL that starts the OAuth2 dance.
    - The UUID of the browser tab where OAuth2 started, so that the newly
      opened tab where OAuth2 happens can communicate with the original tab
      to inform that OAuth2 was successful (or not).
    - The redirect URL, so that the original tab can validate that the
      message from the second tab is coming from a valid origin.
    """

    def __init__(self, url: str, tab_id: str, redirect_uri: str) -> None:
        super().__init__(
            message="You don't have permission to access the data.",
            error_type="OAUTH2_REDIRECT",
            extra={"url": url, "tab_id": tab_id, "redirect_uri": redirect_uri},
        )


class OAuth2Error(SupersetErrorException):
    """
    Exception for when OAuth2 goes wrong.
    """

    def __init__(self, error: str) -> None:
        super().__init__(
            message="Something went wrong while doing OAuth2",
            error_type="OAUTH2_REDIRECT_ERROR",
            extra={"error": error},
        )


class SupersetDisallowedSQLFunctionException(SupersetErrorException):
    """
    Disallowed function found on SQL statement.
    """

    def __init__(self, functions: set[str]) -> None:
        super().__init__(
            message=f"SQL statement contains disallowed function(s): {functions}",
            error_type="SYNTAX_ERROR",
        )


class CreateKeyValueDistributedLockFailedException(Exception):  # noqa: N818
    """
    Exception to signalize failure to acquire lock.
    """


class DeleteKeyValueDistributedLockFailedException(Exception):  # noqa: N818
    """
    Exception to signalize failure to delete lock.
    """


class DatabaseNotFoundException(SupersetErrorException):
    status_code = 404

    def __init__(self, message: str = "Database not found") -> None:
        super().__init__(
            message=message,
            error_type="DATABASE_NOT_FOUND_ERROR",
        )


class TableNotFoundException(SupersetErrorException):
    status_code = 404

    def __init__(self, message: str = "Table not found") -> None:
        super().__init__(
            message=message,
            error_type="TABLE_NOT_FOUND_ERROR",
        )


class SupersetDMLNotAllowedException(SupersetErrorException):
    def __init__(self) -> None:
        super().__init__(
            message=(
                "This database does not allow for DDL/DML, but the query mutates "
                "data. Please contact your administrator for more assistance."
            ),
            error_type="DML_NOT_ALLOWED_ERROR",
        )


class SupersetInvalidCTASException(SupersetErrorException):
    def __init__(self) -> None:
        super().__init__(
            message=(
                "CTAS (create table as select) can only be run with a query where "
                "the last statement is a SELECT. Please make sure your query has "
                "a SELECT as its last statement. Then, try running your query again."
            ),
            error_type="INVALID_CTAS_QUERY_ERROR",
        )


class SupersetInvalidCVASException(SupersetErrorException):
    def __init__(self) -> None:
        super().__init__(
            message=(
                "CVAS (create view as select) can only be run with a query with "
                "a single SELECT statement. Please make sure your query has only "
                "a SELECT statement. Then, try running your query again."
            ),
            error_type="INVALID_CVAS_QUERY_ERROR",
        )


class SupersetResultsBackendNotConfigureException(SupersetErrorException):
    def __init__(self) -> None:
        super().__init__(
            message="Results backend is not configured.",
            error_type="RESULTS_BACKEND_NOT_CONFIGURED_ERROR",
        )


class ScreenshotImageNotAvailableException(SupersetException):
    status_code = 404


# ======================================================================
# Command-layer exceptions (replaces commands/base.py::CommandException)
# ======================================================================


class CommandException(SupersetException):
    """Base for command-layer errors."""

    status_code = 500

    def __init__(
        self, message: str = "", exceptions: list[Exception] | None = None
    ) -> None:
        self.exceptions = exceptions or []
        super().__init__(message=message)

    def to_sip40(self) -> dict[str, Any]:
        """Convert to SIP-40 error dict, including sub-errors."""
        payload = super().to_sip40()
        if self.exceptions:
            payload["errors"] = [str(e) for e in self.exceptions]
        return payload


class CommandInvalidError(CommandException):
    status_code = 422


class ObjectNotFoundError(CommandException):
    status_code = 404

    def __init__(self, object_type: str, object_id: str | int | None = None) -> None:
        msg = f"{object_type} "
        if object_id is not None:
            msg += f'"{object_id}" '
        msg += "not found."
        super().__init__(message=msg)


class ForbiddenError(CommandException):
    status_code = 403
    message = "Action is forbidden"


class CreateFailedError(CommandException):
    status_code = 500
    message = "Create failed"


class UpdateFailedError(CommandException):
    status_code = 500
    message = "Update failed"


class DeleteFailedError(CommandException):
    status_code = 500
    message = "Delete failed"


class ImportFailedError(CommandException):
    status_code = 500
    message = "Import failed"


# ======================================================================
# Litestar exception handlers
# ======================================================================


def superset_exception_handler(
    request: Request[Any, Any, Any], exc: SupersetException
) -> Response[Any]:
    """SIP-40 compatible error response handler.

    Includes both ``message`` (FAB compat) and ``detail`` (Litestar compat)
    keys in the response body for backward compatibility.
    """
    error_detail = exc.to_sip40()
    return Response(
        content={
            "errors": [error_detail],
            "message": exc.message,
            "detail": exc.message,
        },
        status_code=exc.status_code,
        media_type=MediaType.JSON,
    )


def generic_exception_handler(
    request: Request[Any, Any, Any], exc: Exception
) -> Response[Any]:
    """Catch-all for unhandled exceptions.

    Preserves status_code from Litestar HTTP exceptions (404, 405, etc.)
    while wrapping them in SIP-40 format. Logs unhandled non-HTTP
    exceptions for production diagnostics.
    """
    from litestar.exceptions import HTTPException

    if isinstance(exc, HTTPException):
        # Only expose detail for 4xx errors; mask 5xx internals
        if exc.status_code >= 500:
            logger.exception(
                "Unhandled HTTP %d on %s %s",
                exc.status_code,
                request.method,
                request.url,
            )
            detail = "An unexpected error occurred"
        else:
            detail = exc.detail
        return Response(
            content={
                "errors": [
                    {
                        "message": detail,
                        "error_type": type(exc).__name__,
                        "level": "error",
                        "extra": {},
                    }
                ],
                "message": detail,
                "detail": detail,
            },
            status_code=exc.status_code,
        )
    logger.exception("Unhandled exception on %s %s", request.method, request.url)
    return Response(
        content={
            "errors": [
                {
                    "message": "An unexpected error occurred",
                    "error_type": "UNKNOWN_ERROR",
                    "level": "error",
                    "extra": {},
                }
            ],
            "message": "An unexpected error occurred",
            "detail": "An unexpected error occurred",
        },
        status_code=500,
    )
