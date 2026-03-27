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

from superset.controllers.base import extract_pagination, serialize_list_response
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
    path = "/api/v1/report/{report_pk:int}/log"
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
        report_pk: int,
        dao: Any,
        rison_params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """GET /api/v1/report/{report_pk}/log/ -- list logs for a report."""
        page, page_size = extract_pagination(rison_params)
        model_cls = dao.model_cls
        filters = [model_cls.report_schedule_id == report_pk]
        items = await dao.find_all(filters=filters, page=page, page_size=page_size)
        total = await dao.count(filters=filters)
        return serialize_list_response(items, total, _LIST_COLUMNS)

    @get(
        "/{pk:int}",
        guards=[require_permission("can_read", "ReportSchedule")],
    )
    async def get_single(
        self,
        report_pk: int,
        pk: int,
        dao: Any,
    ) -> dict[str, Any]:
        """GET /api/v1/report/{report_pk}/log/{pk} -- get single log entry."""
        item = await dao.find_by_id(pk)
        if item is None:
            raise ObjectNotFoundError("ReportExecutionLog", pk)
        if getattr(item, "report_schedule_id", None) != report_pk:
            raise ObjectNotFoundError("ReportExecutionLog", pk)
        return {"result": item}
