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
"""Legacy ``/superset/explore_json`` controller (deprecated, eol 5.0.0).

The modern frontend uses ``/api/v1/chart/data``; this legacy surface is
preserved so existing installations / clients (e.g. deck.gl viz types and
older embeds that POST/GET ``form_data``) keep working.  Three routes:

* ``GET|POST /superset/explore_json/<datasource_type>/<datasource_id>/``
* ``GET|POST /superset/explore_json/``
* ``GET     /superset/explore_json/data/<cache_key>``

Control flow:

* ``explore_json`` resolves a ``response_type`` from the query string
  (``json`` default; ``csv``/``xlsx``/``query``/``results``/``samples``),
  checks the ``can_csv`` permission for CSV, resolves the datasource via
  :func:`get_datasource_info`, enforces datasource read access, and then:

  - GAQ enabled + ``json``: cache-first (return immediately if the chart
    query is already cached); otherwise resolve the channel from the
    ``async-token`` cookie (401 on miss) and submit the Celery job
    ``load_explore_json_into_cache`` returning ``202`` + ``job_metadata``.
  - otherwise (sync): build the viz, run the query, and ``generate_json``.

* ``explore_json_data`` loads the cached ``{form_data, response_type}``
  written by the Celery task, rebuilds the viz with ``force_cached=True``
  (a cache hit, no warehouse re-execution), and ``generate_json``.
"""

from __future__ import annotations

import json as _json
import logging
import uuid
from typing import Any, cast, TYPE_CHECKING

from litestar import Controller, get, post, Request
from litestar.datastructures import State
from litestar.exceptions import NotAuthorizedException
from litestar.response import Response
from sqlalchemy.ext.asyncio import AsyncSession

from superset.events import event_logger
from superset.exceptions import SupersetException
from superset.guards.rbac import require_permission
from superset.typing import SecurityManagerProtocol, UserProtocol

if TYPE_CHECKING:
    from superset.config import SupersetSettings
    from superset.viz import BaseViz

logger = logging.getLogger(__name__)

# Query-string flags that select a non-default ``response_type`` — the
# union of the original ``ChartDataResultFormat`` + ``ChartDataResultType``
# enum members (``request.args.get(<member>) == "true"``).  ``json`` is the
# default and is listed so an explicit ``?json=true`` is also honoured.
_RESPONSE_TYPE_FLAGS: tuple[str, ...] = (
    "csv",
    "xlsx",
    "json",
    "query",
    "results",
    "samples",
)


def get_datasource_info(
    datasource_id: int | None,
    datasource_type: str | None,
    form_data: dict[str, Any],
) -> tuple[int, str | None]:
    """Resolve ``(datasource_id, datasource_type)`` from URL args + form_data.

    The ``form_data["datasource"]`` ``"<id>__<type>"`` string takes precedence
    over URL-supplied values; ``"None__<type>"`` flags a deleted dataset.

    :raises SupersetException: if no datasource id can be determined.
    """
    datasource = form_data.get("datasource", "")
    if isinstance(datasource, str) and "__" in datasource:
        datasource_id_str, datasource_type = datasource.split("__")
        # The case where the datasource has been deleted.
        if datasource_id_str == "None":
            datasource_id = None
        else:
            datasource_id = int(datasource_id_str)

    if not datasource_id:
        raise SupersetException(
            "The dataset associated with this chart no longer exists"
        )

    return int(datasource_id), datasource_type


