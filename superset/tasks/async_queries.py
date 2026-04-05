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
"""Async query execution Celery tasks for Superset.

Ported 1:1 from the original ``superset/tasks/async_queries.py``.
Uses :func:`superset.db.session.get_sync_session` for synchronous DB
access inside Celery workers, and ``redis.Redis`` (sync) for job status
updates via Redis Streams (matching AsyncEventManager's data model).
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import pickle  # noqa: S403 — required for cache compat with original Superset
from typing import Any, cast

from celery.exceptions import SoftTimeLimitExceeded

from superset.exceptions import SupersetVizException
from superset.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Redis Streams sync helpers (mirrors AsyncEventManager.update_job)
# ---------------------------------------------------------------------------

_sync_redis: Any = None


def _get_sync_redis() -> Any:
    """Lazily create a sync Redis client matching the configured redis_url."""
    global _sync_redis  # noqa: PLW0603
    if _sync_redis is None:
        import redis

        from superset.config import SupersetSettings

        settings = SupersetSettings()  # type: ignore[call-arg]
        redis_url = settings.redis_url
        if not redis_url:
            raise RuntimeError(
                "redis_url is not configured; cannot update async query job status"
            )
        _sync_redis = redis.Redis.from_url(redis_url)
    return _sync_redis


def _update_job(
    job_metadata: dict[str, Any],
    status: str,
    *,
    errors: list[dict[str, Any]] | None = None,
    result_url: str | None = None,
) -> None:
    """Update async job status via Redis Streams (sync).

    Writes to both the channel-specific stream and the global firehose
    stream, matching the data model used by
    :class:`superset.async_events.manager.AsyncEventManager`.
    """
    r = _get_sync_redis()
    stream_prefix = "async-events-"
    global_stream_key = "async-events-full"

    channel_id = job_metadata.get("channel_id", "")
    job_id = job_metadata.get("job_id", "")
    user_id = job_metadata.get("user_id")

    event = {
        "channel_id": channel_id,
        "job_id": job_id,
        "user_id": user_id,
        "status": status,
        "errors": errors or [],
        "result_url": result_url,
    }
    payload = {"data": json.dumps(event)}

    scoped_stream = f"{stream_prefix}{channel_id}"
    r.xadd(scoped_stream, payload)
    r.xadd(global_stream_key, payload)

    # Publish notification for WebSocket relay
    if user_id is not None:
        r.publish(f"events:{user_id}", json.dumps(event))

    logger.debug(
        "Updated job %s on channel %s: status=%s", job_id, channel_id, status
    )


# ---------------------------------------------------------------------------
# Status constants (match original AsyncQueryManager)
# ---------------------------------------------------------------------------

STATUS_DONE = "done"
STATUS_ERROR = "error"


# ---------------------------------------------------------------------------
# User loading helper
# ---------------------------------------------------------------------------


def _load_user_from_job_metadata(
    job_metadata: dict[str, Any],
    session: Any,
) -> Any:
    """Load user from job_metadata using a sync SQLAlchemy session.

    Mirrors the original ``_load_user_from_job_metadata`` which used
    ``security_manager.get_user_by_id()`` / ``get_anonymous_user()``.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from superset.models.security import User

    if user_id := job_metadata.get("user_id"):
        stmt = (
            select(User)
            .where(User.id == user_id)
            .options(selectinload(User.roles))
        )
        user = session.execute(stmt).scalars().one_or_none()
        if user is not None:
            return user

    # Guest token handling: the original deleted the token from metadata
    # and called security_manager.get_guest_user_from_token(). In Liteset
    # the guest user mechanism is not yet implemented in the sync path,
    # so we fall through to anonymous.
    if "guest_token" in job_metadata:
        del job_metadata["guest_token"]

    # Return None (anonymous); caller should handle accordingly.
    return None


# ---------------------------------------------------------------------------
# QueryContext construction helper
# ---------------------------------------------------------------------------


