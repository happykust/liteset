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
Ref: superset/exceptions.py, superset/views/error_handling.py
"""

from __future__ import annotations

import logging
from typing import Any

from litestar import MediaType, Request, Response

logger = logging.getLogger(__name__)


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


class SupersetSecurityException(SupersetException):
    status_code = 403
    message = "Access denied"


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


class SupersetTimeoutException(SupersetException):
    status_code = 504
    message = "Request timed out"


# --- Command-layer exceptions (replaces commands/base.py::CommandException) ---


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


class QueryObjectValidationError(SupersetValidationException):
    status_code = 400


class InvalidPostProcessingError(SupersetValidationException):
    status_code = 400


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


class QueryClauseValidationException(SupersetException):
    """Exception raised when a SQL clause is invalid."""

    status_code = 400


# --- Exception handlers ---


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
