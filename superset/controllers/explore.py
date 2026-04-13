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


# Matches original ``superset_old/db_engine_specs/base.py:builtin_time_grains``
# — the label shown in the ``Time grain`` dropdown for each ISO-8601 duration
# returned by the engine spec's ``get_time_grain_expressions()``.
_TIME_GRAIN_LABELS: dict[str | None, str] = {
    None: "Original value",
    "PT1S": "Second",
    "PT5S": "5 second",
    "PT30S": "30 second",
    "PT1M": "Minute",
    "PT5M": "5 minute",
    "PT10M": "10 minute",
    "PT15M": "15 minute",
    "PT30M": "30 minute",
    "PT0.5H": "Half hour",
    "PT1H": "Hour",
    "PT6H": "6 hour",
    "P1D": "Day",
    "P1W": "Week",
    "P1M": "Month",
    "P3M": "Quarter",
    "P0.25Y": "Quarter",
    "P1Y": "Year",
    "1969-12-28T00:00:00Z/P1W": "Week starting Sunday",
    "1969-12-29T00:00:00Z/P1W": "Week starting Monday",
    "P1W/1970-01-03T00:00:00Z": "Week ending Saturday",
    "P1W/1970-01-04T00:00:00Z": "Week ending Sunday",
}


def _build_time_grain_sqla_choices(database: Any) -> list[list[Any]]:
    """Build the ``[(duration, label)]`` list for the time-grain control.

    Mirrors ``SqlaTable.time_grain_sqla`` in the original Superset, which
    iterates ``database.grains()`` and emits ``(duration, name)`` pairs.
    The frontend's SelectControl requires the first element of each
    choice to match the saved form_data value exactly (ISO-8601
    duration string); otherwise it drops the stored grain on hydration
    and the chart query omits time truncation.
    """
    if database is None:
        return []
    try:
        spec = database.db_engine_spec
        grain_exprs = spec.get_time_grain_expressions()
    except Exception:  # noqa: BLE001
        return []

    choices: list[list[Any]] = []
    for duration in grain_exprs:
        label = _TIME_GRAIN_LABELS.get(duration, duration or "Original value")
        choices.append([duration, label])
    return choices


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
                    # Merge chart params into form_data (chart defaults as base).
                    # Mirrors original Slice.form_data property which always
                    # includes datasource, slice_id, viz_type from the chart
                    # object — not just from stored params JSON.
                    chart_params = getattr(chart, "params", "{}")
                    if chart_params:
                        try:
                            defaults = json.loads(chart_params)
                        except (json.JSONDecodeError, TypeError):
                            defaults = {}
                    else:
                        defaults = {}
                    # Inject chart-level fields matching Slice.form_data property
                    chart_ds_id = getattr(chart, "datasource_id", None)
                    chart_ds_type = getattr(chart, "datasource_type", "table") or "table"
                    defaults.update(
                        {
                            "slice_id": chart.id,
                            "viz_type": getattr(chart, "viz_type", ""),
                            "datasource": (
                                f"{chart_ds_id}__{chart_ds_type}"
                                if chart_ds_id
                                else defaults.get("datasource", "")
                            ),
                        }
                    )
                    # Original Slice.form_data also injects cache_timeout
                    # (superset_old/models/slice.py:285-286)
                    chart_cache_timeout = getattr(chart, "cache_timeout", None)
                    if chart_cache_timeout:
                        defaults["cache_timeout"] = chart_cache_timeout
                    merged = {**defaults, **form_data}
                    form_data = merged
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

        # Fallback: if form_data doesn't contain datasource, use chart's
        # own datasource_id.  Original Superset always has datasource in
        # the chart params, but migrated charts may not.
        if ds_id is None and slice_id_raw:
            try:
                chart = await chart_dao.find_by_id(int(slice_id_raw))
                if chart is not None:
                    chart_ds_id = getattr(chart, "datasource_id", None)
                    if chart_ds_id:
                        ds_id = int(chart_ds_id)
                        ds_type = getattr(chart, "datasource_type", "table") or "table"
                        # Also inject into form_data so frontend gets it
                        form_data["datasource"] = f"{ds_id}__{ds_type}"
            except (ValueError, TypeError):
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
                        # ``time_grain_sqla`` drives the explore
                        # ``time_grain_sqla`` SelectControl.  Original
                        # ``SqlaTable.time_grain_sqla`` returns
                        # ``[(duration, label)]`` pairs built from
                        # ``database.db_engine_spec.get_time_grains()``.
                        # Without this list the control cannot match
                        # saved form_data values like ``"P3M"`` against
                        # its choices and silently drops the grain,
                        # resulting in missing time truncation in chart
                        # queries (breaks table viz ``2008 Q1`` etc.).
                        "time_grain_sqla": _build_time_grain_sqla_choices(
                            getattr(dataset, "database", None)
                        ),
                        # ``granularity_sqla`` is the list of available
                        # datetime columns for the "Time column" control
                        # (``choicify(dttm_cols)`` in the original).
                        "granularity_sqla": [
                            [
                                getattr(c, "column_name", ""),
                                getattr(c, "column_name", ""),
                            ]
                            for c in (getattr(dataset, "columns", None) or [])
                            if getattr(c, "is_dttm", False)
                            and getattr(c, "column_name", None)
                        ],
                        # ``order_by_choices`` drives the table viz
                        # ``order_by_cols`` SelectControl.  It MUST be
                        # the json-encoded [column, asc] shape produced
                        # by the original ``SqlaTable.order_by_choices``
                        # property — otherwise the control can't match
                        # its saved value and silently drops the
                        # selection, resulting in empty ``orderby`` in
                        # subsequent chart queries.
                        "order_by_choices": [
                            *(
                                pair
                                for c in (getattr(dataset, "columns", None) or [])
                                for pair in (
                                    [
                                        json.dumps([c.column_name, True]),
                                        f"{c.column_name} [asc]",
                                    ],
                                    [
                                        json.dumps([c.column_name, False]),
                                        f"{c.column_name} [desc]",
                                    ],
                                )
                                if getattr(c, "column_name", None)
                            ),
                        ],
                    }
            except Exception:  # noqa: BLE001, S110
                pass  # Dataset metadata is optional, continue without it

        # Build metadata matching original GetExploreCommand
        # (superset_old/commands/explore/get.py:156-169)
        metadata: dict[str, Any] | None = None
        if slice_id_raw:
            try:
                chart = await chart_dao.find_by_id(int(slice_id_raw))
                if chart is not None:
                    from datetime import datetime

                    import humanize

                    def _humanize_dt(dt: Any) -> str:
                        if dt and hasattr(dt, "isoformat"):
                            return humanize.naturaltime(datetime.now() - dt)
                        return ""

                    metadata = {
                        "created_on_humanized": _humanize_dt(
                            getattr(chart, "created_on", None)
                        ),
                        "changed_on_humanized": _humanize_dt(
                            getattr(chart, "changed_on", None)
                        ),
                        "owners": [],
                        "dashboards": [],
                    }
                    # Load owners/dashboards/changed_by/created_by
                    # (best-effort — may MissingGreenlet on lazy load)
                    try:
                        from sqlalchemy.orm import selectinload

                        from superset.models.slice import Slice

                        from sqlalchemy import select as sa_select

                        refreshed = await chart_dao.session.execute(
                            sa_select(Slice)
                            .where(Slice.id == chart.id)
                            .options(
                                selectinload(Slice.owners),
                                selectinload(Slice.dashboards),
                            )
                        )
                        slc = refreshed.scalars().one_or_none()
                        if slc:
                            metadata["owners"] = [
                                f"{getattr(o, 'first_name', '')} {getattr(o, 'last_name', '')}".strip()
                                for o in (slc.owners or [])
                            ]
                            metadata["dashboards"] = [
                                {
                                    "id": d.id,
                                    "dashboard_title": getattr(
                                        d, "dashboard_title", ""
                                    ),
                                }
                                for d in (slc.dashboards or [])
                            ]
                            cb = getattr(slc, "created_by", None)
                            if cb:
                                metadata["created_by"] = (
                                    f"{getattr(cb, 'first_name', '')} {getattr(cb, 'last_name', '')}".strip()
                                )
                            chb = getattr(slc, "changed_by", None)
                            if chb:
                                metadata["changed_by"] = (
                                    f"{getattr(chb, 'first_name', '')} {getattr(chb, 'last_name', '')}".strip()
                                )
                    except Exception:  # noqa: BLE001, S110
                        pass
            except (ValueError, TypeError):
                pass

        return {
            "result": {
                "dataset": dataset_data,
                "form_data": form_data,
                "slice": slice_data,
                "message": message,
                "metadata": metadata,
            }
        }