def _create_query_context_from_form(form_data: dict[str, Any]) -> Any:
    """Deserialize form_data into an AsyncQueryContext.

    The original used ``ChartDataQueryContextSchema().load(form_data)``
    (Marshmallow). In Liteset the QueryContext is a dataclass; we build
    it from form_data using the same deserialization path as the
    controller layer.
    """
    from superset.common.query_context import AsyncQueryContext
    from superset.common.query_object import AsyncQueryObject

    # Build a datasource dict from the top-level "datasource" field.
    # The AsyncQueryObject.datasource is a required dict[str, Any],
    # typically {"id": N, "type": "table"}.
    ds_ref = form_data.get("datasource", "")
    datasource_dict: dict[str, Any] = {}
    if isinstance(ds_ref, str) and "__" in ds_ref:
        parts = ds_ref.split("__")
        try:
            datasource_dict = {"id": int(parts[0]), "type": parts[1]}
        except (ValueError, IndexError):
            datasource_dict = {"id": 0, "type": "table"}
    elif isinstance(ds_ref, dict):
        datasource_dict = ds_ref

    queries_raw = form_data.get("queries") or []
    queries = []
    for q in queries_raw:
        # Use per-query datasource if present, else top-level
        q_ds = q.get("datasource", datasource_dict)
        if isinstance(q_ds, str) and "__" in q_ds:
            q_parts = q_ds.split("__")
            try:
                q_ds = {"id": int(q_parts[0]), "type": q_parts[1]}
            except (ValueError, IndexError):
                q_ds = datasource_dict

        queries.append(
            AsyncQueryObject(
                datasource=q_ds if isinstance(q_ds, dict) else datasource_dict,
                columns=q.get("columns") or q.get("groupby") or [],
                metrics=q.get("metrics") or [],
                filters=q.get("filters") or [],
                orderby=q.get("orderby") or [],
                row_limit=q.get("row_limit"),
                row_offset=q.get("row_offset", 0),
                time_range=q.get("time_range"),
                granularity=q.get("granularity") or q.get("granularity_sqla"),
                extras=q.get("extras") or {},
                post_processing=q.get("post_processing") or [],
                time_offsets=q.get("time_offsets") or [],
                order_desc=q.get("order_desc", True),
                series_columns=q.get("series_columns") or [],
                series_limit=q.get("series_limit", 0),
                series_limit_metric=q.get("series_limit_metric"),
                is_timeseries=q.get("is_timeseries", False),
                annotation_layers=q.get("annotation_layers") or [],
            )
        )

    return AsyncQueryContext(
        datasource=form_data.get("datasource"),
        queries=queries,
        form_data=form_data,
        force=form_data.get("force", False),
        custom_cache_timeout=form_data.get("custom_cache_timeout"),
        result_type=form_data.get("result_type"),
        result_format=form_data.get("result_format"),
    )


# ---------------------------------------------------------------------------
# Celery tasks
# ---------------------------------------------------------------------------


@celery_app.task(name="superset.tasks.async_queries.load_chart_data_into_cache")
def load_chart_data_into_cache(
    job_metadata: dict[str, Any],
    form_data: dict[str, Any],
) -> None:
    """Execute chart query and store result in cache.

    Ported from the original ``load_chart_data_into_cache`` task.
    Creates an AsyncQueryContext from form_data, runs the ChartDataCommand
    with ``cache=True``, and updates the job status via Redis Streams.
    """
    from superset.commands.chart_data import ChartDataCommand
    from superset.common.query_context_processor import AsyncQueryContextProcessor
    from superset.config import SupersetSettings
    from superset.db.session import get_sync_session
    from superset.security.dao import AsyncSecurityDAO
    from superset.security.manager import AsyncSecurityManager

    session = get_sync_session()
    try:
        settings = SupersetSettings()  # type: ignore[call-arg]
        user = _load_user_from_job_metadata(job_metadata, session)

        query_context = _create_query_context_from_form(form_data)

        # Resolve the datasource from the form_data "datasource" string
        # e.g. "1__table" -> datasource_id=1, datasource_type="table"
        datasource_ref = form_data.get("datasource", "")
        datasource = None
        if isinstance(datasource_ref, str) and "__" in datasource_ref:
            parts = datasource_ref.split("__")
            try:
                ds_id = int(parts[0])
                from sqlalchemy import select as sa_select

                from superset.models.connectors import SqlaTable

                ds = session.execute(
                    sa_select(SqlaTable).where(SqlaTable.id == ds_id)
                ).scalars().one_or_none()
                datasource = ds
            except (ValueError, IndexError):
                pass

        # Build the processor and command using async wrappers run
        # synchronously via asyncio.run()
        async def _execute() -> dict[str, Any]:
            from superset.db.session import create_session_factory, get_engine

            engine = get_engine()
            factory = create_session_factory(engine)
            async with factory() as async_session:
                dao = AsyncSecurityDAO(async_session)
                sec_mgr = AsyncSecurityManager(dao=dao)
                processor = AsyncQueryContextProcessor(
                    datasource=datasource,
                    settings=settings,
                    security_manager=sec_mgr,
                    user=user,
                    query_context=query_context,
                )
                command = ChartDataCommand(
                    query_context=query_context,
                    processor=processor,
                )
                await command.validate()
                return await command.run()

        result = asyncio.run(_execute())
        cache_key = result.get("cache_key", "")
        result_url = f"/api/v1/chart/data/{cache_key}"
        _update_job(
            job_metadata,
            STATUS_DONE,
            result_url=result_url,
        )
    except SoftTimeLimitExceeded as ex:
        logger.warning("A timeout occurred while loading chart data, error: %s", ex)
        raise
    except Exception as ex:
        # TODO: QueryContext should support SIP-40 style errors
        error = str(ex.message if hasattr(ex, "message") else ex)  # type: ignore[union-attr]
        errors = [{"message": error}]
        _update_job(job_metadata, STATUS_ERROR, errors=errors)
        raise
    finally:
        session.close()


