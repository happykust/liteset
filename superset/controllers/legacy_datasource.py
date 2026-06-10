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
"""Legacy ``/datasource/*`` routes ported from the original
``superset/views/datasource/views.py`` Flask-AppBuilder views.

Full-functionality port of ``POST /datasource/samples``, used by the
Explore "View samples" panel and drill-to-detail. Mirrors the original
``get_samples`` helper: runs a sample query with optional drill-detail
filters (SamplesPayloadSchema) + a separate count(*) query, then
merges the results with pagination metadata.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from collections import Counter
from typing import Any

import pandas as pd
from litestar import Controller, get, post, Request
from litestar.di import Provide
from litestar.response import Response

from superset.common.query_context import AsyncQueryContext
from superset.common.query_context_processor import AsyncQueryContextProcessor
from superset.common.query_object import AsyncQueryObject
from superset.exceptions import SupersetException
from superset.guards.rbac import require_permission
from superset.providers import provide_datasource_dao
from superset.sql.parse import Table
from superset.typing import DatasourceDAOProtocol, UserProtocol

logger = logging.getLogger(__name__)


_VALID_DATASOURCE_TYPES = {"table", "query", "saved_query", "view", "dataset"}


# ---------------------------------------------------------------------------
# Standalone helpers (models are off-limits; implement logic here)
# ---------------------------------------------------------------------------


def _sanitize_datasource_data(datasource_data: dict[str, Any]) -> dict[str, Any]:
    """Strip sensitive parameters from ``datasource_data["database"]``.

    Mirrors ``superset_old/views/utils.py:sanitize_datasource_data``
    exactly — sets ``database.parameters`` to ``{}`` so that engine
    credentials are never sent to the frontend.
    """
    if datasource_data:
        db_info = datasource_data.get("database")
        if db_info:
            db_info["parameters"] = {}
    return datasource_data


def _get_fk_many_from_list(
    object_list: list[dict[str, Any]],
    fkmany: list[Any],
    fkmany_class: type,
    key_attr: str,
) -> list[Any]:
    """Sync a one-to-many ORM list from a list of plain dicts.

    1:1 port of ``BaseDatasource.get_fk_many_from_list`` from
    ``superset_old/connectors/sqla/models.py:535``. Used by
    :func:`_update_from_object` to sync columns and metrics from
    the legacy editor payload.
    """
    object_dict = {o.get(key_attr): o for o in object_list}

    # delete fks that have been removed
    fkmany = [o for o in fkmany if getattr(o, key_attr) in object_dict]

    # sync existing fks
    for fk in fkmany:
        obj = object_dict.get(getattr(fk, key_attr))
        if obj:
            for attr in fkmany_class.update_from_object_fields:
                setattr(fk, attr, obj.get(attr))

    # create new fks
    new_fks = []
    orm_keys = [getattr(o, key_attr) for o in fkmany]
    for obj in object_list:
        key = obj.get(key_attr)
        if key not in orm_keys:
            obj_copy = dict(obj)
            obj_copy.pop("id", None)
            orm_kwargs: dict[str, Any] = {
                k: obj_copy[k]
                for k in obj_copy
                if k in fkmany_class.update_from_object_fields
            }
            new_obj = fkmany_class(**orm_kwargs)
            new_fks.append(new_obj)
    fkmany += new_fks
    return fkmany


def _update_from_object(orm_datasource: Any, obj: dict[str, Any]) -> None:
    """Update an ORM datasource instance from the legacy editor payload.

    1:1 port of ``BaseDatasource.update_from_object`` from
    ``superset_old/connectors/sqla/models.py:574``. Called by the
    ``POST /datasource/save/`` route.
    """
    from superset.models.connectors import SqlMetric, TableColumn

    for attr in orm_datasource.update_from_object_fields:
        setattr(orm_datasource, attr, obj.get(attr))

    orm_datasource.owners = obj.get("owners", [])

    # Syncing metrics
    metrics = (
        _get_fk_many_from_list(
            obj["metrics"], orm_datasource.metrics, SqlMetric, "metric_name"
        )
        if "metrics" in obj
        else []
    )
    orm_datasource.metrics = metrics

    # Syncing columns
    columns = (
        _get_fk_many_from_list(
            obj["columns"], orm_datasource.columns, TableColumn, "column_name"
        )
        if "columns" in obj
        else []
    )
    orm_datasource.columns = columns


async def _get_datasource_by_name(
    session: Any,
    database_name: str,
    catalog: str | None,
    schema: str | None,
    datasource_name: str,
) -> Any | None:
    """Look up a ``SqlaTable`` by database/catalog/schema/table names.

    1:1 port of ``SqlaTable.get_datasource_by_name`` from
    ``superset_old/connectors/sqla/models.py:1218``. Used by
    ``GET /datasource/external_metadata_by_name/``.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from superset.models.connectors import SqlaTable
    from superset.models.core import Database

    stmt = (
        select(SqlaTable)
        .join(Database, SqlaTable.database_id == Database.id)
        .where(SqlaTable.table_name == datasource_name)
        .where(Database.database_name == database_name)
        .where(SqlaTable.catalog == catalog)
        .options(
            selectinload(SqlaTable.columns),
            selectinload(SqlaTable.metrics),
            selectinload(SqlaTable.database),
        )
    )
    result = await session.execute(stmt)
    rows = result.scalars().all()
    # Match original Python-level schema filter
    for tbl in rows:
        if (schema or None) == (tbl.schema or None):
            return tbl
    return None


