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
"""Chart-specific exceptions.

Mirrors the layout of ``superset_old/commands/chart/exceptions.py`` —
re-uses the centralized exceptions from :mod:`superset.exceptions`
(``CommandException`` etc.) and adds the chart-data error subclasses
the original used to wrap query-context failures.
"""

from __future__ import annotations

from superset.exceptions import (
    CommandException,
    CommandInvalidError,
    DashboardsForbiddenError,
    DashboardsNotFoundValidationError,
    ImportFailedError,
    ObjectNotFoundError,
)

# ---------------------------------------------------------------------------
# Chart-data errors — 1:1 with
# ``superset_old.commands.chart.exceptions.ChartDataQueryFailedError`` and
# ``ChartDataCacheLoadError``.  Both inherit from ``CommandException`` so
# Litestar's exception handler maps them through the standard SIP-40 envelope.
# ---------------------------------------------------------------------------


class ChartDataQueryFailedError(CommandException):
    """A query inside the chart-data payload failed.

    1:1 port of ``superset_old.commands.chart.exceptions.ChartDataQueryFailedError``.
    Maps to HTTP 400 like the original ``_get_data_response`` (``response_400``).
    """

    status_code = 400


class ChartDataCacheLoadError(CommandException):
    """Failed to (re)load chart data from cache.

    1:1 port of ``superset_old.commands.chart.exceptions.ChartDataCacheLoadError``.
    Maps to HTTP 422 like the original ``_get_data_response`` (``response_422``).
    """

    status_code = 422


class ChartDeleteFailedReportsExistError(CommandInvalidError):
    """A chart can't be deleted because alerts/reports reference it.

    1:1 port of
    ``superset_old.commands.chart.exceptions.ChartDeleteFailedReportsExistError``.
    The human-readable message (with the offending report names) is supplied
    by the delete command. Maps to HTTP 422 like the original.
    """

    status_code = 422


__all__ = (
    "ChartDataCacheLoadError",
    "ChartDataQueryFailedError",
    "ChartDeleteFailedReportsExistError",
    "CommandException",
    "CommandInvalidError",
    "DashboardsForbiddenError",
    "DashboardsNotFoundValidationError",
    "ImportFailedError",
    "ObjectNotFoundError",
)
