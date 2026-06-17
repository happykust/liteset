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
"""Exception classes for import command-layer errors.

Translation strings flow through :mod:`superset.i18n`.
"""

from __future__ import annotations

from superset.exceptions import CommandException
from superset.i18n import gettext as _


class IncorrectVersionError(CommandException):
    """Raised when the bundle's metadata version does not match the importer."""

    # Both ``status`` and ``status_code`` kept in sync — Marshmallow consumers key
    # off ``status``; liteset HTTP handlers key off ``status_code``.
    status = 422
    status_code = 422
    message = _("Import has incorrect version")


class NoValidFilesFoundError(CommandException):
    """Raised when none of the bundle entries match a known prefix/schema."""

    status = 400
    status_code = 400
    message = _("No valid import files were found")


class IncorrectFormatError(CommandException):
    """Raised when the bundle's file format is unrecognisable (e.g. not a ZIP)."""

    status = 422
    status_code = 422
    message = _("File has the incorrect format")
