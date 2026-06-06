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
"""Report Schedule controller — CRUD endpoints for report schedules."""

from __future__ import annotations

import logging
from typing import Any

import msgspec
from litestar import Controller, delete, get, post, put
from litestar.di import Provide

logger = logging.getLogger(__name__)

from superset.commands.report import (
    BulkDeleteReportScheduleCommand,
    CreateReportScheduleCommand,
    DeleteReportScheduleCommand,
    UpdateReportScheduleCommand,
)
from superset.controllers.base import (
    build_rison_query_params,
    extract_ids_required,
    get_info_payload,
    get_related_payload,
    serialize_list_response,
)
from superset.events import event_logger
from superset.exceptions import ObjectNotFoundError
from superset.guards.rbac import require_feature_flag, require_permission
from superset.params.rison import provide_rison_query
from superset.providers import provide_report_dao
from superset.schemas.report import (
    ReportDetailResult,
    ReportSchedulePostSchema,
    ReportSchedulePutSchema,
)
from superset.typing import UserProtocol
from superset.utils import filter_unset

def _get_slack_channels(
    search_string: str | None = None,
    types: list[str] | None = None,
    exact_match: bool = False,
    force: bool = False,
) -> list[dict[str, str]]:
    """Fetch Slack channels from the API (with optional filtering).

    1:1 port of ``superset_old/utils/slack.py:get_channels_with_search``.

    The Slack API is paginated but does not support server-side search,
    so we fetch all channels and filter client-side.  Results are cached
    under the ``slack_conversations_list`` key via the Superset cache
    backend (mirroring the original ``@cache_util.memoized_func`` decorator).

    Returns a list of ``{"id": ..., "name": ...}`` dicts.
    Raises :exc:`RuntimeError` / :exc:`Exception` on Slack API errors so
    the controller can map them to HTTP 422.
    """
    try:
        from slack_sdk import WebClient
        from slack_sdk.errors import SlackApiError
        from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler
    except ImportError:
        logger.warning(
            "slack_sdk is not installed; cannot list Slack channels. "
            "Install with: pip install slack_sdk"
        )
        return []

    from superset.config import SupersetSettings

    settings = SupersetSettings()  # type: ignore[call-arg]
    slack_token = getattr(settings, "slack_api_token", None)
    if not slack_token:
        logger.info("SLACK_API_TOKEN not configured; returning empty channel list")
        return []
    if callable(slack_token):
        slack_token = slack_token()

    slack_proxy = getattr(settings, "slack_proxy", None) or None
    max_retry_count = getattr(settings, "slack_api_rate_limit_retry_count", 2) or 2
    slack_cache_timeout = getattr(settings, "slack_cache_timeout", 1800) or 1800

    # ------------------------------------------------------------------
    # Cache read (best-effort; skip on error)
    # ------------------------------------------------------------------
    _CACHE_KEY = "slack_conversations_list"
    cached_channels: list[dict[str, str]] | None = None
    if not force:
        try:
            from superset.extensions import cache_manager as _cm
            import asyncio
            import json as _json

            # Run the async cache get in a new event loop (Celery / non-async context)
            async def _aget() -> Any:
                raw = await _cm.cache.get(_CACHE_KEY)
                return raw

            try:
                loop = asyncio.get_event_loop()
                if loop.is_closed():
                    raise RuntimeError("closed loop")
                raw = loop.run_until_complete(_aget())
            except RuntimeError:
                raw = asyncio.run(_aget())

            if raw is not None:
                if isinstance(raw, (bytes, bytearray)):
                    raw = raw.decode("utf-8", errors="replace")
                cached_channels = _json.loads(raw)
        except Exception:  # noqa: BLE001
            pass

    if cached_channels is not None:
        all_channels: list[dict[str, str]] = cached_channels
    else:
        # ------------------------------------------------------------------
        # Fetch from Slack API
        # ------------------------------------------------------------------
        client = WebClient(token=slack_token, proxy=slack_proxy)
        rate_limit_handler = RateLimitErrorRetryHandler(max_retry_count=max_retry_count)
        client.retry_handlers.append(rate_limit_handler)

        all_channels = []
        cursor = None
        page_count = 0
        try:
            while True:
                page_count += 1
                response = client.conversations_list(
                    limit=999,
                    cursor=cursor,
                    exclude_archived=True,
                    types="public_channel,private_channel",
                )
                page_channels = response.data.get("channels", [])
                for ch in page_channels:
                    all_channels.append({
                        "id": ch.get("id", ""),
                        "name": ch.get("name", ""),
                        "is_member": ch.get("is_member", False),
                        "is_private": ch.get("is_private", False),
                    })
                cursor = response.data.get("response_metadata", {}).get("next_cursor")
                if not cursor:
                    break
        except SlackApiError as ex:
            status_code = getattr(ex.response, "status_code", None)
            if status_code == 429:
                raise RuntimeError(
                    f"Slack API rate limit exceeded: {ex}. "
                    "Consider increasing SLACK_API_RATE_LIMIT_RETRY_COUNT"
                ) from ex
            raise RuntimeError(f"Failed to list channels: {ex}") from ex

        # Cache the result (best-effort)
        try:
            from superset.extensions import cache_manager as _cm
            import asyncio
            import json as _json

            serialized = _json.dumps(all_channels)

            async def _aset() -> None:
                await _cm.cache.set(_CACHE_KEY, serialized, ttl=slack_cache_timeout)

            try:
                loop = asyncio.get_event_loop()
                if loop.is_closed():
                    raise RuntimeError("closed loop")
                loop.run_until_complete(_aset())
            except RuntimeError:
                asyncio.run(_aset())
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # Client-side filtering (mirrors get_channels_with_search)
    # ------------------------------------------------------------------
    channels = all_channels

    if types:
        type_set = set(types)
        filtered: list[dict[str, str]] = []
        for ch in channels:
            is_private = ch.get("is_private", False)
            if "public_channel" in type_set and not is_private:
                filtered.append(ch)
            elif "private_channel" in type_set and is_private:
                filtered.append(ch)
        channels = filtered

    if search_string:
        # Support comma-separated list of channel names / ids
        search_terms = [s.strip() for s in search_string.replace(",", " ").split() if s.strip()]
        matched: list[dict[str, str]] = []
        for ch in channels:
            ch_name = (ch.get("name") or "").lower()
            ch_id = (ch.get("id") or "").lower()
            for term in search_terms:
                t = term.lower()
                if exact_match:
                    if t == ch_name or t == ch_id:
                        matched.append(ch)
                        break
                else:
                    if t in ch_name or t in ch_id:
                        matched.append(ch)
                        break
        channels = matched

    return channels


