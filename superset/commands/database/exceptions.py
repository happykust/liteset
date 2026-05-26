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
"""Database-specific exceptions.

The async port re-uses the centralized exceptions from
:mod:`superset.exceptions`.  Per-resource exception aliases are exposed here
to mirror the layout of ``superset_old/commands/database/exceptions.py``.
"""

from __future__ import annotations

from superset.exceptions import (
    CommandException,
    CommandInvalidError,
    DatabaseConnectionFailedError,
    DatabaseTestConnectionDriverError,
    DatabaseTestConnectionUnexpectedError,
    DatabaseUpdateFailedError,
    OAuth2Error,
    ObjectNotFoundError,
    SupersetErrorsException,
)
from superset.i18n import gettext as _


class DatabaseNotFoundError(ObjectNotFoundError):
    """Alias matching the legacy class name; used by the upload commands."""

    def __init__(self, database_id: int | str | None = None) -> None:
        super().__init__("Database", database_id)


class UserNotFoundInSessionError(CommandException):
    """1:1 with ``superset_old/commands/database/exceptions.py``."""

    status_code = 500
    message = _("Could not validate the user in the current session.")


class MissingOAuth2TokenError(DatabaseUpdateFailedError):
    """Connection is missing an OAuth2 token and no OAuth2 dance is possible.

    1:1 with ``superset_old/commands/database/exceptions.py``.
    """

    message = _("Missing OAuth2 token")


class DatabaseSchemaUploadNotAllowed(CommandException):
    status_code = 403
    message = _("Database schema is not allowed for csv uploads.")


class DatabaseUploadNotSupported(CommandException):
    status_code = 422
    message = _("Database type does not support file uploads.")


class DatabaseUploadFailed(CommandException):
    status_code = 422
    message = _("Database upload file failed")


class DatabaseUploadSaveMetadataFailed(CommandException):
    status_code = 500
    message = _("Database upload file failed, while saving metadata")


class DatabaseTablesUnexpectedError(CommandException):
    status_code = 422
    message = _("Unexpected error occurred, please check your logs for details")


class DatabaseDeleteDatasetsExistFailedError(CommandInvalidError):
    """A database can't be deleted because datasets are attached to it.

    1:1 with
    ``superset_old.commands.database.exceptions.DatabaseDeleteDatasetsExistFailedError``.
    """

    status_code = 422
    message = _("Cannot delete a database that has datasets attached")


class DatabaseDeleteFailedReportsExistError(CommandInvalidError):
    """A database can't be deleted because alerts/reports reference it.

    1:1 with
    ``superset_old.commands.database.exceptions.DatabaseDeleteFailedReportsExistError``.
    The human-readable message (with the offending report names) is supplied
    by the delete command.
    """

    status_code = 422


__all__ = (
    "CommandException",
    "CommandInvalidError",
    "DatabaseConnectionFailedError",
    "DatabaseDeleteDatasetsExistFailedError",
    "DatabaseDeleteFailedReportsExistError",
    "DatabaseNotFoundError",
    "DatabaseSchemaUploadNotAllowed",
    "DatabaseTablesUnexpectedError",
    "DatabaseTestConnectionDriverError",
    "DatabaseTestConnectionUnexpectedError",
    "DatabaseUploadFailed",
    "DatabaseUploadNotSupported",
    "DatabaseUploadSaveMetadataFailed",
    "MissingOAuth2TokenError",
    "OAuth2Error",
    "ObjectNotFoundError",
    "SupersetErrorsException",
    "UserNotFoundInSessionError",
)