def _parse_request_form_data(form_data_str: str | bytes) -> dict[str, Any]:
    """Decode a ``form_data`` blob (the explore payload), returning ``{}``."""
    if isinstance(form_data_str, bytes):
        form_data_str = form_data_str.decode("utf-8", errors="replace")
    try:
        parsed = _json.loads(form_data_str)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def _extract_form_data(request: Request[Any, Any, Any]) -> dict[str, Any]:
    """Extract the explore ``form_data`` from a GET or POST request.

    Reads the body's ``form_data`` field (POST), or the ``form_data``
    query-string arg (GET), JSON-decoded.  The request-global form_data is not
    available in the async web path, so we read directly from the request.
    """
    form_data: dict[str, Any] = {}

    # POST: form-encoded body with a ``form_data=<json>`` field (the shape
    # Superset's explore/CSV-export buttons submit).
    try:
        form = await request.form()
        body_form_data = form.get("form_data")
        if body_form_data:
            form_data.update(_parse_request_form_data(body_form_data))
    except Exception:  # noqa: BLE001 — body may not be form-encoded
        logger.debug("explore_json: no form-encoded body", exc_info=True)

    # POST JSON body fallback (some clients send raw JSON).
    if not form_data and request.content_type and "json" in request.content_type[0]:
        try:
            body = await request.json()
            if isinstance(body, dict):
                form_data.update(body)
        except Exception:  # noqa: BLE001
            logger.debug("explore_json: body is not JSON", exc_info=True)

    # GET / query-string ``form_data`` overrides the body, matching the
    # original ``request.args.get("form_data")`` precedence.
    args_form_data = request.query_params.get("form_data")
    if args_form_data:
        form_data.update(_parse_request_form_data(args_form_data))

    return form_data


def _resolve_response_type(request: Request[Any, Any, Any]) -> str:
    """Resolve the ``response_type`` from the request query string.

    The first ``?<flag>=true`` wins; ``json`` is the default.
    """
    for flag in _RESPONSE_TYPE_FLAGS:
        if request.query_params.get(flag) == "true":
            return flag
    return "json"


async def _generate_json(
    viz_obj: BaseViz,
    response_type: str,
) -> Response[Any]:
    """Serialize the viz output per ``response_type``.

    Dispatches to CSV / query / results / samples handlers, defaulting to
    the full JSON payload.
    """
    if response_type == "csv":
        csv_str = await viz_obj.get_csv()
        from datetime import datetime

        filename = datetime.now().strftime("%Y%m%d_%H%M%S")
        return Response(
            content=csv_str,
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}.csv"},
        )

    if response_type == "query":
        query = None
        try:
            query_obj = viz_obj.query_obj()
            if query_obj:
                query = viz_obj.datasource.get_query_str(query_obj)
        except Exception as ex:  # noqa: BLE001
            logger.exception("explore_json query string error")
            return Response(
                content={"error": str(ex)},
                status_code=400,
                media_type="application/json",
            )
        if not query:
            query = "Query cannot be loaded."
        return Response(
            content={
                "query": query,
                "language": viz_obj.datasource.query_language,
            },
            media_type="application/json",
        )

    if response_type == "results":
        payload = await viz_obj.get_df_payload()
        if viz_obj.has_error(payload):
            return Response(
                content=viz_obj.json_dumps(payload),
                status_code=400,
                media_type="application/json",
            )
        import pandas as pd

        df = cast("pd.DataFrame | None", payload.get("df"))
        return Response(
            content=viz_obj.json_dumps(
                {
                    "data": df.to_dict("records") if df is not None else [],
                    "colnames": payload.get("colnames"),
                    "coltypes": payload.get("coltypes"),
                    "rowcount": payload.get("rowcount"),
                    "sql_rowcount": payload.get("sql_rowcount"),
                }
            ),
            media_type="application/json",
        )

    if response_type == "samples":
        samples = await viz_obj.get_samples()
        return Response(
            content=viz_obj.json_dumps(samples),
            media_type="application/json",
        )

    # Default: full JSON payload.  ``data_payload_response`` returns 400
    # when the payload carries an error, else 200.
    payload = await viz_obj.get_payload()
    payload_json, has_error = viz_obj.payload_json_and_has_error(payload)
    return Response(
        content=payload_json,
        status_code=400 if has_error else 200,
        media_type="application/json",
    )


