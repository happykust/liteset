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
"""Async port of ``superset_old/commands/annotation_layer/annotation/exceptions.py``.

The original module mixes Marshmallow ``ValidationError`` subclasses with
``CommandException`` subclasses.  In Liteset (msgspec / Litestar) we no
longer use Marshmallow; the validation errors are reimplemented as plain
``CommandInvalidError`` subclasses with the same ``message`` text so
callers raising/catching them behave identically.
"""

from superset.exceptions import (
    CommandException,
    CommandInvalidError,
    CreateFailedError,
    DeleteFailedError,
)
from superset.i18n import gettext as _


class AnnotationDatesValidationError(CommandInvalidError):
    """Raised when an annotation's start date is after its end date."""

    message = _("End date must be after start date")
    field_name = "start_dttm"


class AnnotationUniquenessValidationError(CommandInvalidError):
    """Raised when an annotation's short description is not unique within its layer."""

    message = _("Short description must be unique for this layer")
    field_name = "short_descr"


class AnnotationNotFoundError(CommandException):
    message = _("Annotation not found.")


class AnnotationInvalidError(CommandInvalidError):
    message = _("Annotation parameters are invalid.")


class AnnotationCreateFailedError(CreateFailedError):
    message = _("Annotation could not be created.")


class AnnotationUpdateFailedError(CreateFailedError):
    message = _("Annotation could not be updated.")


class AnnotationDeleteFailedError(DeleteFailedError):
    message = _("Annotations could not be deleted.")
