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
# mypy: ignore-errors
"""Warm-up cache command for charts.

* Legacy charts (``form_data["viz_type"] in viz_types``) go through
  :class:`superset.viz.BaseViz`'s :meth:`get_payload`. The request-scoped
  ``form_data`` is threaded via the ``_form_data_ctx`` :class:`ContextVar`
  exposed by :func:`superset.jinja_context.set_form_data`.

* Non-legacy charts (modern ``query_context``) go through
  :class:`AsyncQueryContextProcessor`.

* The ``try`` block wraps only the inner work (form-data parsing, viz / query
  execution); ``validate`` (chart fetch) is outside the try, so a missing chart
  still raises :class:`ObjectNotFoundError`.
"""

from __future__ import annotations

import logging
from typing import Any, cast, TYPE_CHECKING

from superset.commands.base import AsyncBaseCommand
from superset.exceptions import CommandInvalidError, ObjectNotFoundError
from superset.utils.core import error_msg_from_exception

if TYPE_CHECKING:
    from superset.db.daos.chart import AsyncChartDAO

logger = logging.getLogger(__name__)


class WarmUpChartCacheCommand(AsyncBaseCommand[dict[str, Any]]):
    """Warm up the cache for a chart by executing its viz / query context.

    Returns a ``{"chart_id", "viz_error", "viz_status"}`` dict.
    """

    def __init__(
        self,
        dao: "AsyncChartDAO",
        chart_id: int | None = None,
        dashboard_id: int | None = None,
        extra_filters: str | None = None,
        security_manager: Any | None = None,
        current_user: Any | None = None,
        chart: Any | None = None,
    ) -> None:
        self._dao = dao
        self._chart = chart
        self._chart_id = (
            chart_id if chart_id is not None else getattr(chart, "id", None)
        )
        self._dashboard_id = dashboard_id
        self._extra_filters = extra_filters
        self._security_manager = security_manager
        self._current_user = current_user

    async def validate(self) -> None:
        """Eagerly load the chart with its datasource + database.

        Pre-loads relationships so the ``run`` body stays free of
        awaits-on-attribute-access. When the command was constructed with
        an already-loaded Slice instance (the dataset warm-up passes charts
        directly), returns without a DB round-trip — the caller is responsible
        for eager-loading the relationship chain.
        """
        if self._chart is not None:
            return
        from sqlalchemy import select as sa_select
        from sqlalchemy.orm import selectinload

        from superset.models.connectors import SqlaTable
        from superset.models.slice import Slice

        stmt = (
            sa_select(Slice)
            .where(Slice.id == self._chart_id)
            .options(
                selectinload(Slice.table).selectinload(SqlaTable.database),
                # Eager-load columns + metrics — the query build later reads
                # them, and a sync lazy-load on the async session raises
                # ``greenlet_spawn has not been called``.
                selectinload(Slice.table).selectinload(SqlaTable.columns),
                selectinload(Slice.table).selectinload(SqlaTable.metrics),
                selectinload(Slice.owners),
            )
        )
        result = await self._dao.session.execute(stmt)
        self._chart = result.scalars().one_or_none()
        if not self._chart:
            raise ObjectNotFoundError("Chart", self._chart_id)

    async def _get_dashboard_filters(self, chart_id: int) -> list[dict[str, Any]]:
        """Return dashboard ``extra_filters`` for the chart, if any.

        ``extra_filters`` overrides dashboard metadata; dashboard metadata
        is consulted only when no explicit overrides are present.
        """
        import json as _stdlib_json

        if not self._dashboard_id:
            return []

        if self._extra_filters:
            return _stdlib_json.loads(self._extra_filters)

        return await self._build_dashboard_extra_filters(chart_id, self._dashboard_id)

    async def _build_dashboard_extra_filters(
        self, slice_id: int, dashboard_id: int
    ) -> list[dict[str, Any]]:
        """Compute dashboard default filters that apply to ``slice_id``.

        Reads the dashboard JSON metadata + position layout and returns
        the default filters. Falls back to an empty list when metadata is
        missing or malformed.
        """
        import contextlib
        import json as _stdlib_json

        from sqlalchemy import select as sa_select
        from sqlalchemy.orm import selectinload

        from superset.models.dashboard import Dashboard

        # Eager-load ``slices`` — read synchronously just below; a bare select
        # would trip MissingGreenlet on the async session.
        stmt = (
            sa_select(Dashboard)
            .where(Dashboard.id == dashboard_id)
            .options(selectinload(Dashboard.slices))
        )
        dashboard = (await self._dao.session.execute(stmt)).scalars().one_or_none()
        if (
            dashboard is None
            or not dashboard.json_metadata
            or not dashboard.slices
            or not any(slc for slc in dashboard.slices if slc.id == slice_id)
        ):
            return []

        with contextlib.suppress(_stdlib_json.JSONDecodeError):
            json_metadata = _stdlib_json.loads(dashboard.json_metadata)
            default_filters = _stdlib_json.loads(
                json_metadata.get("default_filters", "null")
            )
            if not default_filters:
                return []

            filter_scopes = json_metadata.get("filter_scopes", {})
            layout = _stdlib_json.loads(dashboard.position_json or "{}")

            if (
                isinstance(layout, dict)
                and isinstance(filter_scopes, dict)
                and isinstance(default_filters, dict)
            ):
                from superset.legacy import build_extra_filters

                # Pre-fetch the filter-box slices' params — the original
                # reads them off the sync session inline; the async port
                # passes them into the pure function (R11-07).
                filter_params_by_id: dict[str, str | None] = {}
                filter_ids = [
                    int(fid)
                    for fid in default_filters
                    if str(fid).lstrip("-").isdigit()
                ]
                if filter_ids:
                    from superset.models.slice import Slice as _Slice

                    rows = (
                        await self._dao.session.execute(
                            sa_select(_Slice.id, _Slice.params).where(
                                _Slice.id.in_(filter_ids)
                            )
                        )
                    ).all()
                    filter_params_by_id = {str(row[0]): row[1] for row in rows}

                return build_extra_filters(
                    layout,
                    filter_scopes,
                    default_filters,
                    slice_id,
                    filter_params_by_id=filter_params_by_id,
                )
        return []

    async def _warm_up_legacy_cache(
        self,
        chart: Any,
        form_data: dict[str, Any],
        security_manager: Any = None,
        current_user: Any = None,
    ) -> tuple[Any, Any]:
        """Warm up cache for legacy visualizations.

        Sets the ``_form_data_ctx`` ContextVar so downstream Jinja helpers
        see the correct form_data context.

        ``security_manager`` and ``current_user`` are injected so that
        ``viz_obj._rls_cache_key`` can be populated before
        ``viz_obj.get_payload()`` calls ``cache_key()``, differentiating
        cache entries for users with different RLS rules.
        """
        from superset.jinja_context import _form_data_ctx, set_form_data
        from superset.viz import get_viz

        if not chart.datasource:
            # Original raises ``ChartInvalidError`` (subclass of
            # ``CommandInvalidError``); we use the centralized
            # :class:`CommandInvalidError` since the chart-specific alias
            # has not been added to :mod:`superset.commands.chart.exceptions`.
            raise CommandInvalidError("Chart's datasource does not exist")

        if self._dashboard_id:
            form_data["extra_filters"] = await self._get_dashboard_filters(chart.id)

        token = _form_data_ctx.set(form_data)
        try:
            set_form_data(form_data)
            viz_obj = get_viz(
                datasource=chart.datasource,
                form_data=form_data,
                force=True,
            )

            # Populate _rls_cache_key before cache_key() is called inside
            # get_payload().  Mirrors ``security_manager.get_rls_cache_key``
            # which was invoked synchronously inside the original
            # BaseViz.cache_key().  Skipped when no security_manager is
            # available (e.g. tests) — the key defaults to [] (safe but
            # not RLS-differentiated).
            if security_manager is not None:
                try:
                    viz_obj._rls_cache_key = (  # noqa: SLF001
                        await security_manager.get_rls_cache_key(
                            chart.datasource, user=current_user
                        )
                    )
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "Could not populate _rls_cache_key for warm-up cache",
                        exc_info=True,
                    )

            payload = await viz_obj.get_payload()
        finally:
            try:
                _form_data_ctx.reset(token)
            except (LookupError, ValueError):
                _form_data_ctx.set(None)

        return payload.get("errors") or None, payload.get("status")

    def _build_queries(self, qc_dict: dict[str, Any], datasource: Any) -> list[Any]:
        """Build AsyncQueryObject list from stored query_context dict.

        ``AsyncQueryObject.datasource`` is a required dataclass field; the
        stored query_context's per-query dicts don't include it (it lives at
        the top level), so construct via ``from_request`` with the chart's
        datasource — this matches how the chart-data controller builds queries.

        Dashboard filters are NOT applied here — apply them in the caller
        (``_warm_up_non_legacy_cache``) via ``_get_dashboard_filters`` so that
        both the ``_extra_filters`` path *and* the dashboard-metadata path are
        used, exactly as the original does via
        ``_get_dashboard_filters(chart.id)`` inside ``_warm_up_non_legacy_cache``.
        """
        from superset.common.query_object import AsyncQueryObject

        ds_dict = {
            "type": getattr(datasource, "type", "table"),
            "id": getattr(datasource, "id", 0),
        }
        queries: list[AsyncQueryObject] = []
        for q in qc_dict.get("queries", []):
            qo = AsyncQueryObject.from_request(q, ds_dict)
            queries.append(qo)

        if not queries:
            raise CommandInvalidError("Chart query_context has no queries")

        return queries

    async def _warm_up_non_legacy_cache(self, chart: Any) -> tuple[Any, Any]:
        """Warm up cache for modern (query_context-based) visualizations."""
        import json as _stdlib_json

        from superset.common.query_context import AsyncQueryContext
        from superset.common.query_context_processor import (
            AsyncQueryContextProcessor,
        )
        from superset.common.query_status import QueryStatus
        from superset.config import SupersetSettings

        query_context_raw = chart.query_context
        if not query_context_raw:
            raise CommandInvalidError("Chart's query context does not exist")

        qc_dict = _stdlib_json.loads(query_context_raw)

        datasource = chart.table
        if not datasource:
            raise CommandInvalidError("Chart's datasource does not exist")

        queries = self._build_queries(qc_dict, datasource)

        if dashboard_filters := await self._get_dashboard_filters(chart.id):
            for qo in queries:
                if hasattr(qo, "filters") and isinstance(qo.filters, list):
                    qo.filters.extend(dashboard_filters)
                elif hasattr(qo, "filter") and isinstance(qo.filter, list):
                    qo.filter.extend(dashboard_filters)

        query_context = AsyncQueryContext(
            datasource=datasource,
            queries=queries,
            force=True,
            slice_=chart,
        )

        processor = AsyncQueryContextProcessor(
            datasource=datasource,
            settings=SupersetSettings(),
            # Pass the security_manager so the processor can populate the RLS cache key.
            security_manager=self._security_manager,
            user=self._current_user,
            query_context=query_context,
        )

        if self._security_manager is not None:
            await processor.raise_for_access()

        payload = await processor.get_payload(
            query_objects=queries,
            force=True,
        )

        for query_result in cast(list[dict[str, Any]], payload.get("queries", [])):
            error = query_result.get("error")
            status = query_result.get("status")
            if error is not None:
                return error, status

        return None, QueryStatus.SUCCESS

    async def run(self) -> dict[str, Any]:
        """Execute the warm-up.

        ``validate`` runs outside the try so a missing chart raises
        :class:`ObjectNotFoundError`. Only form-data resolution and viz /
        query execution are wrapped; the catch-all returns the canonical
        ``{chart_id, viz_error, viz_status}`` shape.
        """
        from superset.viz import get_active_viz_types

        assert self._chart is not None
        chart = self._chart

        try:
            form_data = chart.form_data
            if form_data.get("viz_type") in get_active_viz_types():
                error, status = await self._warm_up_legacy_cache(
                    chart,
                    form_data,
                    security_manager=self._security_manager,
                    current_user=self._current_user,
                )
            else:
                error, status = await self._warm_up_non_legacy_cache(chart)
        except Exception as ex:  # pylint: disable=broad-except
            error = error_msg_from_exception(ex)
            status = None

        return {"chart_id": chart.id, "viz_error": error, "viz_status": status}
