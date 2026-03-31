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
from superset.controllers.base import (
    build_rison_query_params,
    extract_ids_required,
    get_info_payload,
    get_related_payload,
    serialize_list_response,
)
from superset.events import event_logger
from superset.exceptions import ObjectNotFoundError
from superset.guards.rbac import require_permission
from superset.params.rison import provide_rison_query
from superset.providers import provide_report_dao
from superset.schemas.report import ReportSchedulePostSchema, ReportSchedulePutSchema
from superset.typing import UserProtocol
from superset.utils import filter_unset

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
    "last_eval_dttm",
    "last_state",
    "last_value",
    "log_retention",
    "grace_period",
    "working_timeout",
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
    ) -> dict[str, Any]:
        """GET /api/v1/report/ — list report schedules with pagination."""
        from sqlalchemy.orm import selectinload

        from superset.models.reports import ReportSchedule

        rison_filters, order_by, page, page_size = build_rison_query_params(
            ReportSchedule, rison_params,
        )
        if not order_by:
            order_by = [ReportSchedule.changed_on.desc()]

        items = await dao.find_all(
            filters=rison_filters or None,
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
        total = await dao.count(filters=rison_filters or None)
        event_logger.log("report.list")
        return serialize_list_response(items, total, _LIST_COLUMNS)

    @get(
        "/{pk:int}",
        guards=[require_permission("can_read", "ReportSchedule")],
    )
    async def get_report(self, pk: int, dao: Any) -> dict[str, Any]:
        """GET /api/v1/report/<pk> — get a single report schedule."""
        report = await dao.find_by_id(pk)
        if not report:
            raise ObjectNotFoundError("ReportSchedule", pk)
        event_logger.log("report.get", object_ref=f"report:{pk}")
        return {
            "id": report.id,
            "result": {
                "name": report.name,
                "type": report.type,
                "description": getattr(report, "description", None),
                "crontab": getattr(report, "crontab", None),
                "timezone": getattr(report, "timezone", "UTC"),
                "active": getattr(report, "active", True),
                "chart_id": getattr(report, "chart_id", None),
                "dashboard_id": getattr(report, "dashboard_id", None),
                "database_id": getattr(report, "database_id", None),
                "sql": getattr(report, "sql", None),
                "validator_type": getattr(report, "validator_type", None),
                "validator_config_json": getattr(
                    report, "validator_config_json", None
                ),
                "log_retention": getattr(report, "log_retention", None),
                "grace_period": getattr(report, "grace_period", None),
                "force_screenshot": getattr(report, "force_screenshot", False),
                "custom_width": getattr(report, "custom_width", None),
                "custom_height": getattr(report, "custom_height", None),
                "last_eval_dttm": (
                    report.last_eval_dttm.isoformat()
                    if getattr(report, "last_eval_dttm", None)
                    else None
                ),
                "last_state": getattr(report, "last_state", None),
            },
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
    ) -> dict[str, Any]:
        """POST /api/v1/report/ — create a report schedule."""
        raw = msgspec.structs.asdict(data)
        # Convert recipient structs to dicts
        if raw.get("recipients"):
            raw["recipients"] = [
                msgspec.structs.asdict(r) if hasattr(r, "__struct_fields__") else r
                for r in raw["recipients"]
            ]
        cmd = CreateReportScheduleCommand(
            dao=dao, data=raw, user_id=current_user.id
        )
        item = await cmd.execute()
        event_logger.log(
            "report.create",
            object_ref=str(item.id),
            user_id=current_user.id,
        )
        return {"id": item.id, "result": {"name": item.name}}

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
            dao=dao, pk=pk, data=raw, user_id=current_user.id
        )
        item = await cmd.execute()
        event_logger.log(
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
    async def delete_report(self, pk: int, dao: Any) -> dict[str, str]:
        """DELETE /api/v1/report/<pk> — delete a single report schedule."""
        cmd = DeleteReportScheduleCommand(dao=dao, pk=pk)
        await cmd.execute()
        event_logger.log("report.delete", object_ref=f"report:{pk}")
        return {"message": "OK"}

    @delete(
        "/",
        guards=[require_permission("can_write", "ReportSchedule")],
        status_code=200,
    )
    async def bulk_delete(
        self,
        dao: Any,
        rison_params: dict[str, Any] | None,
    ) -> dict[str, str]:
        """DELETE /api/v1/report/ — bulk delete report schedules."""
        ids = extract_ids_required(rison_params)
        cmd = BulkDeleteReportScheduleCommand(dao=dao, ids=ids)
        await cmd.execute()
        event_logger.log("report.bulk_delete", extra={"count": len(ids)})
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
    ) -> dict[str, Any]:
        """GET /api/v1/report/related/{column_name} — related values for dropdowns."""
        return await get_related_payload(
            dao=dao,
            column_name=column_name,
            rison_params=rison_params,
            allowed_fields=frozenset(
                {"owners", "created_by", "chart", "dashboard", "database"}
            ),
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
    async def slack_channels(self) -> dict[str, Any]:
        """GET /api/v1/report/slack_channels/ -- list Slack channels.

        Returns an empty list when Slack integration is not configured.
        """
        return {"result": []}