@celery_app.task(name="superset.tasks.async_queries.load_explore_json_into_cache")
def load_explore_json_into_cache(  # noqa: C901
    job_metadata: dict[str, Any],
    form_data: dict[str, Any],
    response_type: str | None = None,
    force: bool = False,
) -> None:
    """Load explore JSON data into cache for async retrieval.

    Ported from the original ``load_explore_json_into_cache`` task.
    Creates a viz object from form_data, calls ``get_payload()``,
    caches the original form_data for later retrieval, and updates
    the job status via Redis Streams.
    """
    from superset.config import SupersetSettings
    from superset.db.session import get_sync_session
    from superset.utils.hashing import md5_sha_from_dict
    from superset.utils.json import json_int_dttm_ser

    cache_key_prefix = "ejr-"  # ejr: explore_json request

    session = get_sync_session()
    try:
        settings = SupersetSettings()  # type: ignore[call-arg]
        _load_user_from_job_metadata(job_metadata, session)

        # Resolve datasource from form_data
        datasource_ref = form_data.get("datasource", "")
        datasource_id: int | None = None
        datasource_type: str | None = None
        if isinstance(datasource_ref, str) and "__" in datasource_ref:
            parts = datasource_ref.split("__")
            try:
                datasource_id = int(parts[0])
                datasource_type = parts[1]
            except (ValueError, IndexError):
                pass

        if datasource_id is None:
            raise ValueError(
                "The dataset associated with this chart no longer exists"
            )

        # Load datasource from DB
        from sqlalchemy import select as sa_select

        from superset.models.connectors import SqlaTable

        datasource = session.execute(
            sa_select(SqlaTable).where(SqlaTable.id == datasource_id)
        ).scalars().one_or_none()

        if datasource is None:
            raise ValueError(
                f"Datasource {datasource_id} ({datasource_type}) not found"
            )

        # Deep copy form_data before viz modifies it
        original_form_data = copy.deepcopy(form_data)

        # Build viz object and run query (async, via asyncio.run)
        async def _execute() -> dict[str, Any]:
            from superset.viz import get_viz as _get_viz

            viz_obj = _get_viz(
                datasource=datasource,
                form_data=form_data,
                force=force,
                settings=settings,
            )
            payload = await viz_obj.get_payload()
            if viz_obj.has_error(payload):
                raise SupersetVizException(errors=payload.get("errors", []))
            return cast("dict[str, Any]", payload)

        asyncio.run(_execute())

        # Cache the original form_data value for async retrieval
        cache_value = {
            "form_data": original_form_data,
            "response_type": response_type,
        }
        hash_str = md5_sha_from_dict(cache_value, default=json_int_dttm_ser)
        cache_key = f"{cache_key_prefix}{hash_str}"

        # Store in cache using sync Redis.
        # Uses pickle for compatibility with the original flask-caching
        # Redis backend which serializes cached values as pickle bytes.
        try:
            r = _get_sync_redis()
            cache_timeout = (
                getattr(settings, "cache_default_timeout", 300) or 300
            )
            r.setex(
                f"superset_cache:{cache_key}",
                cache_timeout,
                pickle.dumps(cache_value),  # noqa: S301
            )
        except Exception:
            logger.warning(
                "Failed to store explore json in cache for key %s",
                cache_key,
                exc_info=True,
            )

        result_url = f"/superset/explore_json/data/{cache_key}"
        _update_job(
            job_metadata,
            STATUS_DONE,
            result_url=result_url,
        )
    except SoftTimeLimitExceeded as ex:
        logger.warning(
            "A timeout occurred while loading explore json, error: %s", ex
        )
        raise
    except Exception as ex:
        if isinstance(ex, SupersetVizException):
            errors = ex.errors
        else:
            error = ex.message if hasattr(ex, "message") else str(ex)  # type: ignore[union-attr]
            errors = [error]  # type: ignore[list-item]

        _update_job(job_metadata, STATUS_ERROR, errors=errors)
        raise
    finally:
        session.close()
