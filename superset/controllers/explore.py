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


def _column_type_generic(sql_type: str) -> int:
    """Map a raw SQL column type string to a ``GenericDataType`` int.

    Mirrors ``superset_old/connectors/sqla/models.py:TableColumn.type_generic``
    which uses ``db_engine_spec.get_column_spec(type).generic_type``. We use a
    simple substring mapping sufficient for Postgres/MySQL/SQLite — the exact
    integer values come from :class:`superset.typing.GenericDataType`:
    ``0 = NUMERIC``, ``1 = STRING``, ``2 = TEMPORAL``, ``3 = BOOLEAN``.
    """
    t = (sql_type or "").upper()
    if "BOOL" in t:
        return 3
    if (
        "TIMESTAMP" in t
        or "DATETIME" in t
        or "DATE" in t
        or "TIME" in t
    ):
        return 2
    if (
        "INT" in t
        or "NUMERIC" in t
        or "DECIMAL" in t
        or "FLOAT" in t
        or "DOUBLE" in t
        or "REAL" in t
        or "BIGINT" in t
        or "SMALLINT" in t
    ):
        return 0
    return 1


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
        chart: Any = None

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
                # Eager-load relationships needed by both ``slice_data``
                # (built here) and ``metadata`` (built further below) so
                # we can fetch the chart exactly once.  ``slice.owners``
                # on the payload is required by the frontend
                # ``SaveModal.canOverwriteSlice`` check — without it
                # ``slice.owners.includes(userId)`` is falsy, the
                # overwrite radio is silently disabled, and tests like
                # ``chart_list::should edit correctly`` and
                # ``link.test::save as overwrite`` fail on
                # ``cy.wait('@update')`` timeout because PUT
                # /api/v1/chart/<id> is never triggered.
                from sqlalchemy.orm import selectinload

                from superset.models.slice import Slice

                _eager_opts: list[Any] = [
                    selectinload(Slice.owners),
                    selectinload(Slice.created_by),  # type: ignore[attr-defined]
                    selectinload(Slice.changed_by),  # type: ignore[attr-defined]
                ]
                # ``Slice.dashboards`` is created via ``backref`` from
                # ``Dashboard.slices`` so it's only resolvable at
                # runtime; use ``getattr`` to keep mypy quiet.
                _dashboards_rel = getattr(Slice, "dashboards", None)
                if _dashboards_rel is not None:
                    _eager_opts.append(selectinload(_dashboards_rel))

                chart = await chart_dao.find_by_id_with_options(
                    slice_id,
                    options=_eager_opts,
                )
                if chart is not None:
                    from datetime import datetime, timezone

                    import humanize

                    owners_list = [
                        int(o.id)
                        for o in (getattr(chart, "owners", None) or [])
                        if getattr(o, "id", None) is not None
                    ]
                    changed_on_dt = getattr(chart, "changed_on", None)
                    changed_on_iso = (
                        changed_on_dt.isoformat() if changed_on_dt else None
                    )
                    changed_on_humanized = ""
                    if changed_on_dt:
                        try:
                            now = datetime.now(
                                changed_on_dt.tzinfo or timezone.utc
                            )
                            if changed_on_dt.tzinfo is None:
                                now = datetime.now()
                            changed_on_humanized = humanize.naturaltime(
                                now - changed_on_dt
                            )
                        except Exception:  # noqa: BLE001
                            changed_on_humanized = ""
                    desc = getattr(chart, "description", None) or ""
                    chart_cache_timeout = getattr(chart, "cache_timeout", None)
                    # ``Slice.form_data`` property applies update_time_range
                    # which migrates since/until → time_range
                    # (superset_old/models/slice.py:287, legacy.py:22-42).
                    defaults = chart.form_data
                    # ``slice_data`` mirrors ``Slice.data`` exactly
                    # (superset_old/models/slice.py:219-248).
                    slice_data = {
                        "cache_timeout": chart_cache_timeout,
                        "changed_on": changed_on_iso,
                        "changed_on_humanized": changed_on_humanized,
                        "datasource": getattr(chart, "datasource_name", None),
                        "description": desc or None,
                        "description_markeddown": (
                            f"<p>{desc}</p>" if desc else ""
                        ),
                        "edit_url": f"/chart/edit/{chart.id}",
                        "form_data": defaults,
                        # ``Slice.query_context`` is stored as a raw JSON
                        # string on the chart row; the original returns the
                        # string unparsed (models/slice.py:239).
                        "query_context": getattr(chart, "query_context", None),
                        "modified": (
                            f'<span class="no-wrap">{changed_on_humanized}</span>'
                            if changed_on_humanized
                            else ""
                        ),
                        "owners": owners_list,
                        "slice_id": chart.id,
                        "slice_name": getattr(chart, "slice_name", ""),
                        "slice_url": (
                            f"/explore/?slice_id={chart.id}"
                            f"&form_data=%7B%22slice_id%22%3A%20{chart.id}%7D"
                        ),
                        "certified_by": getattr(chart, "certified_by", None),
                        "certification_details": getattr(
                            chart, "certification_details", None
                        ),
                        "is_managed_externally": bool(
                            getattr(chart, "is_managed_externally", False)
                        ),
                        # ``viz_type`` is not in the original ``Slice.data``
                        # payload but a number of frontend callsites
                        # (``SliceHeader``, ``ChartContainer``) read
                        # ``slice.viz_type`` directly, so we keep it.
                        "viz_type": getattr(chart, "viz_type", ""),
                    }
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
                        selectinload(SqlaTable.owners),
                    ],
                )
                if results:
                    dataset = results[0]
                    # Build dataset_data matching original datasource.data structure
                    db_obj = getattr(dataset, "database", None)
                    dataset_data = {
                        "id": dataset.id,
                        "type": ds_type,
                        "uid": f"{dataset.id}__{ds_type}",
                        "name": getattr(dataset, "table_name", "")
                        or getattr(dataset, "name", ""),
                        "datasource_name": getattr(dataset, "table_name", ""),
                        "table_name": getattr(dataset, "table_name", ""),
                        "database": {
                            "id": db_obj.id if db_obj else 0,
                            "name": (
                                getattr(db_obj, "database_name", "")
                                if db_obj
                                else ""
                            ),
                            "backend": (
                                getattr(db_obj, "backend", "") if db_obj else ""
                            ),
                            "allows_subquery": (
                                getattr(db_obj, "allows_subquery", True)
                                if db_obj
                                else True
                            ),
                            "allow_multi_catalog": (
                                getattr(db_obj, "allow_multi_catalog", False)
                                if db_obj
                                else False
                            ),
                            "explore_database_id": (
                                getattr(db_obj, "explore_database_id", 0)
                                if db_obj
                                else 0
                            ),
                        },
                        "schema": getattr(dataset, "schema", None),
                        "catalog": getattr(dataset, "catalog", None),
                        "sql": getattr(dataset, "sql", None),
                        "is_sqllab_view": getattr(
                            dataset, "is_sqllab_view", False
                        ),
                        "description": getattr(dataset, "description", None),
                        "default_endpoint": getattr(
                            dataset, "default_endpoint", None
                        ),
                        "cache_timeout": getattr(dataset, "cache_timeout", None),
                        "offset": getattr(dataset, "offset", 0),
                        "fetch_values_predicate": getattr(
                            dataset, "fetch_values_predicate", None
                        ),
                        "template_params": getattr(
                            dataset, "template_params", None
                        ),
                        "normalize_columns": getattr(
                            dataset, "normalize_columns", False
                        ),
                        "always_filter_main_dttm": getattr(
                            dataset, "always_filter_main_dttm", False
                        ),
                        "is_managed_externally": getattr(
                            dataset, "is_managed_externally", False
                        ),
                        "extra": getattr(dataset, "extra", None),
                        "folders": getattr(dataset, "folders", None),
                        "params": getattr(dataset, "params", None),
                        "perm": getattr(dataset, "perm", None) or "",
                        "edit_url": f"/tablemodelview/edit/{dataset.id}",
                        "select_star": None,
                        "health_check_message": None,
                        "column_formats": {},
                        "currency_formats": {},
                        "filter_select": bool(
                            getattr(dataset, "filter_select_enabled", True)
                        ),
                        "verbose_map": {
                            "__timestamp": "Time",
                            **{
                                getattr(m, "metric_name", ""): (
                                    getattr(m, "verbose_name", None)
                                    or getattr(m, "metric_name", "")
                                )
                                for m in (getattr(dataset, "metrics", None) or [])
                            },
                            **{
                                getattr(c, "column_name", ""): (
                                    getattr(c, "verbose_name", None)
                                    or getattr(c, "column_name", "")
                                )
                                for c in (getattr(dataset, "columns", None) or [])
                            },
                        },
                        # ``owners`` mirrors ``SqlaTable.owners_data`` which
                        # yields ``{first_name, last_name, username, id}``.
                        # We additionally keep ``value`` because the
                        # frontend's ``exploreReducer`` and
                        # ``DatasourceEditor`` both read ``owner.value``
                        # without a fallback — dropping it would break the
                        # Edit-dataset modal's ``Cannot read properties of
                        # undefined (reading 'map')`` failure mode.
                        "owners": [
                            {
                                "first_name": getattr(o, "first_name", "") or "",
                                "last_name": getattr(o, "last_name", "") or "",
                                "username": getattr(o, "username", "") or "",
                                "id": o.id,
                                "value": o.id,
                            }
                            for o in (getattr(dataset, "owners", None) or [])
                        ],
                        "columns": [
                            {
                                "id": getattr(c, "id", None),
                                "uuid": str(getattr(c, "uuid", "") or "") or None,
                                "column_name": getattr(c, "column_name", ""),
                                "type": getattr(c, "type", ""),
                                "type_generic": _column_type_generic(
                                    getattr(c, "type", "") or ""
                                ),
                                "is_dttm": getattr(c, "is_dttm", False),
                                "filterable": getattr(c, "filterable", True),
                                "groupby": getattr(c, "groupby", True),
                                "verbose_name": getattr(c, "verbose_name", None),
                                "description": getattr(c, "description", None),
                                "expression": getattr(c, "expression", None),
                                "python_date_format": getattr(
                                    c, "python_date_format", None
                                ),
                                "advanced_data_type": getattr(
                                    c, "advanced_data_type", None
                                ),
                                "certified_by": getattr(c, "certified_by", None),
                                "certification_details": getattr(
                                    c, "certification_details", None
                                ),
                                "is_certified": bool(
                                    getattr(c, "certified_by", None)
                                ),
                                "warning_markdown": getattr(
                                    c, "warning_markdown", None
                                ),
                            }
                            for c in (getattr(dataset, "columns", None) or [])
                        ],
                        "metrics": [
                            {
                                "id": getattr(m, "id", None),
                                "uuid": str(getattr(m, "uuid", "") or "") or None,
                                "metric_name": getattr(m, "metric_name", ""),
                                "verbose_name": getattr(m, "verbose_name", None),
                                "expression": getattr(m, "expression", ""),
                                "description": getattr(m, "description", None),
                                "d3format": getattr(m, "d3format", None),
                                "currency": getattr(m, "currency", None),
                                "warning_text": getattr(m, "warning_text", None),
                                "warning_markdown": getattr(
                                    m, "warning_markdown", None
                                ),
                                "certified_by": getattr(m, "certified_by", None),
                                "certification_details": getattr(
                                    m, "certification_details", None
                                ),
                                "is_certified": bool(
                                    getattr(m, "certified_by", None)
                                ),
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
        # (superset_old/commands/explore/get.py:156-169).  Reuses the
        # ``chart`` loaded above with eager-loaded ``owners``,
        # ``dashboards``, ``created_by``, ``changed_by`` — no extra
        # round-trips or direct session access here.
        metadata: dict[str, Any] | None = None
        if chart is not None:
            from datetime import datetime

            import humanize

            def _humanize_dt(dt: Any) -> str:
                if dt and hasattr(dt, "isoformat"):
                    return humanize.naturaltime(datetime.now() - dt)
                return ""

            def _full_name(obj: Any) -> str:
                if obj is None:
                    return ""
                return (
                    f"{getattr(obj, 'first_name', '')} "
                    f"{getattr(obj, 'last_name', '')}"
                ).strip()

            dashboards_rel = getattr(chart, "dashboards", None) or []
            metadata = {
                "created_on_humanized": _humanize_dt(
                    getattr(chart, "created_on", None)
                ),
                "changed_on_humanized": _humanize_dt(
                    getattr(chart, "changed_on", None)
                ),
                "owners": [
                    _full_name(o) for o in (getattr(chart, "owners", None) or [])
                ],
                "dashboards": [
                    {
                        "id": d.id,
                        "dashboard_title": getattr(d, "dashboard_title", ""),
                    }
                    for d in dashboards_rel
                ],
            }
            created_by = getattr(chart, "created_by", None)
            if created_by:
                metadata["created_by"] = _full_name(created_by)
            changed_by = getattr(chart, "changed_by", None)
            if changed_by:
                metadata["changed_by"] = _full_name(changed_by)

        return {
            "result": {
                "dataset": dataset_data,
                "form_data": form_data,
                "slice": slice_data,
                "message": message,
                "metadata": metadata,
            }
        }
