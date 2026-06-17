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

Workers stay synchronous: each task opens a sync Session to load the model,
then drives the Selenium/Playwright capture path via ChartScreenshot /
DashboardScreenshot.

Key adaptations:
* ``security_manager.find_user`` replaced with a direct sync SELECT that
  eager-loads ``roles`` + ``groups`` — lazy loading against a closed session
  raises ``DetachedInstanceError`` inside the webdriver thread.
* ``override_user`` writes the executor user to a per-task ContextVar and
  restores it on exit — the binding never leaks across tasks on the same thread.
* Guest token decoded via ``GuestUser.from_token_payload`` (same dict shape
  the original ``GuestToken`` produced).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from superset.tasks.celery_app import celery_app
from superset.utils.webdriver import cached_settings

logger = logging.getLogger(__name__)

WindowSize = tuple[int, int]


def _find_user_by_username(session: Any, username: str) -> Any | None:
    """Return the User for ``username`` with roles + groups eager-loaded.

    Eager loading is required: the screenshot pipeline serialises ``user.roles``
    into the JWT payload via MachineAuthProvider.get_auth_cookies, and a lazy
    load against an already-closed session raises DetachedInstanceError deep
    inside the webdriver thread.
    """
    from superset.models.security import User

    stmt = (
        select(User)
        .where(User.username == username)
        .options(
            selectinload(User.roles),
            selectinload(User.groups),
        )
    )
    return session.execute(stmt).scalars().unique().one_or_none()


def _compute_digest(model: Any) -> str | None:
    """Return the model digest for the screenshot cache key.

    Delegates to ``Slice.digest`` / ``Dashboard.digest`` so the cache key
    matches the one in ``thumbnail_url`` — preserving the invariant
    ``thumbnail.url == cache.lookup(model.digest)``.
    """
    return getattr(model, "digest", None)


@celery_app.task(
    name="cache_chart_thumbnail",
    soft_time_limit=300,
)
def cache_chart_thumbnail(  # noqa: C901
    current_user: Optional[str],
    chart_id: str,
    force: bool,
    window_size: Optional[WindowSize] = None,
    thumb_size: Optional[WindowSize] = None,
) -> None:
    """Render a chart screenshot and cache it under the canonical key.

    :param current_user: Username of the user that initiated the task
        (forwarded as the ``CURRENT_USER`` candidate to
        :func:`get_executor`).  ``None`` is legal — the task falls
        through to the next executor candidate (``OWNER`` etc.).
    :param chart_id: Stringified primary key of the
        :class:`~superset.models.slice.Slice` row to capture.
    :param force: When ``True``, recapture even if the cache already
        holds a fresh image.
    :param window_size: Browser viewport size used for the capture.
    :param thumb_size: Final thumbnail size after resize.
    """
    from superset.db.session import get_sync_session
    from superset.models.slice import Slice
    from superset.tasks.utils import get_executor
    from superset.utils.core import override_user
    from superset.utils.screenshots import ChartScreenshot, thumbnail_cache
    from superset.utils.urls import get_url_path

    if not thumbnail_cache:
        logger.warning("No cache set, refusing to compute")
        return

    session = get_sync_session()
    try:
        stmt = (
            select(Slice)
            .where(Slice.id == int(chart_id))
            .options(
                selectinload(Slice.owners),
            )
        )
        chart = session.execute(stmt).scalars().unique().one_or_none()
        if not chart:
            logger.warning("No chart found, skip computing chart thumbnail")
            return

        url = get_url_path("Superset.slice", slice_id=chart.id)
        logger.info("Caching chart: %s", url)

        _, username = get_executor(
            executors=cached_settings().thumbnail_executors,
            model=chart,
            current_user=current_user,
        )
        user = _find_user_by_username(session, username)
        with override_user(user):
            screenshot = ChartScreenshot(
                url, _compute_digest(chart), window_size, thumb_size
            )
            screenshot.compute_and_cache(
                user=user,
                window_size=window_size,
                thumb_size=thumb_size,
                force=force,
            )
    finally:
        session.close()


