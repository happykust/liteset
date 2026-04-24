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
"""Async port of ``superset_old/commands/database/ssh_tunnel/exceptions.py``.

Same class hierarchy as the original; the only difference is that the
``SSHTunnelRequiredFieldValidationError`` no longer inherits from
``marshmallow.ValidationError`` (Liteset has dropped Marshmallow) — it is
emitted as a ``CommandInvalidError`` carrying ``field_name`` and message
attributes that the API layer formats into the same JSON error payload.
"""

from superset.exceptions import (
    CommandException,
    CommandInvalidError,
    CreateFailedError,
    DeleteFailedError,
    UpdateFailedError,
)
from superset.i18n import gettext as _


class SSHTunnelError(Exception):
    """Base class — mirrors the original."""


class SSHTunnelDeleteFailedError(DeleteFailedError, SSHTunnelError):
    message = _("SSH Tunnel could not be deleted.")


class SSHTunnelNotFoundError(CommandException, SSHTunnelError):
    status_code = 404
    message = _("SSH Tunnel not found.")


class SSHTunnelInvalidError(CommandInvalidError, SSHTunnelError):
    message = _("SSH Tunnel parameters are invalid.")

    def __init__(
        self,
        message: str | None = None,
        exceptions: list[Exception] | None = None,
    ) -> None:
        super().__init__(message or self.message)
        self._exceptions: list[Exception] = list(exceptions or [])

    def extend(self, exceptions: list[Exception]) -> None:
        self._exceptions.extend(exceptions)

    def get_list_classnames(self) -> list[str]:
        return sorted({type(ex).__name__ for ex in self._exceptions})


class SSHTunnelDatabasePortError(CommandInvalidError, SSHTunnelError):
    message = _("A database port is required when connecting via SSH Tunnel.")


class SSHTunnelUpdateFailedError(UpdateFailedError, SSHTunnelError):
    message = _("SSH Tunnel could not be updated.")


class SSHTunnelCreateFailedError(CreateFailedError, SSHTunnelError):
    message = _("Creating SSH Tunnel failed for an unknown reason")


class SSHTunnelingNotEnabledError(CommandException, SSHTunnelError):
    status_code = 400
    message = _("SSH Tunneling is not enabled")


class SSHTunnelRequiredFieldValidationError(CommandInvalidError, SSHTunnelError):
    """Raised when a required SSH-tunnel field is missing.

    The original wraps Marshmallow ``ValidationError`` so the message
    attaches to the field name in the response payload.  Liteset surfaces
    the same data via ``field_name`` + ``message`` attributes.
    """

    def __init__(self, field_name: str) -> None:
        super().__init__(_("Field is required"))
        self.field_name = field_name


class SSHTunnelMissingCredentials(CommandInvalidError, SSHTunnelError):  # noqa: N818
    message = _("Must provide credentials for the SSH Tunnel")


class SSHTunnelInvalidCredentials(CommandInvalidError, SSHTunnelError):  # noqa: N818
    message = _("Cannot have multiple credentials for the SSH Tunnel")
