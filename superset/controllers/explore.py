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

from litestar import Controller, get, Response
from litestar.connection import Request
from litestar.di import Provide
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from superset.exceptions import (
    ObjectNotFoundError,
    SupersetException,
    SupersetSecurityException,
)
from superset.guards.rbac import require_permission
from superset.providers import (
    provide_chart_dao,
    provide_dataset_dao,
    provide_kv_dao,
    provide_query_dao,
)
from superset.typing import (
    ChartDAOProtocol,
    DatasetDAOProtocol,
    KeyValueDAOProtocol,
    QueryDAOProtocol,
    SecurityManagerProtocol,
    UserProtocol,
)

# Labels shown in the ``Time grain`` dropdown for each ISO-8601 duration
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

    Uses a simple substring mapping sufficient for Postgres/MySQL/SQLite.
    Integer values from :class:`superset.typing.GenericDataType`:
    ``0 = NUMERIC``, ``1 = STRING``, ``2 = TEMPORAL``, ``3 = BOOLEAN``.
    """
    t = (sql_type or "").upper()
    if "BOOL" in t:
        return 3
    if "TIMESTAMP" in t or "DATETIME" in t or "DATE" in t or "TIME" in t:
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

    The frontend's SelectControl requires the first element of each choice to
    match the saved form_data value exactly (ISO-8601 duration string);
    otherwise it drops the stored grain on hydration and the chart query
    omits time truncation.
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
        "query_dao": Provide(provide_query_dao, sync_to_thread=False),
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
        query_dao: QueryDAOProtocol,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
        session: AsyncSession,
    ) -> dict[str, Any] | Response[Any]:
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

        # 1a. Load form_data from permalink key (metadata DB).
        #
        # An explore permalink_key is a *hashids-encoded* string (NOT a UUID):
        # the create path stores the payload under EXPLORE_PERMALINK keyed by an
        # auto-generated *integer* id, then encodes that int id into the URL
        # string via ``encode_permalink_key`` with a per-install salt. Resolution
        # MUST mirror the write: decode the hashid → int id (with the same salt),
        # look the entry up by its integer key, and read
        # ``value["state"]["formData"]`` / ``value["state"]["urlParams"]``.
        if permalink_key:
            from superset.db.daos.key_value import AsyncKeyValueDAO
            from superset.key_value.shared_entries import get_permalink_salt
            from superset.key_value.types import KeyValueResource, SharedKey
            from superset.key_value.utils import decode_permalink_id

            salt = await get_permalink_salt(session, SharedKey.EXPLORE_PERMALINK_SALT)
            try:
                entry_id = decode_permalink_id(permalink_key, salt=salt)
            except Exception as ex:  # noqa: BLE001
                # Bad/garbled key → not a valid permalink → 404.
                raise ObjectNotFoundError("ExplorePermalink", permalink_key) from ex

            value = await AsyncKeyValueDAO(session).get_value_by_key(
                resource=KeyValueResource.EXPLORE_PERMALINK.value,
                key=entry_id,
            )
            if isinstance(value, dict):
                # The permalink references a datasource AND (usually) a chart;
                # a user with datasource access but no access to the chart must
                # not read its full form_data via someone else's permalink.
                from superset.commands.explore_form_data.utils import check_access

                _pl_chart_id: int | None = value.get("chartId")
                _pl_datasource_id: int = (
                    value.get("datasourceId") or value.get("datasetId") or 0
                )
                _pl_datasource_type: str = str(value.get("datasourceType") or "table")
                await check_access(
                    datasource_id=_pl_datasource_id,
                    chart_id=_pl_chart_id,
                    datasource_type=_pl_datasource_type,
                    dataset_dao=dataset_dao,
                    query_dao=query_dao,
                    chart_dao=chart_dao,
                    security_manager=security_manager,
                    user=current_user,
                )
                state = value.get("state") or {}
                fd = state.get("formData") or {}
                form_data = json.loads(fd) if isinstance(fd, str) else fd
                url_params = state.get("urlParams")
                if url_params:
                    form_data["url_params"] = dict(url_params)
            else:
                # Permalink key not found / expired → 404.
                raise ObjectNotFoundError("ExplorePermalink", permalink_key)

        # 1b. Load form_data from temporary cache key — elif, NOT if.
        # When permalink_key is provided, form_data_key is ignored entirely.
        elif form_data_key:
            from superset.controllers.explore_form_data import _form_data_cache

            entry = await _form_data_cache().get(form_data_key)
            if entry is not None:
                try:
                    if isinstance(entry, str):
                        entry = json.loads(entry)
                    if isinstance(entry, dict):
                        # Access check before returning ``state["form_data"]``.
                        # Prevents any ``can_read Explore`` user from reading
                        # form_data cached for an inaccessible datasource/chart
                        # by supplying a foreign form_data_key.
                        from superset.commands.explore_form_data.utils import (
                            check_access,
                        )

                        _datasource_id: int = entry.get("datasource_id") or 0
                        _datasource_type: str = entry.get("datasource_type") or "table"
                        _chart_id: int | None = entry.get("chart_id")
                        await check_access(
                            datasource_id=_datasource_id,
                            chart_id=_chart_id,
                            datasource_type=_datasource_type,
                            dataset_dao=dataset_dao,
                            query_dao=query_dao,
                            chart_dao=chart_dao,
                            security_manager=security_manager,
                            user=current_user,
                        )
                        # Extract the actual form_data from the envelope.
                        # The KV store writes the envelope as:
                        # {"owner": ..., "datasource_id": ...,
                        #  "datasource_type": ..., "chart_id": ...,
                        #  "form_data": "<json_str>"}
                        raw_fd = entry.get("form_data")
                        if raw_fd is not None:
                            form_data = (
                                json.loads(raw_fd)
                                if isinstance(raw_fd, str)
                                else raw_fd
                            )
                    else:
                        form_data = entry
                except (json.JSONDecodeError, TypeError):
                    pass

        # Cache-miss fallback: when form_data_key was provided but the cache
        # returned empty (expired), set an informational message and populate
        # form_data with the fallback slice_id or datasource.
        if not form_data:
            if slice_id_raw:
                try:
                    form_data["slice_id"] = int(slice_id_raw)
                except (ValueError, TypeError):
                    pass
                if form_data_key:
                    message = (
                        "Form data not found in cache, reverting to chart metadata."
                    )
            elif datasource_id_raw:
                form_data["datasource"] = (
                    f"{datasource_id_raw}__{datasource_type or 'table'}"
                )
                if form_data_key:
                    message = (
                        "Form data not found in cache, reverting to dataset metadata."
                    )

        # 1c. Merge ?form_data=<json> URL query parameter. Must run AFTER the
        # initial load from permalink/form_data_key but BEFORE slice defaults
        # are merged so that the {**defaults, **form_data} merge makes the arg
        # win.
        _args_form_data = request.query_params.get("form_data")
        if _args_form_data:
            try:
                _parsed_args = json.loads(_args_form_data)
                if isinstance(_parsed_args, dict):
                    form_data.update(_parsed_args)
            except (TypeError, ValueError):
                pass

        # form_data's embedded slice_id wins over the URL param (e.g. a
        # permalink that carries slice_id=42 while the URL also has ?slice_id=99
        # should load chart 42).
        _fd_slice_id = form_data.get("slice_id")
        if _fd_slice_id is not None:
            try:
                slice_id_raw = str(int(_fd_slice_id))
            except (ValueError, TypeError):
                pass

        # Filter REJECTED_FORM_DATA_KEYS BEFORE the slice-defaults merge.
        # Only strips JS keys from the request-submitted form_data (permalink /
        # form_data_key / URL args). The slice's own stored form_data is NOT
        # filtered so a chart saved with JS keys while the feature was enabled
        # continues to render those keys after the flag is disabled.
        from superset.utils.feature_flags import feature_flag_manager

        if not feature_flag_manager.is_feature_enabled("ENABLE_JAVASCRIPT_CONTROLS"):
            _rejected = {"js_tooltip", "js_onclick_href", "js_data_mutator"}
            form_data = {k: v for k, v in form_data.items() if k not in _rejected}

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
                    selectinload(Slice.created_by),
                    selectinload(Slice.changed_by),
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
                            now = datetime.now(changed_on_dt.tzinfo or timezone.utc)
                            if changed_on_dt.tzinfo is None:
                                now = datetime.now()
                            changed_on_humanized = humanize.naturaltime(
                                now - changed_on_dt
                            )
                        except Exception:  # noqa: BLE001
                            changed_on_humanized = ""
                    desc = getattr(chart, "description", None) or ""
                    from superset.utils.core import markdown as _markdown

                    chart_cache_timeout = getattr(chart, "cache_timeout", None)
                    # ``Slice.form_data`` property applies update_time_range
                    # which migrates since/until → time_range.
                    defaults = chart.form_data
                    slice_data = {
                        "cache_timeout": chart_cache_timeout,
                        "changed_on": changed_on_iso,
                        "changed_on_humanized": changed_on_humanized,
                        "datasource": getattr(chart, "datasource_name", None),
                        "description": desc or None,
                        # Rendered+sanitised HTML, not a bare <p> wrap.
                        "description_markeddown": _markdown(desc),
                        "edit_url": f"/chart/edit/{chart.id}",
                        "form_data": defaults,
                        # ``Slice.query_context`` is stored as a raw JSON string
                        # on the chart row; returned unparsed.
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
            except (ValueError, TypeError):
                pass

        # 3. Resolve datasource: form_data["datasource"] wins over the URL
        # datasource_id param. After resolution the pair is unconditionally
        # written back to form_data["datasource"] normalised to "<id>__<type>".
        ds_id: int | None = None
        ds_type: str = datasource_type or "table"

        # Priority 1: form_data["datasource"] = "21__table" wins over URL param.
        _fd_ds_str = str(form_data.get("datasource", ""))
        _fd_has_ds = "__" in _fd_ds_str
        if _fd_has_ds:
            _fd_parts = _fd_ds_str.split("__")
            _fd_id_str = _fd_parts[0]
            if _fd_id_str != "None":
                try:
                    ds_id = int(_fd_id_str)
                    ds_type = _fd_parts[1] if len(_fd_parts) > 1 else "table"
                except (ValueError, IndexError):
                    pass

        # Priority 2: URL datasource_id param — only when form_data has no
        # "__"-containing datasource key (matching original fallback order).
        if not _fd_has_ds and ds_id is None and datasource_id_raw:
            try:
                ds_id = int(datasource_id_raw)
                # ds_type remains from the datasource_type URL param
            except (ValueError, TypeError):
                pass

        # Priority 3: liteset-only fallback to the chart's own datasource_id
        # for migrated charts whose stored form_data lacks a "datasource" key.
        if not _fd_has_ds and ds_id is None and slice_id_raw:
            try:
                _fb_chart = await chart_dao.find_by_id(int(slice_id_raw))
                if _fb_chart is not None:
                    _fb_ds_id = getattr(_fb_chart, "datasource_id", None)
                    if _fb_ds_id:
                        ds_id = int(_fb_ds_id)
                        ds_type = (
                            getattr(_fb_chart, "datasource_type", "table") or "table"
                        )
            except (ValueError, TypeError):
                pass

        form_data["datasource"] = f"{ds_id}__{ds_type}"

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
                    # Enforce datasource access. Without it any ``can_read
                    # Explore`` user (e.g. Gamma) could read the dataset
                    # name/columns/SQL and chart form_data of an inaccessible
                    # chart/datasource. Called unconditionally — wrapping in
                    # hasattr() would silently skip the access check when a
                    # custom security manager omits the method.
                    try:
                        await security_manager.raise_for_access(
                            datasource=dataset, user=current_user
                        )
                    except SupersetSecurityException as ex:
                        return Response(
                            content=ex.error.to_dict(),
                            status_code=403,
                        )
                    # When there is no ``viz_type`` in the merged form_data and
                    # the datasource defines a ``default_endpoint``, return a
                    # 302 ``{"redirect": ...}`` (the frontend follows it) instead
                    # of rendering an explore state. A chart whose viz_type is
                    # NULL/empty also triggers this (corrupt chart edge-case).
                    _viz_type = (
                        form_data.get("viz_type")
                        if isinstance(form_data, dict)
                        else None
                    )
                    _default_endpoint = getattr(dataset, "default_endpoint", None)
                    if not _viz_type and _default_endpoint:
                        return Response(
                            content={"redirect": _default_endpoint},
                            status_code=302,
                        )
                    # Build dataset_data matching original datasource.data structure
                    db_obj = getattr(dataset, "database", None)
                    # ``select_star`` — a compiled ``SELECT * … LIMIT 100``
                    # preview. The property is sync (compiles via the sync
                    # engine), so computed off-loop.
                    import asyncio as _asyncio

                    _select_star: str | None = None
                    if db_obj is not None and hasattr(type(dataset), "select_star"):
                        _select_star = await _asyncio.to_thread(
                            lambda: dataset.select_star
                        )
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
                                getattr(db_obj, "database_name", "") if db_obj else ""
                            ),
                            "backend": (
                                getattr(db_obj, "backend", "") if db_obj else ""
                            ),
                            "configuration_method": (
                                getattr(db_obj, "configuration_method", None)
                                if db_obj
                                else None
                            ),
                            "allows_subquery": (
                                getattr(db_obj, "allows_subquery", True)
                                if db_obj
                                else True
                            ),
                            "allows_cost_estimate": (
                                getattr(db_obj, "allows_cost_estimate", False)
                                if db_obj
                                else False
                            ),
                            "allows_virtual_table_explore": (
                                getattr(db_obj, "allows_virtual_table_explore", True)
                                if db_obj
                                else True
                            ),
                            "explore_database_id": (
                                getattr(db_obj, "explore_database_id", 0)
                                if db_obj
                                else 0
                            ),
                            "schema_options": (
                                getattr(db_obj, "schema_options", {}) if db_obj else {}
                            ),
                            # Cleared to prevent leaking sensitive connection info.
                            "parameters": {},
                            "disable_data_preview": (
                                getattr(db_obj, "disable_data_preview", False)
                                if db_obj
                                else False
                            ),
                            "disable_drill_to_detail": (
                                getattr(db_obj, "disable_drill_to_detail", False)
                                if db_obj
                                else False
                            ),
                            "allow_multi_catalog": (
                                getattr(db_obj, "allow_multi_catalog", False)
                                if db_obj
                                else False
                            ),
                            "parameters_schema": (
                                getattr(db_obj, "parameters_schema", {})
                                if db_obj
                                else {}
                            ),
                            "engine_information": (
                                getattr(db_obj, "engine_information", {})
                                if db_obj
                                else {}
                            ),
                        },
                        "schema": getattr(dataset, "schema", None),
                        "catalog": getattr(dataset, "catalog", None),
                        "sql": getattr(dataset, "sql", None),
                        "is_sqllab_view": getattr(dataset, "is_sqllab_view", False),
                        "description": getattr(dataset, "description", None),
                        "default_endpoint": getattr(dataset, "default_endpoint", None),
                        "cache_timeout": getattr(dataset, "cache_timeout", None),
                        "offset": getattr(dataset, "offset", 0),
                        "fetch_values_predicate": getattr(
                            dataset, "fetch_values_predicate", None
                        ),
                        "template_params": getattr(dataset, "template_params", None),
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
                        "select_star": _select_star,
                        "health_check_message": None,
                        "column_formats": {
                            getattr(m, "metric_name", ""): getattr(m, "d3format", None)
                            for m in (getattr(dataset, "metrics", None) or [])
                            if getattr(m, "d3format", None)
                        },
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
                        # The frontend DatasourceControl reads ``o.id`` off each entry.
                        "owners": [
                            {
                                "first_name": getattr(o, "first_name", None),
                                "last_name": getattr(o, "last_name", None),
                                "username": getattr(o, "username", None),
                                "id": o.id,
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
                                "is_certified": bool(getattr(c, "certified_by", None)),
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
                                "is_certified": bool(getattr(m, "certified_by", None)),
                            }
                            for m in (getattr(dataset, "metrics", None) or [])
                        ],
                        "main_dttm_col": getattr(dataset, "main_dttm_col", None),
                        "filter_select_enabled": getattr(
                            dataset, "filter_select_enabled", True
                        ),
                        # Without this list the control cannot match saved
                        # form_data values like ``"P3M"`` against its choices
                        # and silently drops the grain, resulting in missing
                        # time truncation in chart queries.
                        "time_grain_sqla": _build_time_grain_sqla_choices(
                            getattr(dataset, "database", None)
                        ),
                        # ``granularity_sqla`` is the list of available
                        # datetime columns for the "Time column" control.
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
                        # ``order_by_cols`` SelectControl. Must be the
                        # json-encoded [column, asc] shape — otherwise the
                        # control silently drops the selection, resulting in
                        # empty ``orderby`` in chart queries.
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
            except SupersetException as ex:
                message = ex.message
            except SQLAlchemyError:
                message = "SQLAlchemy error"
            except Exception:  # noqa: BLE001, S110
                pass  # Dataset metadata is optional, continue without it

        # Build metadata. Reuses the ``chart`` loaded above with eager-loaded
        # ``owners``, ``dashboards``, ``created_by``, ``changed_by`` — no
        # extra round-trips.
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
                    f"{getattr(obj, 'first_name', '')} {getattr(obj, 'last_name', '')}"
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

        # When no slice/datasource is supplied, ``dataset`` becomes a
        # ``[Missing Dataset]`` placeholder.
        if dataset_data is None:
            dataset_data = {
                "name": "[Missing Dataset]",
                "type": str(datasource_type or "table"),
                "columns": [],
                "metrics": [],
                "database": {"id": 0, "backend": "", "parameters": {}},
            }

        # Apply legacy filter migration, extra-filter merging, and URL param
        # injection. These transforms must run AFTER slice defaults are merged
        # but BEFORE the response is built so the frontend always receives
        # adhoc_filters.
        from superset.legacy import update_time_range
        from superset.utils.core import (
            convert_legacy_filters_into_adhoc,
            merge_extra_filters,
            merge_request_params,
        )

        # Migrate legacy since/until → time_range BEFORE the filter transforms.
        # Running it after merge_extra_filters would let a merged "No filter"
        # temporal adhoc-filter setdefault time_range="No filter" incorrectly.
        update_time_range(form_data)

        convert_legacy_filters_into_adhoc(form_data)
        merge_extra_filters(form_data)
        # Merge URL query params (excluding ``form_data`` and ``r``) into
        # ``form_data["url_params"]`` so Jinja template context and
        # URL-param–driven dashboards/explore links work correctly.
        merge_request_params(form_data, dict(request.query_params))

        return {
            "result": {
                "dataset": dataset_data,
                "form_data": form_data,
                "slice": slice_data,
                "message": message or None,
                "metadata": metadata,
            }
        }