def _report_custom_filters() -> dict[str, Any]:
    """Search-filter map for the report list endpoint.

    Registers ``report_all_text`` — a 1:1 port of
    ``superset_old/reports/filters.py::ReportScheduleAllTextFilter`` — which
    OR-matches the search term across ``name``, ``description`` and ``sql``.
    """

    def _report_all_text(model_cls: Any, value: Any) -> Any:
        if not value:
            return None
        from sqlalchemy import or_

        ilike_value = f"%{value}%"
        return or_(
            model_cls.name.ilike(ilike_value),
            model_cls.description.ilike(ilike_value),
            model_cls.sql.ilike(ilike_value),
        )

    return {"report_all_text": _report_all_text}


_LIST_COLUMNS = [
    "id",
    "name",
    "type",
    "description",
    "active",
    "crontab",
    "crontab_humanized",
    "creation_method",
    "timezone",
    "report_format",
    "chart_id",
    "dashboard_id",
    "database_id",
    "extra",
    "last_eval_dttm",
    "last_state",
    "last_value",
    "log_retention",
    "grace_period",
    "working_timeout",
    "changed_on",
    "changed_on_delta_humanized",
    "changed_on_utc",
    "changed_by.first_name",
    "changed_by.last_name",
    "created_on",
    "created_by.first_name",
    "created_by.last_name",
    "owners.id",
    "owners.first_name",
    "owners.last_name",
    "recipients.id",
    "recipients.type",
]


