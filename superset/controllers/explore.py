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
"""Explore controller — assemble form data from multiple sources."""

from __future__ import annotations

import json
from typing import Any

from litestar import Controller, get
from litestar.connection import Request
from litestar.di import Provide

from superset.guards.rbac import require_permission
from superset.providers import provide_chart_dao, provide_dataset_dao, provide_kv_dao
from superset.typing import ChartDAOProtocol, DatasetDAOProtocol, KeyValueDAOProtocol


class ExploreController(Controller):
    path = "/api/v1/explore"
    tags = ["Explore"]
    dependencies = {
        "chart_dao": Provide(provide_chart_dao, sync_to_thread=False),
        "dataset_dao": Provide(provide_dataset_dao, sync_to_thread=False),
        "kv_dao": Provide(provide_kv_dao, sync_to_thread=False),
    }

    @get(
        "/",
        guards=[require_permission("can_read", "Explore")],
    )
    async def get_explore(
        self,
        request: Request[Any, Any, Any],
        chart_dao: ChartDAOProtocol,
        dataset_dao: DatasetDAOProtocol,
        kv_dao: KeyValueDAOProtocol,
    ) -> dict[str, Any]:
        """GET /api/v1/explore/ — assemble form_data from params.

        Query params: form_data_key, slice_id, dataset_id, dataset_type
        """
        form_data_key = request.query_params.get("form_data_key")
        slice_id_raw = request.query_params.get("slice_id")
        dataset_id_raw = request.query_params.get("dataset_id")
        dataset_type = request.query_params.get("dataset_type", "table")

        form_data: dict[str, Any] = {}
        message = ""
        slice_data: dict[str, Any] | None = None
        dataset_data: dict[str, Any] | None = None

        # 1. Load form_data from permalink key
        if form_data_key:
            raw = await kv_dao.get_value(
                resource="explore_form_data",
                resource_id=0,
                key=form_data_key,
            )
            if raw:
                try:
                    entry = json.loads(raw)
                    if isinstance(entry, dict) and "value" in entry:
                        form_data = (
                            json.loads(entry["value"])
                            if isinstance(entry["value"], str)
                            else entry["value"]
                        )
                    else:
                        form_data = entry
                except (json.JSONDecodeError, TypeError):
                    pass

        # 2. Load slice defaults
        if slice_id_raw:
            try:
                slice_id = int(slice_id_raw)
                chart = await chart_dao.find_by_id(slice_id)
                if chart is not None:
                    slice_data = {
                        "slice_id": chart.id,
                        "slice_name": getattr(chart, "slice_name", ""),
                        "viz_type": getattr(chart, "viz_type", ""),
                    }
                    # Merge chart params into form_data (chart defaults as base)
                    chart_params = getattr(chart, "params", "{}")
                    if chart_params:
                        try:
                            defaults = json.loads(chart_params)
                            merged = {**defaults, **form_data}
                            form_data = merged
                        except (json.JSONDecodeError, TypeError):
                            pass
                else:
                    message = f"Chart {slice_id} not found"
            except (ValueError, TypeError):
                pass

        # 3. Load dataset defaults
        if dataset_id_raw:
            try:
                dataset_id = int(dataset_id_raw)
                dataset = await dataset_dao.find_by_id(dataset_id)
                if dataset is not None:
                    dataset_data = {
                        "id": dataset.id,
                        "type": dataset_type,
                        "name": getattr(dataset, "table_name", "")
                        or getattr(dataset, "name", ""),
                    }
            except (ValueError, TypeError):
                pass

        return {
            "result": {
                "dataset": dataset_data,
                "form_data": form_data,
                "slice": slice_data,
                "message": message,
            }
        }
