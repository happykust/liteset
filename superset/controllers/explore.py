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
    async def get_explore(  # noqa: C901
        self,
        request: Request[Any, Any, Any],
        chart_dao: ChartDAOProtocol,
        dataset_dao: DatasetDAOProtocol,
        kv_dao: KeyValueDAOProtocol,
    ) -> dict[str, Any]:
        """GET /api/v1/explore/ — assemble form_data from params.

        Query params: form_data_key, permalink_key, slice_id,
                      datasource_id, datasource_type
        """
        form_data_key = request.query_params.get("form_data_key")
        permalink_key = request.query_params.get("permalink_key")
        slice_id_raw = request.query_params.get("slice_id")
        datasource_id_raw = request.query_params.get("datasource_id")
        datasource_type = request.query_params.get("datasource_type", "table")

        form_data: dict[str, Any] = {}
        message = ""
        slice_data: dict[str, Any] | None = None
        dataset_data: dict[str, Any] | None = None

        # 1a. Load form_data from permalink key (metadata DB)
        if permalink_key:
            raw = await kv_dao.get_value(
                resource="explore_permalink",
                resource_id=0,
                key=permalink_key,
            )
            if raw:
                try:
                    entry = json.loads(raw)
                    if isinstance(entry, dict):
                        # Permalink stores formData/form_data directly
                        fd = entry.get("formData") or entry.get("form_data") or {}
                        form_data = json.loads(fd) if isinstance(fd, str) else fd
                except (json.JSONDecodeError, TypeError):
                    pass

        # 1b. Load form_data from temporary cache key (overrides permalink)
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

        # 3. Resolve datasource from form_data or query params (original logic)
        ds_id: int | None = None
        ds_type: str = datasource_type or "table"

        # If datasource_id is in query params, use it directly
        if datasource_id_raw:
            try:
                ds_id = int(datasource_id_raw)
            except (ValueError, TypeError):
                pass

        # If not in query params, extract from form_data["datasource"] = "21__table"
        if ds_id is None and "datasource" in form_data:
            ds_str = str(form_data["datasource"])
            if "__" in ds_str:
                parts = ds_str.split("__")
                try:
                    ds_id = int(parts[0])
                    ds_type = parts[1] if len(parts) > 1 else "table"
                except (ValueError, IndexError):
                    pass

        # Load the dataset
        if ds_id is not None:
            try:
                from sqlalchemy.orm import selectinload

                from superset.models.connectors import SqlaTable

                results = await dataset_dao.find_all(
                    filters=[SqlaTable.id == ds_id],
                    page=0,
                    page_size=1,
                    options=[
                        selectinload(SqlaTable.database),
                        selectinload(SqlaTable.columns),
                        selectinload(SqlaTable.metrics),
                    ],
                )
                if results:
                    dataset = results[0]
                    # Build dataset_data matching original datasource.data structure
                    db_obj = getattr(dataset, "database", None)
                    dataset_data = {
                        "id": dataset.id,
                        "type": ds_type,
                        "name": getattr(dataset, "table_name", "")
                        or getattr(dataset, "name", ""),
                        "database": {
                            "id": db_obj.id if db_obj else 0,
                            "backend": (
                                getattr(db_obj, "backend", "") if db_obj else ""
                            ),
                        },
                        "schema": getattr(dataset, "schema", None),
                        "columns": [
                            {
                                "column_name": getattr(c, "column_name", ""),
                                "type": getattr(c, "type", ""),
                                "is_dttm": getattr(c, "is_dttm", False),
                                "filterable": getattr(c, "filterable", True),
                                "groupby": getattr(c, "groupby", True),
                                "verbose_name": getattr(c, "verbose_name", None),
                                "description": getattr(c, "description", None),
                                "expression": getattr(c, "expression", None),
                            }
                            for c in (getattr(dataset, "columns", None) or [])
                        ],
                        "metrics": [
                            {
                                "metric_name": getattr(m, "metric_name", ""),
                                "verbose_name": getattr(m, "verbose_name", None),
                                "expression": getattr(m, "expression", ""),
                                "description": getattr(m, "description", None),
                            }
                            for m in (getattr(dataset, "metrics", None) or [])
                        ],
                        "main_dttm_col": getattr(dataset, "main_dttm_col", None),
                        "filter_select_enabled": getattr(
                            dataset, "filter_select_enabled", True
                        ),
                    }
            except Exception:  # noqa: BLE001, S110
                pass  # Dataset metadata is optional, continue without it

        return {
            "result": {
                "dataset": dataset_data,
                "form_data": form_data,
                "slice": slice_data,
                "message": message,
            }
        }
