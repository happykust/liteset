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
from typing import Any, TYPE_CHECKING

from litestar import MediaType, Request, Response

if TYPE_CHECKING:
    from superset.errors import SupersetError

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

    @property
    def status(self) -> int:
        """Backward-compat alias for ``status_code``.

        The original Flask-era codebase used ``exc.status`` (the FAB / Werkzeug
        convention). Litestar uses ``status_code``. Both are supported to avoid
        ``AttributeError`` in Celery tasks, CLI commands, or any non-Litestar
        code path that still reads the old attribute name.
        """
        return self.status_code

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
    """Exceptions with a single :class:`SupersetError` payload.

    Ported 1:1 from ``superset_old/exceptions.py::SupersetErrorException``:
    accepts a ``SupersetError`` instance (the SIP-40 error dataclass) plus
    an optional HTTP ``status`` override. The full error object is kept on
    ``self.error`` so callers can inspect ``error_type`` / ``level`` / ``extra``,
    matching the original behaviour.
    """

    def __init__(
        self, error: SupersetError, status: int | None = None
    ) -> None:
        from superset.errors import SupersetError as _SupersetError

        if not isinstance(error, _SupersetError):
            raise TypeError(
                "SupersetErrorException requires a SupersetError instance "
                f"(got {type(error).__name__})"
            )
        self.error = error
        super().__init__(message=error.message)
        if status is not None:
            self.status_code = status

    def to_sip40(self) -> dict[str, Any]:
        """Return the SIP-40 error dict (mirrors original ``to_dict``).

        Original ``SupersetError.to_dict`` only emits ``message``, ``error_type``
        and ``extra`` — but every Flask response handler also surfaces ``level``,
        so we include it explicitly for parity with the existing payloads.
        ``ErrorLevel`` inherits from ``str``, so the JSON encoder serialises it
        to its plain enum value (``"error"``), not the ``"ErrorLevel.ERROR"``
        repr we'd get from ``str(...)``.
        """
        payload = self.error.to_dict()
        payload.setdefault("level", self.error.level)
        return payload


