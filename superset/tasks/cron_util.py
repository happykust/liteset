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
"""Cron expression utilities for Superset.

Replaces ``superset/tasks/cron_util.py``. The window-size config value
is read from the Superset ``SupersetSettings`` instead of the legacy
``current_app.config``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import datetime, timedelta

from croniter import croniter
from pytz import (
    timezone as pytz_timezone,
    UnknownTimeZoneError,
)

logger = logging.getLogger(__name__)

# Default cron window size in seconds (matches Superset's default
# ``ALERT_REPORTS_CRON_WINDOW_SIZE`` of 59 — one less than the 60s beat
# interval so a schedule never fires in two adjacent windows).
_DEFAULT_CRON_WINDOW_SIZE: int = 59


def cron_schedule_window(
    triggered_at: datetime,
    cron: str,
    timezone: str,
    window_size: int | None = None,
) -> Iterator[datetime]:
    """Yield UTC-normalized schedule times within a cron window.

    :param triggered_at: The time the scheduler was triggered.
    :param cron: A cron expression string.
    :param timezone: IANA timezone name for the cron schedule.
    :param window_size: Override for the window size in seconds.
        Falls back to ``_DEFAULT_CRON_WINDOW_SIZE`` if not supplied.
    """
    if window_size is None:
        window_size = _DEFAULT_CRON_WINDOW_SIZE

    try:
        tz = pytz_timezone(timezone)
    except UnknownTimeZoneError:
        tz = pytz_timezone("UTC")
        logger.warning("Timezone %s was invalid. Falling back to 'UTC'", timezone)

    utc = pytz_timezone("UTC")
    time_now = triggered_at.astimezone(tz)
    start_at = time_now - timedelta(seconds=window_size / 2)
    stop_at = time_now + timedelta(seconds=window_size / 2)
    crons = croniter(cron, start_at)
    for schedule in crons.all_next(datetime):
        if schedule >= stop_at:
            break
        yield schedule.astimezone(utc).replace(tzinfo=None)
