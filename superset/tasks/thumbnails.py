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
"""Thumbnail generation Celery tasks for Superset.

Self-contained implementations that use :func:`superset.db.session.get_sync_session`
for synchronous DB access inside Celery workers.  The actual headless-browser
screenshot logic is stubbed and will be expanded once the Selenium/Playwright
integration is ported to superset.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import text

from superset.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)

# Type alias matching superset's WindowSize
WindowSize = tuple[int, int]


@celery_app.task(
    name="superset.tasks.thumbnails.cache_chart_thumbnail",
    soft_time_limit=300,
)
def cache_chart_thumbnail(
    current_user: str | None,
    chart_id: str,
    force: bool,
    window_size: Optional[WindowSize] = None,
    thumb_size: Optional[WindowSize] = None,
) -> None:
    """Generate and cache a chart thumbnail via headless browser.

    Loads the chart from the database, constructs the screenshot URL,
    and dispatches a headless-browser capture.  The screenshot pipeline
    is currently a stub that logs the operation.
    """
    from superset.db.session import get_sync_session

    session = get_sync_session()
    try:
        row = session.execute(
            text("SELECT id FROM slices WHERE id = :cid"), {"cid": chart_id}
        ).fetchone()
        if not row:
            logger.warning("Chart %s not found for thumbnail generation", chart_id)
            return
        logger.info(
            "Generating thumbnail for chart %s (force=%s, window=%s, thumb=%s)",
            chart_id,
            force,
            window_size,
            thumb_size,
        )
        # TODO: implement headless browser screenshot capture and cache storage.
    finally:
        session.close()


@celery_app.task(
    name="superset.tasks.thumbnails.cache_dashboard_thumbnail", soft_time_limit=300
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

    Loads the dashboard from the database, constructs the screenshot URL,
    and dispatches a headless-browser capture.  Currently a stub.
    """
    from superset.db.session import get_sync_session

    session = get_sync_session()
    try:
        row = session.execute(
            text("SELECT id FROM dashboards WHERE id = :did"), {"did": dashboard_id}
        ).fetchone()
        if not row:
            logger.warning(
                "Dashboard %s not found for thumbnail generation", dashboard_id
            )
            return
        logger.info(
            "Generating thumbnail for dashboard %s (force=%s, cache_key=%s)",
            dashboard_id,
            force,
            cache_key,
        )
        # TODO: implement headless browser screenshot capture and cache storage.
    finally:
        session.close()


@celery_app.task(
    name="superset.tasks.thumbnails.cache_dashboard_screenshot", soft_time_limit=300
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

    Loads the dashboard from the database and dispatches a headless-browser
    capture for the provided *dashboard_url*.  Currently a stub.
    """
    from superset.db.session import get_sync_session

    session = get_sync_session()
    try:
        row = session.execute(
            text("SELECT id FROM dashboards WHERE id = :did"), {"did": dashboard_id}
        ).fetchone()
        if not row:
            logger.warning(
                "Dashboard %s not found for screenshot generation", dashboard_id
            )
            return
        logger.info(
            "Generating screenshot for dashboard %s url=%s (force=%s, cache_key=%s)",
            dashboard_id,
            dashboard_url,
            force,
            cache_key,
        )
        # TODO: implement headless browser screenshot capture and cache storage.
    finally:
        session.close()