class SupersetGenericErrorException(SupersetErrorException):
    """Exceptions that are too generic to have their own type.

    Direct port of ``superset_old/exceptions.py::SupersetGenericErrorException``.
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        from superset.errors import ErrorLevel, SupersetError, SupersetErrorType

        super().__init__(
            SupersetError(
                message=message,
                error_type=SupersetErrorType.GENERIC_BACKEND_ERROR,
                level=ErrorLevel.ERROR,
            ),
            status=status,
        )


class SupersetErrorFromParamsException(SupersetErrorException):
    """Exceptions that pass in parameters to construct a SupersetError.

    Direct port of ``superset_old/exceptions.py::SupersetErrorFromParamsException``.
    """

    def __init__(
        self,
        error_type: Any,
        message: str,
        level: Any = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        from superset.errors import ErrorLevel, SupersetError

        super().__init__(
            SupersetError(
                error_type=error_type,
                message=message,
                level=level if level is not None else ErrorLevel.ERROR,
                extra=extra or {},
            )
        )


class SupersetErrorsException(SupersetException):
    """Exceptions with multiple :class:`SupersetError` payloads.

    Ported 1:1 from ``superset_old/exceptions.py::SupersetErrorsException``.
    The constructor accepts a list of ``SupersetError`` (preferred) but also
    tolerates pre-converted dicts so existing call-sites keep working.
    """

    def __init__(
        self,
        errors: list[Any] | None = None,
        status: int | None = None,
        message: str = "",
        # Backwards-compat alias for callers that still pass the old kwarg.
        status_code: int | None = None,
    ) -> None:
        self.errors = errors or []
        chosen = status if status is not None else status_code
        if chosen is not None:
            self.status_code = chosen
        super().__init__(message=message or str(self.errors))

    def _errors_as_dicts(self) -> list[dict[str, Any]]:
        from superset.errors import SupersetError as _SupersetError

        out: list[dict[str, Any]] = []
        for err in self.errors:
            if isinstance(err, _SupersetError):
                payload = err.to_dict()
                payload.setdefault("level", err.level)  # StrEnum serialises clean
                out.append(payload)
            else:
                out.append(err)
        return out

    def to_sip40(self) -> dict[str, Any]:
        dicts = self._errors_as_dicts()
        if dicts:
            return (
                dicts[0]
                if len(dicts) == 1
                else {
                    "message": self.message,
                    "error_type": self.error_type,
                    "level": "error",
                    "extra": {"errors": dicts},
                }
            )
        return super().to_sip40()


# ======================================================================
# Specific exception classes  (ported 1:1 from original)
# ======================================================================


class SupersetSyntaxErrorException(SupersetErrorsException):
    """Raised when Jinja2 template processing finds a syntax error.

    Ported 1:1 from ``superset_old/exceptions.py::SupersetSyntaxErrorException``.
    """

    status_code = 422

    def __init__(self, errors: list[Any]) -> None:  # list[SupersetError]
        super().__init__(errors=errors)


class SupersetTimeoutException(SupersetErrorFromParamsException):
    status_code = 408


class SupersetGenericDBErrorException(SupersetErrorFromParamsException):
    status_code = 400

    def __init__(
        self,
        message: str,
        level: Any = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        from superset.errors import ErrorLevel, SupersetErrorType

        super().__init__(
            error_type=SupersetErrorType.GENERIC_DB_ENGINE_ERROR,
            message=message,
            level=level if level is not None else ErrorLevel.ERROR,
            extra=extra,
        )


class SupersetTemplateParamsErrorException(SupersetErrorFromParamsException):
    status_code = 400

    def __init__(
        self,
        message: str,
        error: Any,
        level: Any = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        from superset.errors import ErrorLevel

        super().__init__(
            error_type=error,
            message=message,
            level=level if level is not None else ErrorLevel.ERROR,
            extra=extra,
        )


class SupersetSecurityException(SupersetErrorException):
    """Security failure with an attached SIP-40 error.

    Direct port of ``superset_old/exceptions.py::SupersetSecurityException``.
    The optional ``payload`` field carries datasource/role context that
    the original ``raise_for_access`` flow attaches.
    """

    status_code = 403
    message = "Access denied"

    def __init__(
        self, error: SupersetError, payload: dict[str, Any] | None = None
    ) -> None:
        super().__init__(error)
        self.payload = payload


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
        from superset.errors import ErrorLevel, SupersetError, SupersetErrorType

        super().__init__(
            SupersetError(
                message=message,
                error_type=SupersetErrorType.INVALID_PAYLOAD_FORMAT_ERROR,
                level=ErrorLevel.ERROR,
            )
        )


class InvalidPayloadSchemaError(SupersetErrorException):
    status_code = 422

    def __init__(self, messages: dict[str, Any] | None = None) -> None:
        from superset.errors import ErrorLevel, SupersetError, SupersetErrorType

        super().__init__(
            SupersetError(
                message="An error happened when validating the request",
                error_type=SupersetErrorType.INVALID_PAYLOAD_SCHEMA_ERROR,
                level=ErrorLevel.ERROR,
                extra={"messages": messages or {}},
            )
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
        from superset.errors import ErrorLevel, SupersetError, SupersetErrorType

        super().__init__(
            SupersetError(
                message="The schema of the submitted payload is invalid.",
                error_type=SupersetErrorType.MARSHMALLOW_ERROR,
                level=ErrorLevel.ERROR,
                extra={"messages": messages or {}, "payload": payload or {}},
            )
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

    Direct port of ``superset_old/exceptions.py::OAuth2RedirectError``.
    """

    def __init__(self, url: str, tab_id: str, redirect_uri: str) -> None:
        from superset.errors import ErrorLevel, SupersetError, SupersetErrorType

        super().__init__(
            SupersetError(
                message="You don't have permission to access the data.",
                error_type=SupersetErrorType.OAUTH2_REDIRECT,
                level=ErrorLevel.WARNING,
                extra={"url": url, "tab_id": tab_id, "redirect_uri": redirect_uri},
            )
        )


class OAuth2Error(SupersetErrorException):
    """
    Exception for when OAuth2 goes wrong.

    Direct port of ``superset_old/exceptions.py::OAuth2Error``.
    """

    def __init__(self, error: str) -> None:
        from superset.errors import ErrorLevel, SupersetError, SupersetErrorType

        super().__init__(
            SupersetError(
                message="Something went wrong while doing OAuth2",
                error_type=SupersetErrorType.OAUTH2_REDIRECT_ERROR,
                level=ErrorLevel.ERROR,
                extra={"error": error},
            )
        )