class ReportScheduleController(Controller):
    path = "/api/v1/report"
    tags = ["Report Schedule"]
    # 1:1 with the original ``@before_request ensure_alert_reports_enabled``
    # (superset_old/reports/api.py:69-73): every report endpoint returns 404
    # when the ALERT_REPORTS feature flag is disabled.
    guards = [require_feature_flag("ALERT_REPORTS")]
    dependencies = {
        "dao": Provide(provide_report_dao, sync_to_thread=False),
        "rison_params": Provide(provide_rison_query),
    }

    @get(
        "/",
        guards=[require_permission("can_read", "ReportSchedule")],
    )
    async def get_list(
        self,
        dao: Any,
        rison_params: dict[str, Any] | None,
        current_user: UserProtocol,
        security_manager: Any,
    ) -> dict[str, Any]:
        """GET /api/v1/report/ — list report schedules with pagination."""
        from sqlalchemy.orm import selectinload

        from superset.db.filters import report_access_filters
        from superset.models.reports import ReportSchedule

        rison_filters, order_by, page, page_size = build_rison_query_params(
            ReportSchedule,
            rison_params,
            custom_filters=_report_custom_filters(),
        )
        if not order_by:
            order_by = [ReportSchedule.changed_on.desc()]

        # Owner-scope visibility (1:1 with ``ReportScheduleFilter``): non
        # ``can_access_all_datasources`` users only see reports they own.
        base_filters = await report_access_filters(security_manager, current_user)
        all_filters = (rison_filters or []) + base_filters

        items = await dao.find_all(
            filters=all_filters or None,
            page=page,
            page_size=page_size,
            order_by=order_by,
            options=[
                selectinload(ReportSchedule.owners),
                selectinload(ReportSchedule.recipients),
                selectinload(ReportSchedule.changed_by),
                selectinload(ReportSchedule.created_by),
            ],
        )
        total = await dao.count(filters=all_filters or None)
        await event_logger.alog_with_context("report.list")
        return serialize_list_response(
            items,
            total,
            _LIST_COLUMNS,
            list_title="List Report Schedule",
        )

    @get(
        "/{pk:int}",
        guards=[require_permission("can_read", "ReportSchedule")],
    )
    async def get_report(
        self,
        pk: int,
        dao: Any,
        current_user: UserProtocol,
        security_manager: Any,
    ) -> dict[str, Any]:
        """GET /api/v1/report/<pk> — get a single report schedule."""
        from sqlalchemy.orm import selectinload

        from superset.db.filters import report_access_filters
        from superset.models.reports import ReportSchedule

        # Owner-scope visibility (1:1 with ``ReportScheduleFilter``): non
        # ``can_access_all_datasources`` users can only read reports they own.
        base_filters = await report_access_filters(security_manager, current_user)
        results = await dao.find_all(
            filters=[ReportSchedule.id == pk, *base_filters],
            page=0,
            page_size=1,
            options=[
                selectinload(ReportSchedule.chart),
                selectinload(ReportSchedule.dashboard),
                selectinload(ReportSchedule.database),
                selectinload(ReportSchedule.owners),
                selectinload(ReportSchedule.recipients),
            ],
        )
        if not results:
            raise ObjectNotFoundError("ReportSchedule", pk)
        report = results[0]
        await event_logger.alog_with_context("report.get", object_ref=f"report:{pk}")
        return {
            "id": report.id,
            "result": ReportDetailResult.from_model(report),
        }

    @post(
        "/",
        guards=[require_permission("can_write", "ReportSchedule")],
        status_code=201,
    )
    async def create_report(
        self,
        data: ReportSchedulePostSchema,
        dao: Any,
        current_user: UserProtocol,
        security_manager: Any,
    ) -> dict[str, Any]:
        """POST /api/v1/report/ — create a report schedule."""
        raw = msgspec.structs.asdict(data)
        # Convert recipient structs to dicts
        if raw.get("recipients"):
            raw["recipients"] = [
                msgspec.structs.asdict(r) if hasattr(r, "__struct_fields__") else r
                for r in raw["recipients"]
            ]
        # Echo the validated input payload back in ``result`` — 1:1 with the
        # original ``self.response(201, id=new_model.id, result=item)``
        # (superset_old/reports/api.py:366-367). Snapshot before the command
        # mutates ``raw`` (it resolves chart/dashboard ids to ORM objects and
        # serializes validator_config_json).
        echo = dict(raw)
        cmd = CreateReportScheduleCommand(
            dao=dao,
            data=raw,
            user_id=current_user.id,
            security_manager=security_manager,
        )
        item = await cmd.execute()
        await event_logger.alog_with_context(
            "report.create",
            object_ref=str(item.id),
            user_id=current_user.id,
        )
        return {"id": item.id, "result": echo}

    @put(
        "/{pk:int}",
        guards=[require_permission("can_write", "ReportSchedule")],
    )
    async def update_report(
        self,
        pk: int,
        data: ReportSchedulePutSchema,
        dao: Any,
        current_user: UserProtocol,
        security_manager: Any,
    ) -> dict[str, Any]:
        """PUT /api/v1/report/<pk> — update a report schedule."""
        raw = filter_unset(msgspec.structs.asdict(data))
        # Convert recipient structs to dicts
        if raw.get("recipients"):
            raw["recipients"] = [
                msgspec.structs.asdict(r) if hasattr(r, "__struct_fields__") else r
                for r in raw["recipients"]
            ]
        cmd = UpdateReportScheduleCommand(
            dao=dao,
            pk=pk,
            data=raw,
            user_id=current_user.id,
            security_manager=security_manager,
        )
        item = await cmd.execute()
        await event_logger.alog_with_context(
            "report.update",
            object_ref=f"report:{pk}",
            user_id=current_user.id,
        )
        return {"id": item.id, "result": {"name": item.name}}

    @delete(
        "/{pk:int}",
        guards=[require_permission("can_write", "ReportSchedule")],
        status_code=200,
    )
    async def delete_report(
        self,
        pk: int,
        dao: Any,
        current_user: UserProtocol,
        security_manager: Any,
    ) -> dict[str, str]:
        """DELETE /api/v1/report/<pk> — delete a single report schedule."""
        cmd = DeleteReportScheduleCommand(
            dao=dao,
            pk=pk,
            user_id=current_user.id,
            security_manager=security_manager,
        )
        await cmd.execute()
        await event_logger.alog_with_context("report.delete", object_ref=f"report:{pk}")
        return {"message": "OK"}

    @delete(
        "/",
        guards=[require_permission("can_write", "ReportSchedule")],
        status_code=200,
    )
    async def bulk_delete(
        self,
        dao: Any,
        rison_params: list[int] | dict[str, Any] | None,
        current_user: UserProtocol,
        security_manager: Any,
    ) -> dict[str, str]:
        """DELETE /api/v1/report/ — bulk delete report schedules."""
        ids = extract_ids_required(rison_params)
        cmd = BulkDeleteReportScheduleCommand(
            dao=dao,
            ids=ids,
            user_id=current_user.id,
            security_manager=security_manager,
        )
        await cmd.execute()
        await event_logger.alog_with_context(
            "report.bulk_delete", extra={"count": len(ids)}
        )
        return {"message": "OK"}

    @get(
        "/related/{column_name:str}",
        guards=[require_permission("can_read", "ReportSchedule")],
    )
    async def related(
        self,
        column_name: str,
        dao: Any,
        rison_params: dict[str, Any] | None,
        current_user: UserProtocol,
        security_manager: Any,
    ) -> dict[str, Any]:
        """GET /api/v1/report/related/{column_name} — related values for dropdowns."""
        from superset.db.filters import report_access_filters

        base_filters = await report_access_filters(security_manager, current_user)
        return await get_related_payload(
            dao=dao,
            column_name=column_name,
            rison_params=rison_params,
            allowed_fields=frozenset(
                {"owners", "created_by", "chart", "dashboard", "database"}
            ),
            base_filters=base_filters or None,
        )

    @get(
        "/_info",
        guards=[require_permission("can_read", "ReportSchedule")],
    )
    async def info(self, dao: Any) -> dict[str, Any]:
        """GET /api/v1/report/_info -- API metadata for frontend."""
        return await get_info_payload(
            dao=dao,
            model_name="ReportSchedule",
            permissions=["can_read", "can_write"],
        )

    @get(
        "/slack_channels/",
        guards=[require_permission("can_read", "ReportSchedule")],
    )
    async def slack_channels(
        self,
        search_string: str | None = None,
        types: list[str] | None = None,
        exact_match: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        """GET /api/v1/report/slack_channels/ -- list Slack channels.

        1:1 port of ``superset_old/reports/api.py:ReportScheduleRestApi.slack_channels``.
        Queries the Slack API for all accessible channels, applies optional
        filtering by name/id (``search_string``), and returns a list of
        ``{id, name}`` dicts.

        Returns an empty list when Slack integration is not configured or
        ``slack_sdk`` is not installed.

        Parameters
        ----------
        search_string:
            Comma-separated channel names or IDs to filter by.  Supports
            partial matching unless ``exact_match`` is ``True``.
        types:
            List of channel types to include; e.g. ``["public_channel"]``,
            ``["private_channel"]``, or both.  When ``None`` (default) all
            channel types are returned.
        exact_match:
            When ``True``, only channels whose name or id matches exactly.
        force:
            When ``True``, bypass the cached channel list and re-fetch from
            the Slack API.
        """
        from litestar.exceptions import HTTPException

        try:
            channels = _get_slack_channels(
                search_string=search_string,
                types=types,
                exact_match=exact_match,
                force=force,
            )
            return {"result": channels}
        except Exception as exc:  # noqa: BLE001
            logger.error("Error fetching slack channels: %s", str(exc), exc_info=True)
            raise HTTPException(
                status_code=422, detail=str(exc)
            ) from exc
