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
"""Dashboard-specific exceptions."""

from __future__ import annotations

from superset.exceptions import (
    CommandInvalidError,
    ForbiddenError,
    ImportFailedError,
    ObjectNotFoundError,
)


class DashboardNotFoundError(ObjectNotFoundError):
    """Raised when a dashboard cannot be located."""

    def __init__(self, dashboard_id: str | int | None = None) -> None:
        super().__init__("Dashboard", dashboard_id)


class DashboardAccessDeniedError(ForbiddenError):
    """Raised when the current user is not allowed to access a dashboard."""

    message = "You don't have access to this dashboard."


class DashboardDeleteFailedReportsExistError(CommandInvalidError):
    """Deletion blocked because alerts/reports reference this dashboard (HTTP 422)."""

    status_code = 422


class DashboardSlugExistsValidationError(CommandInvalidError):
    """Slug uniqueness violation — field-keyed 422 error."""

    status_code = 422
    message = "Must be unique"

    def normalized_messages(self) -> dict[str, list[str]]:
        return {"slug": [str(self.message)]}


class DashboardInvalidError(CommandInvalidError):
    """Accumulating dashboard validation error (per-field 422)."""

    status_code = 422
    message = "Dashboard parameters are invalid."


__all__ = (
    "CommandInvalidError",
    "DashboardAccessDeniedError",
    "DashboardDeleteFailedReportsExistError",
    "DashboardInvalidError",
    "DashboardNotFoundError",
    "DashboardSlugExistsValidationError",
    "ForbiddenError",
    "ImportFailedError",
    "ObjectNotFoundError",
)
