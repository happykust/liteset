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
"""Report execution log controller -- read-only log access."""

from __future__ import annotations

from typing import Any

from litestar import Controller, get
from litestar.di import Provide

from superset.controllers.base import (
    build_rison_query_params,
    serialize_list_response,
)
from superset.exceptions import ObjectNotFoundError
from superset.guards.rbac import require_permission
from superset.params.rison import provide_rison_query
from superset.providers import provide_report_execution_log_dao

_LIST_COLUMNS = [
    "id",
    "scheduled_dttm",
    "end_dttm",
    "start_dttm",
    "value",
    "value_row_json",
    "state",
    "error_message",
    "uuid",
]


class ReportExecutionLogController(Controller):
    path = "/api/v1/report/{pk:int}/log"
    tags = ["Report Execution Log"]
    dependencies = {
        "dao": Provide(provide_report_execution_log_dao, sync_to_thread=False),
        "rison_params": Provide(provide_rison_query),
    }

    @get(
        "/",
        guards=[require_permission("can_read", "ReportSchedule")],
    )
    async def get_list(
        self,
        pk: int,
        dao: Any,
        rison_params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """GET /api/v1/report/{pk}/log/ -- list logs for a report."""
        model_cls = dao.model_cls
        # 1:1 with the original ``ReportScheduleLogRestApi`` (FAB
        # ``get_list_headless``): honor the user-supplied rison ``filters`` and
        # ``order_column``/``order_direction`` (order_columns: state, value,
        # error_message, end_dttm, start_dttm, scheduled_dttm) in addition to
        # the mandatory ``report_schedule_id == pk`` scope.
        rison_filters, order_by, page, page_size = build_rison_query_params(
            model_cls, rison_params
        )
        filters = [model_cls.report_schedule_id == pk] + (rison_filters or [])
        items = await dao.find_all(
            filters=filters,
            page=page,
            page_size=page_size,
            order_by=order_by,
        )
        total = await dao.count(filters=filters)
        return serialize_list_response(
            items,
            total,
            _LIST_COLUMNS,
            list_title="List Report Log",
        )

    @get(
        "/{log_id:int}",
        guards=[require_permission("can_read", "ReportSchedule")],
    )
    async def get_single(
        self,
        pk: int,
        log_id: int,
        dao: Any,
    ) -> dict[str, Any]:
        """GET /api/v1/report/{pk}/log/{log_id} -- get single log entry."""
        item = await dao.find_by_id(log_id)
        if item is None:
            raise ObjectNotFoundError("ReportExecutionLog", log_id)
        if getattr(item, "report_schedule_id", None) != pk:
            raise ObjectNotFoundError("ReportExecutionLog", log_id)
        return {"result": item}