class ExploreJsonController(Controller):
    """Legacy explore_json endpoints (deprecated, eol 5.0.0).

    Registered at the app root so the paths resolve at exactly
    ``/superset/explore_json/...``.  Guarded with ``can_read`` on ``Chart``
    (the same guard the modern ``/api/v1/chart/data`` endpoints use) plus an
    in-handler datasource read-access check.
    """

    path = "/"
    tags = ["Charts"]

    @post(
        [
            "/superset/explore_json/{datasource_type:str}/{datasource_id:int}/",
            "/superset/explore_json/",
        ],
        guards=[require_permission("can_read", "Chart")],
        status_code=200,
        opt={"skip_csrf": True},
    )
    async def explore_json_post(
        self,
        request: Request[Any, Any, Any],
        session: AsyncSession,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
        state: State,
        datasource_type: str | None = None,
        datasource_id: int | None = None,
    ) -> Response[Any]:
        """POST /superset/explore_json[/<type>/<id>]/."""
        return await self._explore_json(
            request,
            session,
            security_manager,
            current_user,
            state,
            datasource_type,
            datasource_id,
        )

    @get(
        [
            "/superset/explore_json/{datasource_type:str}/{datasource_id:int}/",
            "/superset/explore_json/",
        ],
        guards=[require_permission("can_read", "Chart")],
    )
    async def explore_json_get(
        self,
        request: Request[Any, Any, Any],
        session: AsyncSession,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
        state: State,
        datasource_type: str | None = None,
        datasource_id: int | None = None,
    ) -> Response[Any]:
        """GET /superset/explore_json[/<type>/<id>]/."""
        return await self._explore_json(
            request,
            session,
            security_manager,
            current_user,
            state,
            datasource_type,
            datasource_id,
        )

    async def _explore_json(  # noqa: C901, PLR0912, PLR0913
        self,
        request: Request[Any, Any, Any],
        session: AsyncSession,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
        state: State,
        datasource_type: str | None,
        datasource_id: int | None,
    ) -> Response[Any]:
        """Core ``explore_json`` handler (shared by GET and POST routes)."""
        settings = cast("SupersetSettings", getattr(state, "settings", None))

        response_type = _resolve_response_type(request)

        # Verify the user can export CSV.
        if response_type == "csv" and not await security_manager.can_access(
            "can_csv", "Superset", user=current_user
        ):
            return Response(
                content={"error": "You don't have the rights to download as csv"},
                status_code=403,
                media_type="application/json",
            )

        form_data = await _extract_form_data(request)

        try:
            datasource_id, datasource_type = get_datasource_info(
                datasource_id, datasource_type, form_data
            )
        except SupersetException as ex:
            return Response(
                content={"error": str(ex)},
                status_code=400,
                media_type="application/json",
            )

        force = request.query_params.get("force") == "true"

        # Filter REJECTED_FORM_DATA_KEYS when JS controls are disabled.
        from superset.utils.feature_flags import feature_flag_manager

        if not feature_flag_manager.is_feature_enabled("ENABLE_JAVASCRIPT_CONTROLS"):
            rejected = {"js_tooltip", "js_onclick_href", "js_data_mutator"}
            form_data = {k: v for k, v in form_data.items() if k not in rejected}

        # --- GLOBAL_ASYNC_QUERIES branch (JSON only) ---------------------
        if getattr(settings, "global_async_queries", False) and response_type == "json":
            datasource = await self._load_datasource(session, cast(int, datasource_id))
            if datasource is None:
                return Response(
                    content={"error": "Datasource not found"},
                    status_code=404,
                    media_type="application/json",
                )
            await self._raise_for_datasource_access(
                security_manager, current_user, datasource
            )

            # (a) Cache-first: return immediately if the query is cached.
            import contextlib

            from superset.cache.sync_viz_cache import build_sync_viz_cache
            from superset.exceptions import CacheLoadError
            from superset.viz import get_viz as make_viz

            with contextlib.suppress(CacheLoadError):
                viz_obj = make_viz(
                    datasource=datasource,
                    form_data=form_data,
                    force=force,
                    force_cached=True,
                    settings=settings,
                )
                # Read the DataFrame from the shared DATA_CACHE_CONFIG slot the
                # Celery worker wrote to; without it force_cached always misses.
                viz_obj.cache_manager = build_sync_viz_cache(
                    getattr(settings, "data_cache_config", None),
                    getattr(settings, "redis_url", None),
                )
                # RLS-differentiate the cache key (same value the worker wrote
                # for this user), so a cache hit only serves this user's
                # RLS-filtered result.
                try:
                    viz_obj._rls_cache_key = await security_manager.get_rls_cache_key(
                        datasource, user=current_user
                    )
                except Exception:  # noqa: BLE001 — key defaults to [] (safe)
                    logger.warning(
                        "Could not populate _rls_cache_key for explore_json",
                        exc_info=True,
                    )
                payload = await viz_obj.get_payload()
                # Deliberate deviation from the original (core.py:322-325 returned
                # the cached payload whenever it was non-None, incl. error payloads
                # as a 400): for the async transport we only short-circuit on a
                # *successful* cached payload and otherwise fall through to submit
                # a fresh job, so a stale cached error isn't served instead of a retry.
                if payload is not None and payload.get("status") != "failed":
                    payload_json, has_error = viz_obj.payload_json_and_has_error(
                        payload
                    )
                    if not has_error:
                        return Response(
                            content=payload_json,
                            status_code=200,
                            media_type="application/json",
                        )

            # (b) Otherwise submit a background job.  The channel id MUST
            # come from the request's ``async-token`` cookie — the same
            # claim the polling endpoint and the WebSocket relay read from.
            # A missing / invalid cookie maps to 401, matching the original
            # ``AsyncQueryTokenException`` → ``response_401``.
            from superset.async_events.manager import (
                build_job_metadata,
                maybe_forward_guest_token,
            )
            from superset.middleware.async_token import (
                resolve_async_channel_id_from_request,
            )
            from superset.tasks.async_queries import load_explore_json_into_cache

            channel_id = resolve_async_channel_id_from_request(request, settings)
            if not channel_id:
                raise NotAuthorizedException(detail="Not authorized")

            job_id = str(uuid.uuid4())
            job_metadata = build_job_metadata(
                channel_id=channel_id,
                job_id=job_id,
                user_id=getattr(current_user, "id", None),
                status="pending",
            )
            # For an embedded guest user, forward the raw guest JWT so the
            # worker reconstructs the same GuestUser (hence the same RLS cache
            # key), keeping the key consistent between submit and the data fetch.
            # Only the *dispatched* metadata carries the token — the 202 response
            # returns the clean ``job_metadata`` (the token is never echoed back).
            dispatch_metadata = await maybe_forward_guest_token(
                job_metadata,
                request=request,
                settings=settings,
                security_manager=security_manager,
                current_user=current_user,
            )
            load_explore_json_into_cache.delay(
                dispatch_metadata, form_data, response_type, force
            )
            await event_logger.alog_with_context(
                "explore_json", object_ref=f"datasource:{datasource_id}"
            )
            return Response(content=job_metadata, status_code=202)

        # --- Synchronous branch ------------------------------------------
        datasource = await self._load_datasource(session, cast(int, datasource_id))
        if datasource is None:
            return Response(
                content={"error": "Datasource not found"},
                status_code=404,
                media_type="application/json",
            )
        await self._raise_for_datasource_access(
            security_manager, current_user, datasource
        )

        from superset.viz import get_viz as make_viz

        try:
            viz_obj = make_viz(
                datasource=datasource,
                form_data=form_data,
                force=force,
                settings=settings,
            )
            result = await _generate_json(viz_obj, response_type)
        except SupersetException as ex:
            return Response(
                content={"error": str(ex)},
                status_code=400,
                media_type="application/json",
            )
        await event_logger.alog_with_context(
            "explore_json", object_ref=f"datasource:{datasource_id}"
        )
        return result

    @get(
        "/superset/explore_json/data/{cache_key:str}",
        guards=[require_permission("can_read", "Chart")],
    )
    async def explore_json_data(
        self,
        cache_key: str,
        session: AsyncSession,
        security_manager: SecurityManagerProtocol,
        current_user: UserProtocol,
        state: State,
    ) -> Response[Any]:
        """GET /superset/explore_json/data/<cache_key> — serve cached result.

        Loads the cached ``{form_data, response_type}`` written by
        ``load_explore_json_into_cache``, rebuilds the viz with
        ``force_cached=True`` (cache hit, no warehouse re-execution), and
        calls ``generate_json``.
        """
        from superset.common.query_context_processor import load_cached_explore_form
        from superset.extensions import cache_manager

        settings = cast("SupersetSettings", getattr(state, "settings", None))

        cached = await load_cached_explore_form(
            getattr(cache_manager, "cache", None), cache_key
        )
        if not cached:
            return Response(
                content={"error": "Cached data not found"},
                status_code=404,
                media_type="application/json",
            )

        form_data = cached.get("form_data") or {}
        response_type = cached.get("response_type") or "json"

        try:
            datasource_id, datasource_type = get_datasource_info(None, None, form_data)
        except SupersetException as ex:
            return Response(
                content={"error": str(ex)},
                status_code=400,
                media_type="application/json",
            )

        datasource = await self._load_datasource(session, datasource_id)
        if datasource is None:
            return Response(
                content={"error": "Datasource not found"},
                status_code=404,
                media_type="application/json",
            )
        await self._raise_for_datasource_access(
            security_manager, current_user, datasource
        )

        from superset.cache.sync_viz_cache import build_sync_viz_cache
        from superset.viz import get_viz as make_viz

        try:
            viz_obj = make_viz(
                datasource=datasource,
                form_data=form_data,
                force_cached=True,
                settings=settings,
            )
            # Serve the DataFrame the worker cached in the shared
            # DATA_CACHE_CONFIG slot (force_cached → no warehouse re-execution).
            viz_obj.cache_manager = build_sync_viz_cache(
                getattr(settings, "data_cache_config", None),
                getattr(settings, "redis_url", None),
            )
            # RLS-differentiate the cache key so a user only reads their own
            # RLS-filtered cached result (matches the key the worker wrote).
            try:
                viz_obj._rls_cache_key = await security_manager.get_rls_cache_key(
                    datasource, user=current_user
                )
            except Exception:  # noqa: BLE001 — key defaults to [] (safe)
                logger.warning(
                    "Could not populate _rls_cache_key for explore_json_data",
                    exc_info=True,
                )
            result = await _generate_json(viz_obj, response_type)
        except SupersetException as ex:
            return Response(
                content={"error": str(ex)},
                status_code=400,
                media_type="application/json",
            )
        await event_logger.alog_with_context(
            "explore_json_data", object_ref=f"cache:{cache_key}"
        )
        return result

    @staticmethod
    async def _load_datasource(
        session: AsyncSession,
        datasource_id: int,
    ) -> Any | None:
        """Load a ``SqlaTable`` with database/columns/metrics eager-loaded."""
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        from superset.models.connectors import SqlaTable

        stmt = (
            select(SqlaTable)
            .where(SqlaTable.id == int(datasource_id))
            .options(
                selectinload(SqlaTable.database),
                selectinload(SqlaTable.columns),
                selectinload(SqlaTable.metrics),
            )
        )
        result = await session.execute(stmt)
        return result.scalars().one_or_none()

    @staticmethod
    async def _raise_for_datasource_access(
        security_manager: SecurityManagerProtocol,
        user: UserProtocol,
        datasource: Any,
    ) -> None:
        """Enforce datasource read access via the security manager."""
        await security_manager.raise_for_access(user=user, datasource=datasource)