async def _get_physical_table_metadata_async(
    database: Any,
    table: Table,
    normalize_columns: bool = False,
) -> list[dict[str, Any]]:
    """Return column metadata for a physical table using the async engine.

    Mirrors ``superset_old/connectors/sqla/utils.py:get_physical_table_metadata``
    which uses SQLAlchemy Inspector via a sync connection. Here we run
    the inspector inside ``async_conn.run_sync``.

    Raises ``sqlalchemy.exc.NoSuchTableError`` when the table (and view)
    do not exist — exactly as the original does at
    ``superset_old/connectors/sqla/utils.py:60-61``::

        if not (database.has_table(table) or database.has_view(table)):
            raise NoSuchTableError(table)

    Used when ``SqlaTable.get_datasource_by_name`` returns ``None`` (i.e.
    the table is not tracked in Superset) in ``external_metadata_by_name``.
    """
    from sqlalchemy.exc import NoSuchTableError

    from superset.utils.database import get_async_connection

    def _inspect_sync(sync_conn: Any) -> list[dict[str, Any]]:
        from sqlalchemy import inspect as sa_inspect
        from sqlalchemy.exc import NoSuchTableError as _NoSuchTableError

        inspector = sa_inspect(sync_conn)

        # Mirror original: raise NoSuchTableError when the table / view is not
        # visible to the connection (superset_old/connectors/sqla/utils.py:59-61).
        table_exists = inspector.has_table(table.table, schema=table.schema or None)
        if not table_exists:
            # Also check views (mirrors database.has_view in the original).
            try:
                views = inspector.get_view_names(schema=table.schema or None)
            except Exception:  # noqa: BLE001
                views = []
            if table.table not in views:
                raise _NoSuchTableError(table.table)

        raw_cols = inspector.get_columns(table.table, schema=table.schema)

        cols: list[dict[str, Any]] = []
        for col in raw_cols:
            col_name: str = col.get("name") or col.get("column_name", "")
            if normalize_columns:
                col_name = col_name.lower()
            type_str: str = ""
            try:
                type_str = str(col.get("type", ""))
            except Exception:  # noqa: BLE001
                type_str = type(col.get("type", "")).__name__
            cols.append(
                {
                    "name": col_name,
                    "type": type_str.split("(")[0] if "(" in type_str else type_str,
                    "longType": type_str,
                    "comment": col.get("comment"),
                }
            )
        return cols

    try:
        async with get_async_connection(database) as (async_conn, _):
            return await async_conn.run_sync(_inspect_sync)
    except NoSuchTableError:
        raise
    except Exception:  # noqa: BLE001
        return []


def _replace_verbose_with_column(
    filters: list[dict[str, Any]],
    datasource: Any,
) -> None:
    """Rewrite filter ``col`` values from verbose name → physical column name.

    1:1 port of ``superset_old/views/datasource/utils.py::replace_verbose_with_column``.
    Always uses ``verbose_name`` / ``column_name`` attributes regardless
    of whether the datasource is virtual. The frontend may send either the
    physical column name or the verbose label; the query builder only
    understands the physical name.
    """
    columns = getattr(datasource, "columns", None) or []
    if not columns:
        return

    verbose_attr = "verbose_name"
    column_attr = "column_name"

    for flt in filters:
        col_value = flt.get("col")
        if col_value is None:
            logger.warning("Filter missing 'col' key: %s", flt)
            continue

        match = None
        for col in columns:
            if not hasattr(col, verbose_attr) or not hasattr(col, column_attr):
                logger.warning(
                    "Column object %s missing expected attributes '%s' or '%s'",
                    col,
                    verbose_attr,
                    column_attr,
                )
                continue

            if getattr(col, verbose_attr) == col_value:
                match = getattr(col, column_attr)
                break

        if match:
            flt["col"] = match