@celery_app.task(
    name="cache_dashboard_thumbnail",
    soft_time_limit=300,
)
def cache_dashboard_thumbnail(  # noqa: C901
    current_user: Optional[str],
    dashboard_id: int,
    force: bool,
    thumb_size: Optional[WindowSize] = None,
    window_size: Optional[WindowSize] = None,
    cache_key: str | None = None,
) -> None:
    """Render a dashboard screenshot and cache it under the canonical key.

    :param current_user: Username of the user that initiated the task.
    :param dashboard_id: Primary key of the
        :class:`~superset.models.dashboard.Dashboard` row to capture.
    :param force: When ``True``, recapture even if the cache already
        holds a fresh image.
    :param window_size: Browser viewport size used for the capture.
    :param thumb_size: Final thumbnail size after resize.
    :param cache_key: Optional precomputed cache key (used by the
        dashboard controller's screenshot endpoint, which derives
        the key from a permalink rather than the digest alone).
    """
    from superset.db.session import get_sync_session
    from superset.models.dashboard import Dashboard
    from superset.tasks.utils import get_executor
    from superset.utils.core import override_user
    from superset.utils.screenshots import DashboardScreenshot, thumbnail_cache
    from superset.utils.urls import get_url_path

    if not thumbnail_cache:
        logger.warning("No cache set, refusing to compute")
        return

    session = get_sync_session()
    try:
        stmt = (
            select(Dashboard)
            .where(Dashboard.id == int(dashboard_id))
            .options(
                selectinload(Dashboard.owners),
            )
        )
        dashboard = session.execute(stmt).scalars().unique().one_or_none()
        if not dashboard:
            logger.warning("No dashboard found, skip computing dashboard thumbnail")
            return

        url = get_url_path("Superset.dashboard", dashboard_id_or_slug=dashboard.id)
        logger.info("Caching dashboard: %s", url)

        _, username = get_executor(
            executors=cached_settings().thumbnail_executors,
            model=dashboard,
            current_user=current_user,
        )
        user = _find_user_by_username(session, username)
        with override_user(user):
            screenshot = DashboardScreenshot(
                url, _compute_digest(dashboard), window_size, thumb_size
            )
            screenshot.compute_and_cache(
                user=user,
                window_size=window_size,
                thumb_size=thumb_size,
                force=force,
                cache_key=cache_key,
            )
    finally:
        session.close()


@celery_app.task(
    name="cache_dashboard_screenshot",
    soft_time_limit=300,
)
def cache_dashboard_screenshot(  # noqa: C901
    username: str,
    dashboard_id: int,
    dashboard_url: str,
    force: bool,
    cache_key: str | None = None,
    guest_token: dict[str, Any] | None = None,
    thumb_size: Optional[WindowSize] = None,
    window_size: Optional[WindowSize] = None,
) -> None:
    """Render a dashboard screenshot for a *given URL* (download flow).

    Used by the dashboard download-as-PNG / PDF flow: the URL is
    constructed by the controller (with ``anchor``, ``activeTabs``,
    ``dataMask`` query parameters baked in) and passed straight through
    to the webdriver.

    Authentication branches on the optional ``guest_token`` payload —
    when present the screenshot runs as a :class:`GuestUser` (embedded
    flow); otherwise the executor user is resolved via
    :func:`get_executor` exactly like the standard thumbnail task.
    """
    from superset.db.session import get_sync_session
    from superset.models.dashboard import Dashboard
    from superset.security.guest import GuestUser
    from superset.tasks.utils import get_executor
    from superset.utils.core import override_user
    from superset.utils.screenshots import DashboardScreenshot, thumbnail_cache

    if not thumbnail_cache:
        logger.warning("No cache set, refusing to compute")
        return

    session = get_sync_session()
    try:
        stmt = (
            select(Dashboard)
            .where(Dashboard.id == int(dashboard_id))
            .options(
                selectinload(Dashboard.owners),
            )
        )
        dashboard = session.execute(stmt).scalars().unique().one_or_none()
        if not dashboard:
            logger.warning("No dashboard found, skip computing dashboard screenshot")
            return

        logger.info("Caching dashboard: %s", dashboard_url)

        current_user_obj: Any
        if guest_token:
            current_user_obj = GuestUser.from_token_payload(guest_token)
        else:
            _, exec_username = get_executor(
                executors=cached_settings().thumbnail_executors,
                model=dashboard,
                current_user=username,
            )
            current_user_obj = _find_user_by_username(session, exec_username)

        with override_user(current_user_obj):
            screenshot = DashboardScreenshot(
                dashboard_url, _compute_digest(dashboard), window_size, thumb_size
            )
            screenshot.compute_and_cache(
                user=current_user_obj,
                window_size=window_size,
                thumb_size=thumb_size,
                cache_key=cache_key,
                force=force,
            )
    finally:
        session.close()


__all__ = [
    "WindowSize",
    "cache_chart_thumbnail",
    "cache_dashboard_screenshot",
    "cache_dashboard_thumbnail",
]
