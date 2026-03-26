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
"""Thumbnail generation Celery tasks for Liteset.

Replaces ``superset/tasks/thumbnails.py``. Tasks are thin wrappers that
delegate to the superset thumbnail tasks during the Strangler Fig
migration. The Celery task stays in superset; liteset only triggers it.
"""
from __future__ import annotations

import logging
from typing import Optional

from liteset.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)

# Type alias matching superset's WindowSize
WindowSize = tuple[int, int]


@celery_app.task(name="liteset.tasks.thumbnails.cache_chart_thumbnail", soft_time_limit=300)
def cache_chart_thumbnail(
    current_user: str | None,
    chart_id: str,
    force: bool,
    window_size: Optional[WindowSize] = None,
    thumb_size: Optional[WindowSize] = None,
) -> None:
    """Generate and cache a chart thumbnail.

    Delegates to the superset implementation during migration.
    """
    from superset.tasks.thumbnails import (
        cache_chart_thumbnail as _superset_cache_chart_thumbnail,
    )

    _superset_cache_chart_thumbnail(
        current_user, chart_id, force, window_size=window_size, thumb_size=thumb_size
    )


@celery_app.task(
    name="liteset.tasks.thumbnails.cache_dashboard_thumbnail", soft_time_limit=300
)
def cache_dashboard_thumbnail(
    current_user: str | None,
    dashboard_id: int,
    force: bool,
    thumb_size: Optional[WindowSize] = None,
    window_size: Optional[WindowSize] = None,
    cache_key: str | None = None,
) -> None:
    """Generate and cache a dashboard thumbnail.

    Delegates to the superset implementation during migration.
    """
    from superset.tasks.thumbnails import (
        cache_dashboard_thumbnail as _superset_cache_dashboard_thumbnail,
    )

    _superset_cache_dashboard_thumbnail(
        current_user,
        dashboard_id,
        force,
        thumb_size=thumb_size,
        window_size=window_size,
        cache_key=cache_key,
    )


@celery_app.task(
    name="liteset.tasks.thumbnails.cache_dashboard_screenshot", soft_time_limit=300
)
def cache_dashboard_screenshot(
    username: str,
    dashboard_id: int,
    dashboard_url: str,
    force: bool,
    cache_key: str | None = None,
    guest_token: dict[str, str] | None = None,
    thumb_size: Optional[WindowSize] = None,
    window_size: Optional[WindowSize] = None,
) -> None:
    """Generate and cache a dashboard screenshot.

    Delegates to the superset implementation during migration.
    """
    from superset.tasks.thumbnails import (
        cache_dashboard_screenshot as _superset_cache_dashboard_screenshot,
    )

    _superset_cache_dashboard_screenshot(
        username,
        dashboard_id,
        dashboard_url,
        force,
        cache_key=cache_key,
        guest_token=guest_token,
        thumb_size=thumb_size,
        window_size=window_size,
    )
