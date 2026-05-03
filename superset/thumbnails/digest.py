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
"""Thumbnail digest computation.

1:1 port of ``superset_old/thumbnails/digest.py``.

The two adaptations versus the Flask original:

* ``app.config["..."]`` → :class:`SupersetSettings` (cached via
  :func:`superset.utils.webdriver.cached_settings`).
* ``security_manager.find_user`` / ``override_user`` Flask thread-locals
  → synchronous metadata-DB lookup + :class:`ContextVar` binding via
  :func:`superset.utils.core.set_current_user`.  The RLS predicate
  evaluation goes through :func:`superset.utils.rls.compose_rls_text_clauses`
  which already mirrors ``BaseDatasource.get_sqla_row_level_filters``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING

from superset.tasks.exceptions import ExecutorNotFoundError
from superset.tasks.types import ExecutorType
from superset.tasks.utils import get_executor
from superset.utils.hashing import md5_sha_from_str

if TYPE_CHECKING:
    from superset.models.connectors import SqlaTable
    from superset.models.dashboard import Dashboard
    from superset.models.slice import Slice

logger = logging.getLogger(__name__)


def _adjust_string_for_executor(
    unique_string: str,
    executor_type: ExecutorType,
    executor: str,
) -> str:
    """Add the executor to the unique string for user-specific thumbnails.

    1:1 port of ``superset_old.thumbnails.digest._adjust_string_for_executor``.
    """
    if executor_type == ExecutorType.CURRENT_USER:
        # add the user id to the string to make it unique
        unique_string = f"{unique_string}\n{executor}"

    return unique_string


def _adjust_string_with_rls(
    unique_string: str,
    datasources: "Iterable[SqlaTable | None]",
    executor: str,
) -> str:
    """Add RLS filters to the unique string based on the executor.

    1:1 port of ``superset_old.thumbnails.digest._adjust_string_with_rls``.

    The original calls ``security_manager.find_user(executor)`` plus
    ``security_manager.get_current_guest_user_if_guest()`` and then walks
    each datasource's ``get_sqla_row_level_filters`` under
    ``override_user(user)``.  The async port mirrors this with a sync
    metadata-DB user lookup (the only one the digest needs) and the
    :func:`set_current_user` :class:`ContextVar` binding consumed by the
    sync RLS path :func:`superset.utils.rls.compose_rls_text_clauses` —
    which is what ``get_sqla_row_level_filters`` calls under the hood.
    """
    from sqlalchemy import select

    from superset.models.security import User
    from superset.utils.core import (
        _current_user_ctx,
        get_current_user,
    )
    from superset.utils.rls import _metadata_sync_session

    # Try the foreground user first (matches ``find_user``); fall back to
    # the active guest user if the executor name doesn't resolve.  This
    # mirrors the original's ``find_user(executor) or
    # get_current_guest_user_if_guest()`` chain.
    user: object | None = None
    with _metadata_sync_session() as session:
        stmt = select(User).where(User.username == executor)
        user = session.execute(stmt).scalars().one_or_none()

    if user is None:
        candidate = get_current_user()
        if candidate is not None and getattr(candidate, "is_guest", False):
            user = candidate

    if user:
        stringified_rls = ""
        token = _current_user_ctx.set(user)
        try:
            for datasource in datasources:
                if (
                    datasource
                    and hasattr(datasource, "is_rls_supported")
                    and datasource.is_rls_supported
                ):
                    rls_filters = datasource.get_sqla_row_level_filters()

                    if len(rls_filters) > 0:
                        stringified_rls += (
                            f"{str(datasource.id)}\t"
                            + "\t".join([str(f) for f in rls_filters])
                            + "\n"
                        )
        finally:
            try:
                _current_user_ctx.reset(token)
            except (LookupError, ValueError):
                _current_user_ctx.set(None)

        if stringified_rls:
            unique_string = f"{unique_string}\n{stringified_rls}"

    return unique_string


def _cached_settings() -> object:
    """Return a process-wide cached :class:`SupersetSettings` instance.

    Routed through :func:`superset.utils.webdriver.cached_settings` so the
    digest helpers and the screenshot pipeline share a single instance.
    """
    from superset.utils.webdriver import cached_settings

    return cached_settings()


def get_dashboard_digest(dashboard: "Dashboard") -> str | None:
    """Return the cache-key digest for ``dashboard``.

    1:1 port of ``superset_old.thumbnails.digest.get_dashboard_digest``.
    Reads ``THUMBNAIL_EXECUTORS`` and the optional override
    ``THUMBNAIL_DASHBOARD_DIGEST_FUNC`` from :class:`SupersetSettings`.
    """
    from superset.utils.core import get_username

    settings = _cached_settings()
    try:
        executor_type, executor = get_executor(
            executors=settings.thumbnail_executors,  # type: ignore[attr-defined]
            model=dashboard,
            current_user=get_username(),
        )
    except ExecutorNotFoundError:
        return None

    func = getattr(settings, "thumbnail_dashboard_digest_func", None)
    if func:
        return func(dashboard, executor_type, executor)

    unique_string = (
        f"{dashboard.id}\n{dashboard.charts}\n{dashboard.position_json}\n"
        f"{dashboard.css}\n{dashboard.json_metadata}"
    )

    unique_string = _adjust_string_for_executor(unique_string, executor_type, executor)
    # TODO(liteset): ``Dashboard.datasources`` was a sync ``db.session.query``
    # property in the original Flask code (pulled all attached datasources for
    # RLS hashing). Reimplementing it in the async stack requires either
    # eager-loading the datasources upstream or making this whole digest
    # function async. Until that refactor lands, fall back to an empty list
    # so the digest is computed without RLS contribution.
    unique_string = _adjust_string_with_rls(
        unique_string, getattr(dashboard, "datasources", []), executor
    )

    return md5_sha_from_str(unique_string)


def get_chart_digest(chart: "Slice") -> str | None:
    """Return the cache-key digest for ``chart``.

    1:1 port of ``superset_old.thumbnails.digest.get_chart_digest``.
    Reads ``THUMBNAIL_EXECUTORS`` and the optional override
    ``THUMBNAIL_CHART_DIGEST_FUNC`` from :class:`SupersetSettings`.
    """
    from superset.utils.core import get_username

    settings = _cached_settings()
    try:
        executor_type, executor = get_executor(
            executors=settings.thumbnail_executors,  # type: ignore[attr-defined]
            model=chart,
            current_user=get_username(),
        )
    except ExecutorNotFoundError:
        return None

    func = getattr(settings, "thumbnail_chart_digest_func", None)
    if func:
        return func(chart, executor_type, executor)

    unique_string = f"{chart.params or ''}.{executor}"
    unique_string = _adjust_string_for_executor(unique_string, executor_type, executor)
    unique_string = _adjust_string_with_rls(unique_string, [chart.datasource], executor)

    return md5_sha_from_str(unique_string)


__all__ = (
    "_adjust_string_for_executor",
    "_adjust_string_with_rls",
    "get_chart_digest",
    "get_dashboard_digest",
)
