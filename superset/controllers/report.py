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

from superset.commands.report import (
    BulkDeleteReportScheduleCommand,
    CreateReportScheduleCommand,
    DeleteReportScheduleCommand,
    UpdateReportScheduleCommand,
)
from superset.commands.report_exceptions import (
    ReportScheduleCreateFailedError,
    ReportScheduleDeleteFailedError,
    ReportScheduleUpdateFailedError,
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
from superset.typing import SecurityManagerProtocol, UserProtocol
from superset.utils import filter_unset

logger = logging.getLogger(__name__)


_SLACK_CACHE_KEY = "slack_conversations_list"


def _slack_cache_get() -> list[dict[str, str]] | None:
    """Read the Slack channel list from the cache (best-effort).

    Returns the cached list if present, or ``None`` on cache miss or error.
    Uses the synchronous cache interface — avoids asyncio loop conflicts when
    called from a sync function running on the event loop thread.  1:1 with the
    original Flask ``@cache_util.memoized_func`` synchronous decorator in
    ``superset_old/utils/slack.py:62-65``.
    """
    import json as _json

    from superset.extensions import cache_manager as _cm

    try:
        value = _cm.sync_cache.get(_SLACK_CACHE_KEY)
        if value is None:
            return None
        if isinstance(value, list):
            return value
        # Legacy: JSON-string payload written by an older release
        if isinstance(value, (bytes, bytearray)):
            value = value.decode("utf-8", errors="replace")
        if isinstance(value, str):
            return _json.loads(value)
        return None
    except Exception:  # noqa: BLE001
        logger.debug("Slack cache read failed; will fetch from API", exc_info=True)
        return None


def _slack_cache_set(channels: list[dict[str, str]], ttl: int) -> None:
    """Write the Slack channel list to the cache (best-effort).

    Silently ignores errors so a cache failure never breaks the API call.
    Uses the synchronous cache interface — avoids asyncio loop conflicts when
    called from a sync function running on the event loop thread.  1:1 with the
    original Flask ``@cache_util.memoized_func`` synchronous decorator in
    ``superset_old/utils/slack.py:62-65``.
    """
    from superset.extensions import cache_manager as _cm

    try:
        _cm.sync_cache.set(_SLACK_CACHE_KEY, channels, ttl=ttl)
    except Exception:  # noqa: BLE001
        logger.debug("Slack cache write failed; ignoring", exc_info=True)


def _slack_fetch_all_channels(
    client: Any,
) -> list[dict[str, str]]:
    """Fetch all Slack channels via paginated ``conversations_list`` calls.

    Raises :exc:`RuntimeError` on :exc:`SlackApiError` (rate-limit or other).
    """
    from slack_sdk.errors import SlackApiError

    all_channels: list[dict[str, str]] = []
    cursor = None
    try:
        while True:
            response = client.conversations_list(
                limit=999,
                cursor=cursor,
                exclude_archived=True,
                types="public_channel,private_channel",
            )
            page_channels = response.data.get("channels", [])
            for ch in page_channels:
                all_channels.append(
                    {
                        "id": ch.get("id", ""),
                        "name": ch.get("name", ""),
                        "is_member": ch.get("is_member", False),
                        "is_private": ch.get("is_private", False),
                    }
                )
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
    return all_channels


def _slack_filter_by_type(
    channels: list[dict[str, str]],
    types: list[str],
) -> list[dict[str, str]]:
    """Filter channels to only the requested Slack channel types."""
    type_set = set(types)
    filtered: list[dict[str, str]] = []
    for ch in channels:
        is_private = ch.get("is_private", False)
        if "public_channel" in type_set and not is_private:
            filtered.append(ch)
        elif "private_channel" in type_set and is_private:
            filtered.append(ch)
    return filtered


def _slack_filter_by_search(
    channels: list[dict[str, str]],
    search_string: str,
    exact_match: bool,
) -> list[dict[str, str]]:
    """Filter channels by name / id search terms.

    Splits ``search_string`` on comma, whitespace, OR semicolon — 1:1 with
    ``superset_old/utils/core.py::recipients_string_to_list`` which uses
    ``re.split(r',|\\s|;', address_string)`` (called from
    ``superset_old/utils/slack.py:162``).
    """
    import re

    search_terms = [s for s in re.split(r",|\s|;", search_string) if s.strip()]
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
    return matched


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
    if callable(slack_token):
        slack_token = slack_token()
    # NB: no early-return when the token is missing — the original creates
    # ``WebClient(token=None)`` and lets Slack answer ``not_authed``
    # (SlackApiError -> SupersetException -> HTTP 422,
    # superset_old/utils/slack.py:47-58); short-circuiting to ``[]`` here
    # would turn that 422 into a silent 200.

    slack_proxy = getattr(settings, "slack_proxy", None) or None
    max_retry_count = getattr(settings, "slack_api_rate_limit_retry_count", 2) or 2
    slack_cache_timeout = getattr(settings, "slack_cache_timeout", 1800) or 1800

    # ------------------------------------------------------------------
    # Cache read (best-effort; skip on error)
    # ------------------------------------------------------------------
    cached_channels: list[dict[str, str]] | None = None
    if not force:
        cached_channels = _slack_cache_get()

    if cached_channels is not None:
        all_channels: list[dict[str, str]] = cached_channels
    else:
        # ------------------------------------------------------------------
        # Fetch from Slack API
        # ------------------------------------------------------------------
        client = WebClient(token=slack_token, proxy=slack_proxy)
        rate_limit_handler = RateLimitErrorRetryHandler(max_retry_count=max_retry_count)
        client.retry_handlers.append(rate_limit_handler)

        all_channels = _slack_fetch_all_channels(client)

        # Cache the result (best-effort)
        _slack_cache_set(all_channels, ttl=slack_cache_timeout)

    # ------------------------------------------------------------------
    # Client-side filtering (mirrors get_channels_with_search)
    # ------------------------------------------------------------------
    channels = all_channels

    if types:
        channels = _slack_filter_by_type(channels, types)

    if search_string:
        channels = _slack_filter_by_search(channels, search_string, exact_match)

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


# Exactly mirrors ``superset_old/reports/api.py::ReportScheduleRestApi.list_columns``
# (26 fields).  Fields present only in ``show_columns`` (report_format,
# database_id, last_value, log_retention, grace_period, working_timeout) and
# the liteset-only ``changed_on_utc`` are intentionally absent — they belong to
# the detail endpoint only.
_LIST_COLUMNS = [
    "active",
    "changed_by.first_name",
    "changed_by.last_name",
    "changed_on",
    "changed_on_delta_humanized",
    "chart_id",
    "created_by.first_name",
    "created_by.last_name",
    "created_on",
    "creation_method",
    "crontab",
    "crontab_humanized",
    "dashboard_id",
    "description",
    "extra",
    "id",
    "last_eval_dttm",
    "last_state",
    "name",
    "owners.first_name",
    "owners.id",
    "owners.last_name",
    "recipients.id",
    "recipients.type",
    "timezone",
    "type",
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
        # filter_unset removes fields whose default is msgspec.UNSET (e.g.
        # ``validator_type`` which uses UNSET to distinguish absent from explicit
        # null — explicit null is rejected at decode time with HTTP 422,
        # matching the original Marshmallow schema's no-allow_none behaviour).
        raw = filter_unset(msgspec.structs.asdict(data))
        # Convert recipient structs to plain dicts.  msgspec.structs.asdict()
        # does NOT recursively convert nested Struct instances, so after the
        # outer asdict() the recipient_config_json field is still a
        # ReportRecipientConfigJSON struct.  The DAO checks
        # isinstance(config, dict) — which is False for a struct — and passes
        # the struct directly to asyncpg, raising ProgrammingError → 422.
        # Fix: convert the nested recipient_config_json struct to a plain dict
        # as well (identical to how validator_config_json is handled below).
        if raw.get("recipients"):
            converted_recipients = []
            for r in raw["recipients"]:
                r_dict = (
                    msgspec.structs.asdict(r) if hasattr(r, "__struct_fields__") else r
                )
                rcj = r_dict.get("recipient_config_json")
                if rcj is msgspec.UNSET:
                    # Optional in the original (no required=True on the
                    # Nested field) — absent config keeps the model default
                    # ``'{}'``.
                    r_dict.pop("recipient_config_json", None)
                elif rcj is not None and hasattr(rcj, "__struct_fields__"):
                    r_dict["recipient_config_json"] = {
                        k: v
                        for k, v in msgspec.structs.asdict(rcj).items()
                        if v is not msgspec.UNSET
                    }
                converted_recipients.append(r_dict)
            raw["recipients"] = converted_recipients
        # Convert validator_config_json struct to a plain dict so that the
        # command's json.dumps() call succeeds — msgspec.structs.asdict() does
        # NOT recursively convert nested Struct instances (original Marshmallow
        # deserialized this to a plain dict; the command expects a plain dict).
        # Also strip UNSET values so json.dumps() doesn't fail on them (fields
        # in ValidatorConfigJSON are optional via msgspec.UNSET, mirroring the
        # original's Marshmallow ``required=False``).
        if raw.get("validator_config_json") is not None and hasattr(
            raw["validator_config_json"], "__struct_fields__"
        ):
            vcj_raw = msgspec.structs.asdict(raw["validator_config_json"])
            raw["validator_config_json"] = {
                k: v for k, v in vcj_raw.items() if v is not msgspec.UNSET
            }
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
        try:
            item = await cmd.execute()
        except ObjectNotFoundError as ex:
            # 1:1 with original POST handler
            # (superset_old/reports/api.py:368-369): a "not found" during
            # creation means an invalid resource reference (chart, dashboard,
            # database), so return 400 instead of the default 404.
            from litestar.exceptions import HTTPException

            raise HTTPException(status_code=400, detail=str(ex)) from ex
        except ReportScheduleCreateFailedError as ex:
            # 1:1 with superset_old/reports/api.py:372-379 — DB-level
            # failure during create returns 422 with the error message.
            logger.error("Error creating report schedule: %s", str(ex), exc_info=True)
            from litestar.exceptions import HTTPException

            raise HTTPException(status_code=422, detail=str(ex)) from ex
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
        # Convert recipient structs to plain dicts.  msgspec.structs.asdict()
        # does NOT recursively convert nested Struct instances, so after the
        # outer asdict() the recipient_config_json field is still a
        # ReportRecipientConfigJSON struct.  The DAO checks
        # isinstance(config, dict) — which is False for a struct — and passes
        # the struct directly to asyncpg, raising ProgrammingError → 422.
        # Fix: convert the nested recipient_config_json struct to a plain dict
        # as well (identical to how validator_config_json is handled below).
        if raw.get("recipients"):
            converted_recipients = []
            for r in raw["recipients"]:
                r_dict = (
                    msgspec.structs.asdict(r) if hasattr(r, "__struct_fields__") else r
                )
                rcj = r_dict.get("recipient_config_json")
                if rcj is msgspec.UNSET:
                    # Optional in the original (no required=True on the
                    # Nested field) — absent config keeps the model default
                    # ``'{}'``.
                    r_dict.pop("recipient_config_json", None)
                elif rcj is not None and hasattr(rcj, "__struct_fields__"):
                    r_dict["recipient_config_json"] = {
                        k: v
                        for k, v in msgspec.structs.asdict(rcj).items()
                        if v is not msgspec.UNSET
                    }
                converted_recipients.append(r_dict)
            raw["recipients"] = converted_recipients
        # Convert validator_config_json struct to a plain dict so that the
        # command's json.dumps() call succeeds — msgspec.structs.asdict() does
        # NOT recursively convert nested Struct instances (original Marshmallow
        # deserialized this to a plain dict; the command expects a plain dict).
        # Also strip UNSET values so json.dumps() doesn't fail on them (fields
        # in ValidatorConfigJSON are optional via msgspec.UNSET, mirroring the
        # original's Marshmallow ``required=False``).
        if raw.get("validator_config_json") is not None and hasattr(
            raw["validator_config_json"], "__struct_fields__"
        ):
            vcj_raw = msgspec.structs.asdict(raw["validator_config_json"])
            raw["validator_config_json"] = {
                k: v for k, v in vcj_raw.items() if v is not msgspec.UNSET
            }
        # Echo the validated input payload back in ``result`` — 1:1 with the
        # original ``self.response(200, id=new_model.id, result=item)``
        # (superset_old/reports/api.py:447). Snapshot before the command
        # mutates ``raw`` (it resolves chart/dashboard ids to ORM objects and
        # serializes validator_config_json).
        echo = dict(raw)
        cmd = UpdateReportScheduleCommand(
            dao=dao,
            pk=pk,
            data=raw,
            user_id=current_user.id,
            security_manager=security_manager,
        )
        try:
            item = await cmd.execute()
        except ReportScheduleUpdateFailedError as ex:
            # 1:1 with superset_old/reports/api.py:454-461 — DB-level
            # failure during update returns 422.
            logger.error(
                "Error updating report schedule %d: %s", pk, str(ex), exc_info=True
            )
            from litestar.exceptions import HTTPException

            raise HTTPException(status_code=422, detail=str(ex)) from ex
        await event_logger.alog_with_context(
            "report.update",
            object_ref=f"report:{pk}",
            user_id=current_user.id,
        )
        return {"id": item.id, "result": echo}

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
        try:
            await cmd.execute()
        except ReportScheduleDeleteFailedError as ex:
            # 1:1 with superset_old/reports/api.py:299-306 and :520 —
            # DB-level failure during delete returns 422.
            logger.error(
                "Error deleting report schedule %d: %s", pk, str(ex), exc_info=True
            )
            from litestar.exceptions import HTTPException

            raise HTTPException(status_code=422, detail=str(ex)) from ex
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
        try:
            await cmd.execute()
        except ReportScheduleDeleteFailedError as ex:
            # 1:1 with superset_old/reports/api.py:520-521 —
            # DB-level failure during bulk-delete returns 422.
            logger.error(
                "Error deleting report schedules %s: %s",
                ids,
                str(ex),
                exc_info=True,
            )
            from litestar.exceptions import HTTPException

            raise HTTPException(status_code=422, detail=str(ex)) from ex
        await event_logger.alog_with_context(
            "report.bulk_delete", extra={"count": len(ids)}
        )
        # 1:1 with original ``ngettext('Deleted %(num)d report schedule',
        # 'Deleted %(num)d report schedules', num=len(item_ids))``
        # (superset_old/reports/api.py:508-514) — locale-aware plural forms.
        from superset.i18n import ngettext

        msg = ngettext(
            "Deleted %(num)d report schedule",
            "Deleted %(num)d report schedules",
            num=len(ids),
        )
        return {"message": msg}

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
                {"owners", "created_by", "changed_by", "chart", "dashboard", "database"}
            ),
            base_filters=base_filters or None,
            # 1:1 superset_old/reports/api.py:233-237.
            text_field_rel_fields={
                "dashboard": "dashboard_title",
                "chart": "slice_name",
                "database": "database_name",
            },
        )

    @get(
        "/_info",
        guards=[require_permission("can_read", "ReportSchedule")],
    )
    async def info(
        self,
        dao: Any,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
    ) -> dict[str, Any]:
        """GET /api/v1/report/_info -- API metadata for frontend."""
        return await get_info_payload(
            dao=dao,
            model_name="ReportSchedule",
            permissions=["can_read", "can_write"],
            security_manager=security_manager,
            current_user=current_user,
            class_permission_name="ReportSchedule",
        )

    @get(
        "/slack_channels/",
        guards=[require_permission("can_read", "ReportSchedule")],
    )
    async def slack_channels(
        self,
        rison_params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """GET /api/v1/report/slack_channels/ -- list Slack channels.

        1:1 port of
        ``superset_old/reports/api.py:ReportScheduleRestApi.slack_channels``.
        Queries the Slack API for all accessible channels, applies optional
        filtering by name/id (``search_string``), and returns a list of
        ``{id, name}`` dicts.

        Returns an empty list when Slack integration is not configured or
        ``slack_sdk`` is not installed.

        Filter parameters are read from the Rison ``q=`` query parameter
        (``search_string``, ``types``, ``exact_match``, ``force``), matching
        the original ``kwargs.get("rison", {})`` plumbing in
        ``superset_old/reports/api.py:534-586``.
        """
        from litestar.exceptions import HTTPException

        params = rison_params or {}
        search_string = params.get("search_string") or params.get("searchString")
        types = params.get("types", [])
        exact_match = params.get("exact_match", False) or params.get(
            "exactMatch", False
        )
        force = params.get("force", False)

        try:
            channels = _get_slack_channels(
                search_string=search_string,
                types=types,
                exact_match=exact_match,
                force=force,
            )
            return {"result": channels}
        except RuntimeError as exc:
            # 1:1 with superset_old/reports/api.py:588-590: the original only
            # catches SupersetException (which the Slack util wraps SlackApiError
            # into) and maps it to 422; all other exceptions propagate as 500.
            # In liteset, _slack_fetch_all_channels raises RuntimeError for Slack
            # errors — so we catch only RuntimeError here, letting unexpected
            # AttributeError / TypeError / etc. propagate and become HTTP 500.
            logger.error("Error fetching slack channels: %s", str(exc), exc_info=True)
            raise HTTPException(status_code=422, detail=str(exc)) from exc