class SupersetDisallowedSQLFunctionException(SupersetErrorException):
    """
    Disallowed function found on SQL statement.

    Direct port of
    ``superset_old/exceptions.py::SupersetDisallowedSQLFunctionException``.
    """

    def __init__(self, functions: set[str]) -> None:
        from superset.errors import ErrorLevel, SupersetError, SupersetErrorType

        super().__init__(
            SupersetError(
                message=f"SQL statement contains disallowed function(s): {functions}",
                error_type=SupersetErrorType.SYNTAX_ERROR,
                level=ErrorLevel.ERROR,
            )
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
        from superset.errors import ErrorLevel, SupersetError, SupersetErrorType

        super().__init__(
            SupersetError(
                message=message,
                error_type=SupersetErrorType.DATABASE_NOT_FOUND_ERROR,
                level=ErrorLevel.ERROR,
            )
        )


class TableNotFoundException(SupersetErrorException):
    status_code = 404

    def __init__(self, message: str = "Table not found") -> None:
        from superset.errors import ErrorLevel, SupersetError, SupersetErrorType

        super().__init__(
            SupersetError(
                message=message,
                error_type=SupersetErrorType.TABLE_NOT_FOUND_ERROR,
                level=ErrorLevel.ERROR,
            )
        )


class SupersetDMLNotAllowedException(SupersetErrorException):
    def __init__(self) -> None:
        from superset.errors import ErrorLevel, SupersetError, SupersetErrorType

        super().__init__(
            SupersetError(
                message=(
                    "This database does not allow for DDL/DML, but the query mutates "
                    "data. Please contact your administrator for more assistance."
                ),
                error_type=SupersetErrorType.DML_NOT_ALLOWED_ERROR,
                level=ErrorLevel.ERROR,
            )
        )


class SupersetInvalidCTASException(SupersetErrorException):
    def __init__(self) -> None:
        from superset.errors import ErrorLevel, SupersetError, SupersetErrorType

        super().__init__(
            SupersetError(
                message=(
                    "CTAS (create table as select) can only be run with a query where "
                    "the last statement is a SELECT. Please make sure your query has "
                    "a SELECT as its last statement. Then, try running your query again."
                ),
                error_type=SupersetErrorType.INVALID_CTAS_QUERY_ERROR,
                level=ErrorLevel.ERROR,
            )
        )


class SupersetInvalidCVASException(SupersetErrorException):
    def __init__(self) -> None:
        from superset.errors import ErrorLevel, SupersetError, SupersetErrorType

        super().__init__(
            SupersetError(
                message=(
                    "CVAS (create view as select) can only be run with a query with "
                    "a single SELECT statement. Please make sure your query has only "
                    "a SELECT statement. Then, try running your query again."
                ),
                error_type=SupersetErrorType.INVALID_CVAS_QUERY_ERROR,
                level=ErrorLevel.ERROR,
            )
        )


class SupersetResultsBackendNotConfigureException(SupersetErrorException):
    def __init__(self) -> None:
        from superset.errors import ErrorLevel, SupersetError, SupersetErrorType

        super().__init__(
            SupersetError(
                message="Results backend is not configured.",
                error_type=SupersetErrorType.RESULTS_BACKEND_NOT_CONFIGURED_ERROR,
                level=ErrorLevel.ERROR,
            )
        )


class ScreenshotImageNotAvailableException(SupersetException):
    status_code = 404


# ======================================================================
# Command-layer exceptions (replaces commands/base.py::CommandException)
# ======================================================================


class CommandException(SupersetException):
    """Base for command-layer errors.

    ``message`` accepts a plain string or a Marshmallow-style nested error
    dict (``{file_or_field: {field: [msg, ...]}}``); the dict shape is
    preserved verbatim because front-end parsers rely on it.
    """

    status_code = 500

    def __init__(
        self,
        message: str | dict[str, Any] = "",
        exceptions: list[Exception] | None = None,
    ) -> None:
        self.exceptions = exceptions or []
        super().__init__(message=message if isinstance(message, str) else str(message))
        # Preserve the structured payload for callers that parse it back.
        if not isinstance(message, str):
            self.extra["errors"] = message

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


class DashboardsForbiddenError(ForbiddenError):
    """Raised when user is not allowed to modify one of the target dashboards.

    Ported 1:1 from ``DashboardsForbiddenError`` in
    ``superset_old/commands/chart/exceptions.py``.
    """

    message = "Changing one or more of these dashboards is forbidden"


class DashboardsNotFoundValidationError(CommandInvalidError):
    """Raised when one or more requested dashboard ids don't exist.

    Ported 1:1 from
    ``superset_old/commands/chart/exceptions.py::DashboardsNotFoundValidationError``.
    """

    status_code = 422
    message = "Dashboards do not exist"


class OwnersNotFoundValidationError(CommandInvalidError):
    """Raised when one or more requested owner ids can't be resolved."""

    status_code = 422
    message = "Owners are invalid"


class RolesNotFoundValidationError(CommandInvalidError):
    """Raised when one or more requested role ids can't be resolved.

    Ported 1:1 from
    ``superset_old/commands/exceptions.py::RolesNotFoundValidationError``.
    """

    status_code = 422
    message = "Some roles do not exist"


class DatasourceNotFoundValidationError(CommandInvalidError):
    """Raised when one or more requested datasource (table) ids can't be resolved.

    Ported 1:1 from
    ``superset_old/commands/exceptions.py::DatasourceNotFoundValidationError``.
    """

    status_code = 422
    message = "Datasource does not exist"


class DatasourceTypeUpdateRequiredValidationError(CommandInvalidError):
    """Raised when ``datasource_id`` is updated without a ``datasource_type``.

    Ported 1:1 from
    ``superset_old/commands/exceptions.py::DatasourceTypeUpdateRequiredValidationError``.
    """

    status_code = 422
    message = "Datasource type is required when datasource_id is updated"


class TagNotFoundValidationError(CommandInvalidError):
    """Raised when a requested tag id can't be resolved during an update.

    Ported 1:1 from
    ``superset_old/commands/tag/exceptions.py``-adjacent
    ``superset_old/commands/exceptions.py::TagNotFoundValidationError``
    (a ``ValidationError`` on the ``tags`` field, status 422). The message
    is supplied by the caller (e.g. ``f"Tag ID {tag_id} not found"``).
    """

    status_code = 422

    def __init__(self, message: str) -> None:
        super().__init__(message=message)


class TagForbiddenError(ForbiddenError):
    """Raised when the user lacks permission to manage tags on an object.

    Ported 1:1 from
    ``superset_old/commands/exceptions.py::TagForbiddenError`` (a
    ``ForbiddenError`` subclass, status 403). The message is supplied by
    the caller.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message=message)


class RLSRuleNotFoundError(CommandException):
    """Raised when an RLS rule lookup by id returns nothing.

    Ported 1:1 from
    ``superset_old/commands/security/exceptions.py::RLSRuleNotFoundError``.
    """

    status_code = 404
    message = "RLS Rule not found."


class RuleDeleteFailedError(CommandException):
    """Raised when bulk-delete of RLS rules fails inside the transaction.

    Ported 1:1 from
    ``superset_old/commands/security/exceptions.py::RuleDeleteFailedError``.
    """

    status_code = 500
    message = "RLS rules could not be deleted."


class CreateFailedError(CommandException):
    status_code = 500
    message = "Create failed"


class UpdateFailedError(CommandException):
    status_code = 500
    message = "Update failed"


class DeleteFailedError(CommandException):
    status_code = 500
    message = "Delete failed"


class DatasetNotFoundError(CommandException):
    status_code = 404
    message = "Dataset does not exist"


class ImportFailedError(CommandException):
    status_code = 500
    message = "Import failed"


# ======================================================================
# Database command exceptions (ported 1:1 from
# superset_old/commands/database/exceptions.py)
# ======================================================================


class DatabaseCreateFailedError(CreateFailedError):
    message = "Database could not be created."


class DatabaseUpdateFailedError(UpdateFailedError):
    message = "Database could not be updated."


class DatabaseConnectionFailedError(  # pylint: disable=too-many-ancestors
    DatabaseCreateFailedError,
    DatabaseUpdateFailedError,
):
    message = "Connection failed, please check your connection settings"


class DatabaseTestConnectionFailedError(SupersetErrorsException):
    """Raised when a database connection test fails.

    Ported from superset_old/commands/database/exceptions.py:162-164.
    Original has ``status = 422``.
    """

    status_code = 422
    message = "Connection failed, please check your connection settings"


class DatabaseTestConnectionDriverError(CommandInvalidError):
    message = "Could not load database driver"


class DatabaseTestConnectionUnexpectedError(SupersetErrorsException):
    status_code = 422
    message = "Unexpected error occurred, please check your logs for details"


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
    # SupersetErrorsException carries a *list* of SIP-40 errors —
    # return them as-is instead of wrapping in a single-element list.
    if isinstance(exc, SupersetErrorsException) and exc.errors:
        return Response(
            content={
                "errors": exc.errors,
                "message": exc.message,
                "detail": exc.message,
            },
            status_code=exc.status_code,
            media_type=MediaType.JSON,
        )
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


def validation_error_handler(
    request: Request[Any, Any, Any], exc: Exception
) -> Response[Any]:
    """Translate Litestar ValidationException to 400/422.

    Splits two cases that are conflated by Litestar's default 400:

    * ``unknown field`` (msgspec ``forbid_unknown_fields``) → ``422``
      Unprocessable Entity — the payload is syntactically valid JSON but
      contains keys the server refuses to accept (FAB add_columns
      whitelist semantics).
    * everything else (missing required fields, type mismatch) → ``400``
      Bad Request, matching the original FAB / Marshmallow behaviour
      that the contract tests expect.
    """
    detail = getattr(exc, "detail", "") or str(exc)
    extra = getattr(exc, "extra", None)
    body_text = str(detail) + " " + (str(extra) if extra else "")
    status_code = 422 if "unknown field" in body_text.lower() else 400
    error_type = "UNKNOWN_FIELD" if status_code == 422 else "VALIDATION_ERROR"
    return Response(
        content={
            "errors": [
                {
                    "message": detail,
                    "error_type": error_type,
                    "level": "error",
                    "extra": {},
                }
            ],
            "message": detail,
            "detail": detail,
        },
        status_code=status_code,
    )


def integrity_error_handler(
    request: Request[Any, Any, Any], exc: Exception
) -> Response[Any]:
    """Translate SQLAlchemy IntegrityError to a 422 response.

    Mirrors original Flask Superset behaviour where unique-constraint
    or foreign-key violations surface as ``422 Unprocessable Entity``
    rather than ``500 Internal Server Error``.
    """
    detail = str(getattr(exc, "orig", exc)) or "Integrity error"
    # Sanitize ``<class 'X'>: msg`` prefixes that some SQLAlchemy 2.0 +
    # asyncpg combos produce — they leak the DBAPI exception class repr into
    # the user-visible toast (e.g. ``<class 'asyncpg.exceptions.ForeignKey…``).
    import re as _re

    detail = _re.sub(r"^<class '[^']+'>:\s*", "", detail)
    logger.warning("IntegrityError on %s %s: %s", request.method, request.url, detail)
    return Response(
        content={
            "errors": [
                {
                    "message": detail,
                    "error_type": "INTEGRITY_ERROR",
                    "level": "error",
                    "extra": {},
                }
            ],
            "message": detail,
            "detail": detail,
        },
        status_code=422,
    )


def generic_exception_handler(
    request: Request[Any, Any, Any], exc: Exception
) -> Response[Any]:
    """Catch-all for unhandled exceptions.

    Preserves status_code from Litestar HTTP exceptions (404, 405, etc.)
    while wrapping them in SIP-40 format. Logs unhandled non-HTTP
    exceptions for production diagnostics.
    """
    from litestar.exceptions import HTTPException, InternalServerException

    # Litestar wraps signature-model failures from *dependencies* (here,
    # ``rison_params``) as ``InternalServerException`` — but a malformed
    # rison root (``?q=!()`` on a dict-typed handler) is client input, not
    # a server bug. Re-classify those msgspec mismatches as 422 so the user
    # sees a useful validation error instead of 500. See
    # ``litestar._signature.model._create_exception`` for the dep-vs-input
    # split that produces this.
    if isinstance(exc, InternalServerException):
        cause = exc.__cause__
        cause_msg = str(cause) if cause is not None else ""
        if cause is not None and "$.rison_params" in cause_msg:
            return Response(
                content={
                    "errors": [
                        {
                            "message": (
                                "Invalid Rison query parameter: "
                                f"{cause_msg.split(' - at')[0].strip()}"
                            ),
                            "error_type": "VALIDATION_ERROR",
                            "level": "error",
                            "extra": {},
                        }
                    ],
                    "message": "Invalid Rison query parameter",
                    "detail": cause_msg.split(" - at")[0].strip(),
                },
                status_code=422,
            )

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
