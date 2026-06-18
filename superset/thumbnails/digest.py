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

The two adaptations versus the original:

* ``app.config["..."]`` → :class:`SupersetSettings` (cached via
  :func:`superset.utils.webdriver.cached_settings`).
* ``security_manager.find_user`` / ``override_user`` request-scoped state
  → synchronous metadata-DB lookup + :class:`ContextVar` binding via
  :func:`superset.utils.core.set_current_user`.  The RLS predicate
  evaluation goes through :func:`superset.utils.rls.compose_rls_text_clauses`
  which already mirrors ``BaseDatasource.get_sqla_row_level_filters``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any, cast, TYPE_CHECKING

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
    """Add the executor to the unique string for user-specific thumbnails."""
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
    # ``find_user(executor) or get_current_guest_user_if_guest()`` chain.
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


def _query_dashboard_datasources(
    session: "Any", dashboard_id: int
) -> "list[SqlaTable]":
    """Bulk-load the ``SqlaTable`` datasources backing a dashboard's charts.

    Grouped each slice's ``datasource_id`` by its ``cls_model`` and
    bulk-loaded the rows. The async
    port enumerates them through a synchronous metadata ``session`` keyed off
    the dashboard id, so the lookup never depends on the relationship-load
    state of the (possibly async-detached) ``Dashboard`` instance.

    Only ``table``-type datasources are returned — the only ``cls_model`` the
    port maps (``SqlaTable``).  ``SqlaTable.database`` is eager-loaded because
    the sync RLS path (``get_sqla_row_level_filters`` →
    ``compose_rls_text_clauses``) walks it to build the Jinja template
    processor; the datasources stay attached to ``session`` for the rest of
    the RLS evaluation.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from superset.models.connectors import SqlaTable
    from superset.models.dashboard import dashboard_slices
    from superset.models.slice import Slice

    rows = session.execute(
        select(Slice.datasource_id, Slice.datasource_type)
        .join(dashboard_slices, dashboard_slices.c.slice_id == Slice.id)
        .where(dashboard_slices.c.dashboard_id == dashboard_id)
    ).all()
    table_ids = {
        ds_id
        for ds_id, ds_type in rows
        if ds_id is not None and (ds_type or "table") == "table"
    }
    if not table_ids:
        return []
    return list(
        session.execute(
            select(SqlaTable)
            .where(SqlaTable.id.in_(table_ids))
            .options(selectinload(SqlaTable.database))
        )
        .scalars()
        .all()
    )


def _query_dashboard_slice_names(session: "Any", dashboard_id: int) -> list[str]:
    """Slice names of every chart attached to ``dashboard_id``.

    Sync mirror of ``Dashboard.charts`` (``[slc.slice_name or "<empty>" for
    slc in self.slices]``) computed through the metadata ``session`` so the
    digest never depends on the relationship-load state of a (possibly
    async-detached) ``Dashboard``.  Touching the unloaded ``slices``
    relationship on an object bound to an ``AsyncSession`` emits a sync
    lazy-load → ``MissingGreenlet`` → HTTP 500 (e.g. on ``GET /dashboard/``
    list serialisation, which does not eager-load ``slices``).  Ordered by
    ``Slice.id`` for a deterministic digest.
    """
    from sqlalchemy import select

    from superset.models.dashboard import dashboard_slices
    from superset.models.slice import Slice

    rows = (
        session.execute(
            select(Slice.slice_name)
            .join(dashboard_slices, dashboard_slices.c.slice_id == Slice.id)
            .where(dashboard_slices.c.dashboard_id == dashboard_id)
            .order_by(Slice.id)
        )
        .scalars()
        .all()
    )
    return [name or "<empty>" for name in rows]


def _query_chart_datasources(
    session: "Any", datasource_id: int | None, datasource_type: str | None
) -> "list[SqlaTable]":
    """Return ``[SqlaTable]`` for a single chart's datasource via ``session``.

    The single-chart analogue of :func:`_query_dashboard_datasources`:
    re-queries the ``SqlaTable`` (eager-loading ``database`` and keeping it
    attached to the sync ``session``) so the RLS walk in
    :func:`_adjust_string_with_rls` → ``compose_rls_text_clauses`` never trips
    a ``MissingGreenlet`` by touching ``chart.datasource.database`` on an
    async-detached ORM object (the chart-list path eager-loads only
    ``Slice.table``, not ``SqlaTable.database``).  Only ``table``-type
    datasources are mapped, mirroring ``Slice.datasource``.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from superset.models.connectors import SqlaTable

    if datasource_id is None or (datasource_type or "table") != "table":
        return []
    return list(
        session.execute(
            select(SqlaTable)
            .where(SqlaTable.id == datasource_id)
            .options(selectinload(SqlaTable.database))
        )
        .scalars()
        .all()
    )