def _resolve_per_page(per_page_raw: str | None) -> tuple[int, str | None]:
    """Resolve the ``per_page`` query parameter to an int.

    Returns ``(per_page, error_message_or_None)``.  Default is
    ``SAMPLES_ROW_LIMIT`` (config) or 1000.  Range 1–1000 is enforced,
    mirroring ``SamplesRequestSchema.set_default_per_page``.
    """
    samples_row_limit = 1000
    try:
        from superset.config import SupersetSettings as _SRLSettings

        samples_row_limit = int(
            getattr(_SRLSettings(), "samples_row_limit", 1000)  # type: ignore[call-arg]
        )
    except Exception:  # noqa: BLE001
        logger.debug("Could not read samples_row_limit from config", exc_info=True)

    try:
        per_page = int(per_page_raw) if per_page_raw is not None else samples_row_limit
    except (TypeError, ValueError):
        # Original ``fields.Integer()`` raises ValidationError for a
        # non-integer string → HTTP 400 (views.py:200-203), not a silent
        # fallback to the default.
        return samples_row_limit, "per_page must be an integer"

    # Original SamplesRequestSchema: Range(min=1, max=1000) raises
    # ValidationError for out-of-range values → HTTP 400.  Mirror that.
    if per_page < 1 or per_page > 1000:
        per_page = max(1, min(per_page, 1000))  # keep a sane value
        return per_page, "per_page must be between 1 and 1000"
    return per_page, None


