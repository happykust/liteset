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

import copy
import logging
from typing import Any

import pandas as pd
from litestar import Controller, Request, post
from litestar.di import Provide
from litestar.response import Response

from superset.common.query_context import AsyncQueryContext
from superset.common.query_context_processor import AsyncQueryContextProcessor
from superset.common.query_object import AsyncQueryObject
from superset.guards.rbac import require_authentication
from superset.providers import provide_datasource_dao
from superset.typing import DatasourceDAOProtocol, UserProtocol

logger = logging.getLogger(__name__)


_VALID_DATASOURCE_TYPES = {"table", "query", "saved_query", "view", "dataset"}


def _replace_verbose_with_column(
    filters: list[dict[str, Any]],
    datasource: Any,
) -> None:
    """Rewrite filter ``col`` values from verbose name → physical column name.

    Mirrors ``superset_old/views/datasource/utils.py::replace_verbose_with_column``.
    The frontend may send either the physical column name or the verbose
    label; the query builder only understands the physical name.
    """
    if not filters:
        return
    columns = getattr(datasource, "columns", None) or []
    if not columns:
        return

    is_virtual = getattr(datasource, "sql", None) is not None
    column_attr = "column_name" if not is_virtual else "label"
    verbose_attr = "verbose_name" if not is_virtual else "column_name"

    for flt in filters:
        col_value = flt.get("col")
        if not isinstance(col_value, str):
            continue
        # If already a physical name, leave alone.
        if any(getattr(c, column_attr, None) == col_value for c in columns):
            continue
        for col in columns:
            if getattr(col, verbose_attr, None) == col_value:
                match = getattr(col, column_attr, None)
                if match:
                    flt["col"] = match
                break


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
        page = 1
    if page < 1:
        page = 1

    per_page_raw = qp.get("per_page")
    try:
        per_page = int(per_page_raw) if per_page_raw is not None else 1000
    except (TypeError, ValueError):
        per_page = 1000
    if per_page < 1 or per_page > 100_000:
        per_page = max(1, min(per_page, 100_000))

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
) -> dict[str, Any] | None:
    """Parse the optional JSON body (drill-detail filters, time_range, extras).

    Returns None if the body is missing or not a dict. Unknown keys are
    ignored — matches SamplesPayloadSchema semantics.
    """
    try:
        raw = await request.json()
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(raw, dict):
        return None
    payload: dict[str, Any] = {}
    if isinstance(raw.get("filters"), list):
        payload["filters"] = list(raw["filters"])
    if "granularity" in raw:
        payload["granularity"] = raw.get("granularity")
    if "time_range" in raw:
        payload["time_range"] = raw.get("time_range")
    if isinstance(raw.get("extras"), dict):
        payload["extras"] = raw.get("extras")
    return payload or None


class LegacyDatasourceController(Controller):
    """Legacy ``/datasource`` routes ported from the original FAB view."""

    path = "/datasource"
    tags = ["Legacy", "Datasource"]
    dependencies = {
        "ds_dao": Provide(provide_datasource_dao, sync_to_thread=False),
    }

    @post(
        "/samples",
        guards=[require_authentication],
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
            sec_mgr.is_guest_user(user)
            if hasattr(sec_mgr, "is_guest_user")
            else False
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
                    "message": (
                        f'Datasource "{params["datasource_id"]}" not found.'
                    ),
                },
                status_code=404,
            )

        if is_guest:
            # Embedded dashboard drill-to-detail permission check.
            dashboard_id = params.get("dashboard_id")
            if not dashboard_id:
                return Response(content={"message": "Forbidden"}, status_code=403)
            if hasattr(sec_mgr, "can_drill_dataset_via_dashboard_access"):
                allowed = await sec_mgr.can_drill_dataset_via_dashboard_access(
                    datasource, dashboard_id, user=user
                )
                if not allowed:
                    return Response(
                        content={"message": "Forbidden"}, status_code=403
                    )
        else:
            # Regular datasource-access check (mirrors raise_for_access).
            if hasattr(sec_mgr, "raise_for_access"):
                try:
                    await sec_mgr.raise_for_access(
                        datasource=datasource, user=user
                    )
                except Exception as exc:  # noqa: BLE001
                    return Response(
                        content={"message": str(exc)}, status_code=403
                    )

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
        offset = (page - 1) * per_page

        samples_qo = AsyncQueryObject(
            datasource={
                "type": datasource.type,
                "id": datasource.id,
            },
            metrics=[],
            columns=[],
            filters=list(payload.get("filters", [])) if payload else [],
            row_limit=per_page,
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
            result_type=(
                "drill_detail"
                if payload and payload.get("filters")
                else "samples"
            ),
        )

        processor = AsyncQueryContextProcessor(
            datasource=datasource,
            settings=settings_obj,
            security_manager=sec_mgr,
            user=user,
            query_context=samples_context,
        )

        try:
            sample_result = await processor.get_payload(
                query_objects=[samples_qo], force=force
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to fetch samples for datasource")
            return Response(
                content={
                    "errors": [
                        {
                            "message": f"Failed to fetch samples: {exc}",
                            "error_type": "GENERIC_BACKEND_ERROR",
                            "level": "error",
                            "extra": {},
                        }
                    ],
                },
                status_code=400,
            )

        sample_data = (sample_result.get("queries") or [{}])[0]
        if isinstance(sample_data.get("df"), pd.DataFrame):
            df = sample_data.pop("df")
            sample_data["data"] = df.to_dict(orient="records")
            sample_data["colnames"] = list(df.columns)
            sample_data.setdefault("coltypes", [])
            sample_data["rowcount"] = len(sample_data["data"])

        # --- Run a separate count(*) query ---------------------------------
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
        total_count = sample_data.get("rowcount", 0)
        try:
            count_result = await count_processor.get_payload(
                query_objects=[count_qo], force=force
            )
            count_q = (count_result.get("queries") or [{}])[0]
            if isinstance(count_q.get("df"), pd.DataFrame):
                df = count_q["df"]
                if not df.empty:
                    first = df.iloc[0].to_dict()
                    for k in ("COUNT(*)", "count", "count_star"):
                        if k in first:
                            total_count = int(first[k])
                            break
            elif isinstance(count_q.get("data"), list) and count_q["data"]:
                row = count_q["data"][0]
                for k in ("COUNT(*)", "count", "count_star"):
                    if k in row:
                        total_count = int(row[k])
                        break
        except Exception:  # noqa: BLE001
            logger.warning(
                "Count query failed for datasource %s", datasource.id, exc_info=True
            )

        sample_data["page"] = page
        sample_data["per_page"] = per_page
        sample_data["total_count"] = total_count
        sample_data.setdefault("status", "success")

        return Response(content={"result": sample_data}, status_code=200)