def get_dashboard_digest(dashboard: "Dashboard") -> str | None:
    """Return the cache-key digest for ``dashboard``.

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

    if func := getattr(settings, "thumbnail_dashboard_digest_func", None):
        return func(dashboard, executor_type, executor)

    # The ``charts`` part (slice names) and the RLS datasources are both read
    # through one sync metadata session so the digest never depends on the
    # relationship-load state of a (possibly async-detached) ``Dashboard``.
    # ``dashboard.charts`` used to read ``self.slices`` directly — a sync
    # lazy-load on an ``AsyncSession``-bound object → ``MissingGreenlet`` →
    # HTTP 500 on the dashboard-list endpoint (which does not eager-load
    # ``slices``).  ``id``/``position_json``/``css``/``json_metadata`` are
    # plain columns (already loaded) so they stay inline.  The datasources
    # stay attached to ``session`` for ``get_sqla_row_level_filters`` (which
    # walks ``datasource.database`` and columns to build the template
    # processor) — hashed ``Dashboard.datasources``.
    from superset.utils.rls import _metadata_sync_session

    with _metadata_sync_session() as session:
        chart_names = _query_dashboard_slice_names(session, cast("int", dashboard.id))
        unique_string = (
            f"{dashboard.id}\n{chart_names}\n{dashboard.position_json}\n"
            f"{dashboard.css}\n{dashboard.json_metadata}"
        )
        unique_string = _adjust_string_for_executor(
            unique_string, executor_type, executor
        )
        datasources = _query_dashboard_datasources(session, cast("int", dashboard.id))
        unique_string = _adjust_string_with_rls(unique_string, datasources, executor)

    return md5_sha_from_str(unique_string)


def get_chart_digest(chart: "Slice") -> str | None:
    """Return the cache-key digest for ``chart``.

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

    if func := getattr(settings, "thumbnail_chart_digest_func", None):
        return func(chart, executor_type, executor)

    unique_string = f"{chart.params or ''}.{executor}"
    unique_string = _adjust_string_for_executor(unique_string, executor_type, executor)
    # Re-query the datasource through a sync metadata session (rather than
    # passing the async-bound ``chart.datasource``) so the RLS walk in
    # ``_adjust_string_with_rls`` → ``compose_rls_text_clauses`` can lazily
    # touch ``datasource.database`` (and columns) without tripping a
    # ``MissingGreenlet`` — the chart-list endpoint eager-loads only
    # ``Slice.table``, not ``SqlaTable.database``.  ``datasource_id`` /
    # ``datasource_type`` are plain columns (already loaded).
    from superset.utils.rls import _metadata_sync_session

    with _metadata_sync_session() as session:
        # ``Slice`` uses legacy ``Column`` mappings, so mypy types these
        # instance attributes as ``Column[...]`` rather than the runtime
        # int/str — narrow them to the helper's declared parameter types.
        datasources = _query_chart_datasources(
            session,
            cast("int | None", chart.datasource_id),
            cast("str | None", chart.datasource_type),
        )
        unique_string = _adjust_string_with_rls(unique_string, datasources, executor)

    return md5_sha_from_str(unique_string)


__all__ = (
    "_adjust_string_for_executor",
    "_adjust_string_with_rls",
    "get_chart_digest",
    "get_dashboard_digest",
)
