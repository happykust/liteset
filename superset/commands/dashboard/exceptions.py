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
"""Dashboard-specific exceptions.

The async port re-uses the centralized exceptions from
:mod:`superset.exceptions`.  Per-resource exception aliases are exposed here
to mirror the layout of ``superset_old/commands/dashboard/exceptions.py``.
"""

from __future__ import annotations

from superset.exceptions import (
    CommandInvalidError,
    ForbiddenError,
    ImportFailedError,
    ObjectNotFoundError,
)


class DashboardNotFoundError(ObjectNotFoundError):
    """Raised when a dashboard cannot be located.

    1:1 with ``superset_old.commands.dashboard.exceptions.DashboardNotFoundError``.
    """

    def __init__(self, dashboard_id: str | int | None = None) -> None:
        super().__init__("Dashboard", dashboard_id)


class DashboardAccessDeniedError(ForbiddenError):
    """Raised when the current user is not allowed to access a dashboard.

    1:1 with ``superset_old.commands.dashboard.exceptions.DashboardAccessDeniedError``.
    """

    message = "You don't have access to this dashboard."


__all__ = (
    "CommandInvalidError",
    "DashboardAccessDeniedError",
    "DashboardNotFoundError",
    "ForbiddenError",
    "ImportFailedError",
    "ObjectNotFoundError",
)