def _parse_samples_params(
    request: Request[Any, Any, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None, list[str]]:
    """Validate and parse request.args + JSON body.

    Returns ``(params, payload, errors)`` where ``params`` mirrors
    ``SamplesRequestSchema`` and ``payload`` mirrors
    ``SamplesPayloadSchema`` (None if the body was empty/absent).
    """
    errors: list[str] = []
    qp = request.query_params

    datasource_type = qp.get("datasource_type") or "table"
    if datasource_type not in _VALID_DATASOURCE_TYPES:
        errors.append(
            f"datasource_type must be one of {sorted(_VALID_DATASOURCE_TYPES)}"
        )

    try:
        datasource_id = int(qp.get("datasource_id") or 0)
    except (TypeError, ValueError):
        datasource_id = 0
        errors.append("datasource_id must be an integer")
    if datasource_id <= 0:
        errors.append("datasource_id is required")

    force_raw = (qp.get("force") or "false").lower()
    force = force_raw in ("true", "1", "yes")

    try:
        page = int(qp.get("page") or 1)
    except (TypeError, ValueError):
        # Original ``fields.Integer(load_default=1)`` raises ValidationError
        # for a non-integer string → HTTP 400 (views.py:200-203).
        page = 1
        errors.append("page must be an integer")
    if page < 1:
        page = 1

    per_page_raw = qp.get("per_page")
    per_page, per_page_error = _resolve_per_page(per_page_raw)
    if per_page_error:
        errors.append(per_page_error)

    dashboard_id_raw = qp.get("dashboard_id")
    dashboard_id: int | None = None
    if dashboard_id_raw is not None:
        try:
            dashboard_id = int(dashboard_id_raw)
        except (TypeError, ValueError):
            errors.append("dashboard_id must be an integer")

    params = {
        "datasource_type": datasource_type,
        "datasource_id": datasource_id,
        "force": force,
        "page": page,
        "per_page": per_page,
        "dashboard_id": dashboard_id,
    }
    return params, None, errors


async def _parse_samples_payload(
    request: Request[Any, Any, Any],
) -> dict[str, Any]:
    """Parse the optional JSON body (drill-detail filters, time_range, extras).

    Always returns a dict (possibly empty) — mirrors SamplesPayloadSchema's
    @pre_load handler that converts None→{} so the caller always sees a dict
    and therefore always uses DRILL_DETAIL result_type (superset_old/
    views/datasource/schemas.py:87-92, views/datasource/utils.py:107-133).

    Unknown keys are silently ignored, matching SamplesPayloadSchema semantics.
    """
    try:
        raw = await request.json()
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(raw, dict):
        return {}
    payload: dict[str, Any] = {}
    if isinstance(raw.get("filters"), list):
        payload["filters"] = list(raw["filters"])
    if "granularity" in raw:
        payload["granularity"] = raw.get("granularity")
    if "time_range" in raw:
        payload["time_range"] = raw.get("time_range")
    if isinstance(raw.get("extras"), dict):
        payload["extras"] = raw.get("extras")
    return payload


class LegacyDatasourceController(Controller):
    """Legacy ``/datasource`` routes ported from the original FAB view."""

    path = "/datasource"
    tags = ["Legacy", "Datasource"]
    dependencies = {
        "ds_dao": Provide(provide_datasource_dao, sync_to_thread=False),
    }

    @post(
        "/samples",
        guards=[require_permission("can_samples", "Datasource")],
        status_code=200,
    )
    async def samples(  # noqa: C901
        self,
        request: Request[Any, Any, Any],
        ds_dao: DatasourceDAOProtocol,
        current_user: UserProtocol,
    ) -> Response[dict[str, Any]]:
        """POST /datasource/samples?datasource_type=<t>&datasource_id=<id>
        &force=<bool>&page=<n>&per_page=<n>&dashboard_id=<id>

        Body (SamplesPayloadSchema, optional):

            {
                "filters": [<ChartDataFilterSchema>],
                "granularity": str | null,
                "time_range": str | null,
                "extras": {<ChartDataExtrasSchema>}
            }

        Returns the original-compatible shape::

            {"result": {
                "data": [...], "colnames": [...], "coltypes": [...],
                "page": n, "per_page": n, "total_count": n,
                "status": "success", ...
            }}
        """
        params, _payload_unused, errors = _parse_samples_params(request)
        if errors:
            return Response(
                content={"message": "; ".join(errors)},
                status_code=400,
            )

        payload = await _parse_samples_payload(request)

        # --- Permissions: match original Datasource.samples ---------------
        #
        # Non-guest users need plain datasource access (validated by the
        # ``require_authentication`` guard + ``raise_for_access`` below).
        # Guest users (used by embedded dashboards) need to satisfy the
        # dashboard-based drill-through access rule.
        from superset.dependencies import provide_security_manager

        sec_mgr = await provide_security_manager(
            ds_dao.session,  # type: ignore[attr-defined]
            request.app.state,
        )
        user = current_user
        is_guest = (
            sec_mgr.is_guest_user(user) if hasattr(sec_mgr, "is_guest_user") else False
        )

        datasource = await ds_dao.get_datasource(
            params["datasource_type"], params["datasource_id"]
        )
        if datasource is None:
            return Response(
                content={
                    "errors": [
                        {
                            "message": (
                                f'Datasource "{params["datasource_id"]}" not found.'
                            ),
                            "error_type": "ObjectNotFoundError",
                            "level": "error",
                            "extra": {},
                        }
                    ],
                    "message": (f'Datasource "{params["datasource_id"]}" not found.'),
                },
                status_code=404,
            )

        if is_guest:
            # Embedded dashboard drill-to-detail permission check.
            # Mirrors the original ``Datasource.samples`` guest branch:
            # a dashboard_id is required, the dashboard must exist (404 if not),
            # and the guest must be allowed to drill the dataset via that
            # dashboard. Fails CLOSED (403) on any denial.
            dashboard_id = params.get("dashboard_id")
            if not dashboard_id:
                return Response(content={"message": "Forbidden"}, status_code=403)

            from sqlalchemy import select as _select
            from sqlalchemy.orm import selectinload as _selectinload

            from superset.models.dashboard import Dashboard

            dash_stmt = (
                _select(Dashboard)
                .where(Dashboard.id == dashboard_id)
                .options(
                    _selectinload(Dashboard.slices),
                    _selectinload(Dashboard.roles),
                    _selectinload(Dashboard.embedded),
                )
            )
            dash_result = await ds_dao.session.execute(dash_stmt)
            dashboard = dash_result.scalars().one_or_none()
            if dashboard is None:
                return Response(content={"message": "Not found"}, status_code=404)

            allowed = await sec_mgr.can_drill_dataset_via_dashboard_access(
                datasource, dashboard, user=user
            )
            if not allowed:
                return Response(content={"message": "Forbidden"}, status_code=403)
        # Non-guest path: NO datasource-level raise_for_access — the original
        # ``Datasource.samples`` gates non-guest access solely on the
        # ("can_samples", "Datasource") role permission and calls
        # ``get_samples()`` directly (superset_old/views/datasource/views.py:
        # 194-230); ``get_payload()`` performs no security check either.

        # Replace verbose column names in filters with physical ones,
        # matching the original ``replace_verbose_with_column`` helper.
        if payload and payload.get("filters"):
            _replace_verbose_with_column(payload["filters"], datasource)

        # --- Build a query object for the samples --------------------------
        from superset.config import SupersetSettings

        settings_obj = SupersetSettings()  # type: ignore[call-arg]
        page: int = params["page"]
        per_page: int = params["per_page"]
        force: bool = params["force"]

        # Apply SAMPLES_ROW_LIMIT cap — 1:1 with original
        # ``get_limit_clause`` which caps the effective row_limit at
        # SAMPLES_ROW_LIMIT (default 1000) even if per_page passes
        # schema validation.
        samples_row_limit: int = int(getattr(settings_obj, "samples_row_limit", 1000))
        row_limit = per_page
        if row_limit < 0 or row_limit > samples_row_limit:
            row_limit = samples_row_limit
        offset = (page - 1) * row_limit

        samples_qo = AsyncQueryObject(
            datasource={
                "type": datasource.type,
                "id": datasource.id,
            },
            metrics=[],
            columns=[],
            filters=list(payload.get("filters", [])) if payload else [],
            row_limit=row_limit,
            row_offset=offset,
            extras=dict(payload.get("extras") or {}) if payload else {},
            time_range=payload.get("time_range") if payload else None,
            granularity=payload.get("granularity") if payload else None,
            post_processing=[],
            orderby=[],
            is_timeseries=False,
        )

        samples_context = AsyncQueryContext(
            datasource=datasource,
            queries=[samples_qo],
            force=force,
            result_format="json",
            result_type=("drill_detail" if payload is not None else "samples"),
        )

        processor = AsyncQueryContextProcessor(
            datasource=datasource,
            settings=settings_obj,
            security_manager=sec_mgr,
            user=user,
            query_context=samples_context,
        )

        # --- Run the count(*) query FIRST, matching original get_samples order
        # (superset_old/views/datasource/utils.py:155-172: count_star runs first,
        # raises DatasetSamplesFailedError on failure before sample is ever run).
        count_qo = copy.deepcopy(samples_qo)
        count_qo.metrics = [
            {
                "expressionType": "SQL",
                "sqlExpression": "COUNT(*)",
                "label": "COUNT(*)",
            }
        ]
        count_qo.columns = []
        count_qo.row_limit = 1
        count_qo.row_offset = 0
        count_qo.orderby = []
        count_qo.post_processing = []
        count_context = AsyncQueryContext(
            datasource=datasource,
            queries=[count_qo],
            force=force,
            result_format="json",
            result_type="full",
        )
        count_processor = AsyncQueryContextProcessor(
            datasource=datasource,
            settings=settings_obj,
            security_manager=sec_mgr,
            user=user,
            query_context=count_context,
        )
        # NO broad try/except here — 1:1 with the original
        # (superset_old/views/datasource/utils.py:155 catches only
        # IndexError/KeyError): exceptions from get_payload propagate to the
        # app-level handlers (@handle_api_exception semantics — a
        # SupersetException keeps its own status, anything else → 500).
        count_result = await count_processor.get_payload(
            query_objects=[count_qo], force=force
        )
        count_star_data: dict[str, Any] = (count_result.get("queries") or [{}])[0]

        # Mirror original: if count_star itself reports failure, raise immediately
        # before running the sample query (superset_old utils.py:158-159).
        # DatasetSamplesFailedError IS-A CommandInvalidError → HTTP 422
        # (superset_old/commands/dataset/exceptions.py:181,
        # superset_old/commands/exceptions.py:54-57).
        if count_star_data.get("status") == "failed":
            return Response(
                content={
                    "errors": [
                        {
                            "message": count_star_data.get(
                                "error", "Count query failed"
                            ),
                            "error_type": "GENERIC_BACKEND_ERROR",
                            "level": "error",
                            "extra": {},
                        }
                    ],
                },
                status_code=422,
            )

        # Extract total_count from count_star result.
        # 1:1 with superset_old/views/datasource/utils.py:169:
        #   sample_data["total_count"] = count_star_data["data"][0]["COUNT(*)"]
        # The original wraps this in except (IndexError, KeyError)
        # → DatasetSamplesFailedError.  In liteset, get_df_payload returns
        # "df" (DataFrame) rather than "data" (list of dicts), so we read
        # from the DataFrame instead; strict access mirrors the original's
        # failure mode.
        try:
            _count_df = count_star_data["df"]
            total_count = int(_count_df.iloc[0]["COUNT(*)"])
        except (IndexError, KeyError):
            # 1:1 original: ``raise DatasetSamplesFailedError from exc``
            # (utils.py:171-172) → CommandInvalidError → HTTP 422.
            return Response(
                content={
                    "errors": [
                        {
                            "message": (
                                "Failed to extract COUNT(*) from count query result"
                            ),
                            "error_type": "GENERIC_BACKEND_ERROR",
                            "level": "error",
                            "extra": {},
                        }
                    ],
                },
                status_code=422,
            )

        # --- Now run the sample query (original order: count first, then samples)
        # As above: no broad catch — exceptions propagate 1:1 with the original.
        sample_result = await processor.get_payload(
            query_objects=[samples_qo], force=force
        )

        sample_data = (sample_result.get("queries") or [{}])[0]
        if isinstance(sample_data.get("df"), pd.DataFrame):
            df = sample_data.pop("df")
            sample_data["data"] = df.to_dict(orient="records")
            sample_data["colnames"] = list(df.columns)
            sample_data.setdefault("coltypes", [])
            sample_data["rowcount"] = len(sample_data["data"])

        # Mirror original: if sample query fails, delete the count_star cache
        # key (superset_old utils.py:163-165) and return an error.
        if sample_data.get("status") == "failed":
            _cache_key = count_star_data.get("cache_key")
            if _cache_key:
                try:
                    from superset.extensions import cache_manager

                    await cache_manager.data_cache.delete(_cache_key)
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "Failed to delete count_star cache key %s",
                        _cache_key,
                        exc_info=True,
                    )
            # 1:1 original: ``raise DatasetSamplesFailedError(sample_data.get
            # ("error"))`` (utils.py:163-165) → CommandInvalidError → HTTP 422.
            return Response(
                content={
                    "errors": [
                        {
                            "message": sample_data.get("error", "Sample query failed"),
                            "error_type": "GENERIC_BACKEND_ERROR",
                            "level": "error",
                            "extra": {},
                        }
                    ],
                },
                status_code=422,
            )

        sample_data["page"] = page
        sample_data["per_page"] = per_page
        sample_data["total_count"] = total_count
        sample_data.setdefault("status", "success")

        return Response(content={"result": sample_data}, status_code=200)

    # ------------------------------------------------------------------
    # POST /datasource/save/ — legacy editor save
    # ------------------------------------------------------------------
    @post(
        "/save/",
        guards=[require_permission("can_save", "Datasource")],
        media_type="application/json",
        status_code=200,
    )
    async def save(  # noqa: C901
        self,
        request: Request[Any, Any, Any],
        ds_dao: DatasourceDAOProtocol,
        current_user: UserProtocol,
    ) -> Response[dict[str, Any]]:
        """POST /datasource/save/ — save datasource from the legacy editor.

        Reads ``data`` from the request form (a JSON string), validates
        ownership, checks for duplicate column names, then calls
        ``_update_from_object`` to sync the ORM instance.

        Mirrors ``superset_old/views/datasource/views.py:Datasource.save``.
        """
        try:
            form = await request.form()
            raw_data = form.get("data")
        except Exception:  # noqa: BLE001
            raw_data = None

        if not isinstance(raw_data, str):
            return Response(
                content={"message": "Request missing data field."},
                status_code=500,
            )

        try:
            datasource_dict: dict[str, Any] = json.loads(raw_data)
        except (ValueError, TypeError) as exc:
            return Response(
                content={"message": f"Invalid JSON in data field: {exc}"},
                status_code=400,
            )

        normalize_columns: bool = bool(datasource_dict.get("normalize_columns", False))
        always_filter_main_dttm: bool = bool(
            datasource_dict.get("always_filter_main_dttm", False)
        )
        datasource_dict["normalize_columns"] = normalize_columns
        datasource_dict["always_filter_main_dttm"] = always_filter_main_dttm

        datasource_id: int = datasource_dict.get("id", 0)
        datasource_type: str = datasource_dict.get("type", "table")
        database_id: int | None = (datasource_dict.get("database") or {}).get("id")

        orm_datasource = await ds_dao.get_datasource(datasource_type, datasource_id)
        if orm_datasource is None:
            return Response(
                content={"message": f'Datasource "{datasource_id}" not found.'},
                status_code=404,
            )

        # Datasource-access gate on this WRITE endpoint. Upstream guards the
        # legacy ``Datasource.save`` view with ``@has_access_api`` (the ``can_save
        # Datasource`` write perm, which gamma lacks — ``Datasource`` is a
        # READ-ONLY model view for gamma). The port's route is only
        # ``require_authentication``, and the conditional ``raise_for_ownership``
        # below fires ONLY when the payload carries ``owners`` — so a user with no
        # datasource access (e.g. gamma) could otherwise POST here and rewrite a
        # datasource's metrics/columns. Gate on datasource access (admin /
        # all_datasource_access pass, gamma → 403) before any mutation. We can't
        # use the upstream ``can_save Datasource`` perm directly because the port
        # doesn't seed it (a broader role-tuple sweep); ``raise_for_access`` is
        # the equivalent data-access gate already used on the v1 datasource GET.
        from superset.dependencies import provide_security_manager
        from superset.exceptions import SupersetSecurityException

        _sec_mgr = await provide_security_manager(
            ds_dao.session,  # type: ignore[attr-defined]
            request.app.state,
        )
        if hasattr(_sec_mgr, "raise_for_access"):
            try:
                await _sec_mgr.raise_for_access(
                    datasource=orm_datasource, user=current_user
                )
            except SupersetSecurityException as exc:
                return Response(
                    content={"message": getattr(exc, "message", str(exc))},
                    status_code=403,
                )

        # Always assign database_id — mirrors original (views/datasource/views.py:91):
        # ``orm_datasource.database_id = database_id`` with no None guard.
        # Explicit null ({database: {id: null}}) intentionally dissociates the
        # datasource from its database, matching the original unconditional write.
        orm_datasource.database_id = database_id

        # Check ownership when the payload includes owners, then populate the
        # owner list UNCONDITIONALLY — 1:1 with the original
        # (superset_old/views/datasource/views.py:93-102) where
        # ``populate_owner_list(datasource_dict["owners"], ...)`` sits OUTSIDE
        # the ``if`` and raises KeyError (→ 500) for an owners-less payload
        # BEFORE ``update_from_object`` could silently wipe the owners.
        from superset.commands.utils import populate_owner_list
        from superset.dependencies import provide_security_manager

        sec_mgr = await provide_security_manager(
            ds_dao.session,  # type: ignore[attr-defined]
            request.app.state,
        )

        if (
            "owners" in datasource_dict
            and getattr(orm_datasource, "owner_class", None) is not None
        ):
            # Raise for ownership — 1:1 with views.py:93-98: pass the
            # current user id (a REQUIRED positional arg) and catch only the
            # security exception. The previous bare ``raise_for_ownership(
            # orm_datasource)`` raised ``TypeError`` (missing ``user_id``) that
            # the over-broad ``except Exception`` masked as a 403 for everyone —
            # owners and admins included — so saving a datasource with owners
            # always failed.
            from superset.exceptions import SupersetSecurityException

            try:
                await sec_mgr.raise_for_ownership(orm_datasource, current_user.id)
            except SupersetSecurityException:
                # ``orm_datasource.database_id`` was already reassigned
                # above; roll it back so the denied save persists nothing.
                # Upstream only ``db.session.commit()``s on the success
                # path — early error returns discard the dirty change at
                # Flask teardown. The liteset request wrapper instead
                # COMMITS a returned Response, so we must roll back here.
                await ds_dao.session.rollback()  # type: ignore[attr-defined]
                return Response(
                    content={"message": "Datasource access is restricted."},
                    status_code=403,
                )

        # KeyError ("owners" absent) propagates → 500, no commit — matches
        # the original's unconditional ``datasource_dict["owners"]`` access.
        owner_ids = [
            o if isinstance(o, int) else o.get("id", o)
            for o in (datasource_dict["owners"] or [])
        ]
        datasource_dict["owners"] = await populate_owner_list(
            sec_mgr,
            current_user_id=current_user.id,
            owner_ids=owner_ids,
            default_to_user=False,
        )

        # Duplicate column name detection — mirrors original Counter check
        columns_payload: list[dict[str, Any]] = datasource_dict.get("columns") or []
        duplicates = [
            name
            for name, count in Counter(
                col.get("column_name", "") for col in columns_payload
            ).items()
            if count > 1
        ]
        if duplicates:
            # Discard the early ``database_id`` reassignment (see 403 note).
            await ds_dao.session.rollback()  # type: ignore[attr-defined]
            return Response(
                content={
                    "message": f"Duplicate column name(s): {','.join(duplicates)}"
                },
                status_code=409,
            )

        # Sync ORM instance from the editor payload
        try:
            await asyncio.to_thread(
                _update_from_object, orm_datasource, datasource_dict
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to update datasource from object")
            # ``_update_from_object`` may have partially mutated the ORM
            # instance before raising; discard it (see 403 note).
            await ds_dao.session.rollback()  # type: ignore[attr-defined]
            return Response(
                content={"message": f"Update failed: {exc}"},
                status_code=500,
            )

        data_out: dict[str, Any] = _sanitize_datasource_data(dict(orm_datasource.data))
        await ds_dao.session.commit()  # type: ignore[attr-defined]
        return Response(content=data_out, status_code=200)

    # ------------------------------------------------------------------
    # GET /datasource/get/<datasource_type>/<datasource_id>/ — legacy get
    # ------------------------------------------------------------------
    @get(
        "/get/{datasource_type:str}/{datasource_id:int}/",
        guards=[require_permission("can_get", "Datasource")],
        status_code=200,
    )
    async def datasource_get(
        self,
        datasource_type: str,
        datasource_id: int,
        ds_dao: DatasourceDAOProtocol,
    ) -> Response[dict[str, Any]]:
        """GET /datasource/get/<type>/<id>/ — return raw datasource data.

        Mirrors ``superset_old/views/datasource/views.py:Datasource.get``.
        """
        orm_datasource = await ds_dao.get_datasource(datasource_type, datasource_id)
        if orm_datasource is None:
            return Response(
                content={"message": f'Datasource "{datasource_id}" not found.'},
                status_code=404,
            )
        data_out: dict[str, Any] = _sanitize_datasource_data(dict(orm_datasource.data))
        return Response(content=data_out, status_code=200)

    # ------------------------------------------------------------------
    # GET /datasource/external_metadata/<type>/<id>/ — column info
    # ------------------------------------------------------------------
    @get(
        "/external_metadata/{datasource_type:str}/{datasource_id:int}/",
        guards=[require_permission("can_external_metadata", "Datasource")],
        status_code=200,
    )
    async def external_metadata(
        self,
        datasource_type: str,
        datasource_id: int,
        ds_dao: DatasourceDAOProtocol,
    ) -> Response[Any]:
        """GET /datasource/external_metadata/<type>/<id>/

        Retrieves column information from the source system.
        Mirrors ``superset_old/views/datasource/views.py:Datasource.external_metadata``.
        """
        orm_datasource = await ds_dao.get_datasource(datasource_type, datasource_id)
        if orm_datasource is None:
            return Response(
                content={"message": f'Datasource "{datasource_id}" not found.'},
                status_code=404,
            )
        try:
            ext_meta = await asyncio.to_thread(orm_datasource.external_metadata)
        except SupersetException as exc:
            return Response(
                content={"message": str(exc)},
                status_code=400,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "external_metadata failed for datasource %s", datasource_id
            )
            return Response(
                content={"message": str(exc)},
                status_code=400,
            )
        return Response(content=ext_meta, status_code=200)

    # ------------------------------------------------------------------
    # GET /datasource/external_metadata_by_name/ — by DB/schema/table
    # ------------------------------------------------------------------
    @get(
        "/external_metadata_by_name/",
        guards=[require_permission("can_external_metadata_by_name", "Datasource")],
        status_code=200,
    )
    async def external_metadata_by_name(
        self,
        request: Request[Any, Any, Any],
        ds_dao: DatasourceDAOProtocol,
    ) -> Response[Any]:  # noqa: C901
        """GET /datasource/external_metadata_by_name/?q=<rison>

        Accepts RISON-encoded query param ``q`` with keys:
          - ``datasource_type`` (required)
          - ``database_name`` (required)
          - ``catalog_name`` (optional)
          - ``schema_name`` (required)
          - ``table_name`` (required)
          - ``normalize_columns`` (optional bool)
          - ``always_filter_main_dttm`` (optional bool)

        Mirrors ``superset_old/views/datasource/views.py``
        ``Datasource.external_metadata_by_name``.
        """
        # The port uses ``prison`` (a maintained fork) for Rison decoding;
        # the unmaintained ``rison`` pypi package is not installed, so
        # importing it raises ``ModuleNotFoundError`` → 500 on every call.
        import prison as _rison

        q_raw = request.query_params.get("q") or ""
        try:
            params: dict[str, Any] = _rison.loads(q_raw) if q_raw else {}
        except Exception:  # noqa: BLE001
            try:
                params = json.loads(q_raw) if q_raw else {}
            except Exception:  # noqa: BLE001
                params = {}

        database_name: str = params.get("database_name") or ""
        if not database_name:
            return Response(
                content={"message": "database_name is required"},
                status_code=400,
            )
        table_name: str = params.get("table_name") or ""
        if not table_name:
            return Response(
                content={"message": "table_name is required"},
                status_code=400,
            )
        catalog_name: str | None = params.get("catalog_name")
        schema_name: str | None = params.get("schema_name") or None
        normalize_columns: bool = bool(params.get("normalize_columns", False))

        session = ds_dao.session  # type: ignore[attr-defined]

        # Try to find a tracked SqlaTable first
        orm_datasource = await _get_datasource_by_name(
            session=session,
            database_name=database_name,
            catalog=catalog_name,
            schema=schema_name,
            datasource_name=table_name,
        )

        if orm_datasource is not None:
            # Return column info from Superset metadata
            try:
                ext_meta = await asyncio.to_thread(orm_datasource.external_metadata)
                return Response(content=ext_meta, status_code=200)
            except SupersetException as exc:
                return Response(content={"message": str(exc)}, status_code=400)
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "external_metadata_by_name: external_metadata failed for %s.%s.%s",
                    database_name,
                    schema_name,
                    table_name,
                )
                return Response(content={"message": str(exc)}, status_code=400)

        # Fallback: use the SQLAlchemy inspector via the async connection
        # Mirrors original get_physical_table_metadata call when datasource is None.
        from sqlalchemy import select as sa_select

        from superset.models.core import Database

        db_stmt = sa_select(Database).where(Database.database_name == database_name)
        db_result = await session.execute(db_stmt)
        database = db_result.scalars().first()
        if database is None:
            return Response(
                content={"message": f'Database "{database_name}" not found.'},
                status_code=404,
            )

        table = Table(table_name, schema_name)
        try:
            meta = await _get_physical_table_metadata_async(
                database=database,
                table=table,
                normalize_columns=normalize_columns,
            )
        except Exception as exc:  # noqa: BLE001
            # Mirrors original: (NoResultFound, NoSuchTableError) → 404
            # (superset_old/views/datasource/views.py:190-191).
            return Response(
                content={"message": f"Table metadata error: {exc}"},
                status_code=404,
            )
        return Response(content=meta, status_code=200)
