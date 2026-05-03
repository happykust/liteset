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
# mypy: ignore-errors
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
# soft_time_limit resolution
# ---------------------------------------------------------------------------
# Mirrors the original ``query_timeout = current_app.config[
# "SQLLAB_ASYNC_TIME_LIMIT_SEC"]`` evaluated at module load: Celery binds
# ``soft_time_limit`` when the ``@celery_app.task`` decorator runs, so we
# need a value at import time.  We attempt to instantiate
# :class:`SupersetSettings` (which reads env / superset_config.py) and
# fall back to the same default the model declares (21600s = 6h) when
# the worker process imports this module before settings are present —
# matching the original which crashed at import on missing config but
# letting the module import cleanly in unit tests / worker bootstrap.
def _resolve_query_timeout() -> int:
    try:
        from superset.config import SupersetSettings

        settings = SupersetSettings()  # type: ignore[call-arg]
        return int(getattr(settings, "sqllab_async_time_limit_sec", 21600))
    except Exception:  # noqa: BLE001 — best-effort import-time resolution
        logger.debug(
            "Could not resolve sqllab_async_time_limit_sec at module import; "
            "falling back to default 21600s",
            exc_info=True,
        )
        return 21600


query_timeout = _resolve_query_timeout()


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

    logger.debug("Updated job %s on channel %s: status=%s", job_id, channel_id, status)


# ---------------------------------------------------------------------------
# Status constants (match original AsyncQueryManager)
# ---------------------------------------------------------------------------

STATUS_DONE = "done"
STATUS_ERROR = "error"


# ---------------------------------------------------------------------------
# User loading helpers
# ---------------------------------------------------------------------------


async def _load_user_from_job_metadata(
    job_metadata: dict[str, Any],
    async_session: Any,
    settings: Any,
) -> Any:
    """Resolve the request user from ``job_metadata``.

    Ports the original ``_load_user_from_job_metadata`` which dispatched
    on ``user_id`` (logged-in user via ``security_manager.get_user_by_id``)
    or ``guest_token`` (embedded guest user via
    ``security_manager.get_guest_user_from_token``), falling through to
    ``security_manager.get_anonymous_user()`` — an
    :class:`AnonymousUserMixin` instance with ``is_authenticated=False``.

    Runs against the live :class:`AsyncSession` so the returned object
    can be reused inside the same task pipeline (RLS, permissions, audit
    logging) without lazy-loading errors.

    Mutates ``job_metadata`` in place: the guest token is removed once
    consumed so it is not re-broadcast on the Redis status update,
    matching the original behaviour.
    """
    from superset.middleware.auth import UnauthenticatedUser
    from superset.security.dao import AsyncSecurityDAO

    dao = AsyncSecurityDAO(async_session)

    # 1. Logged-in user --------------------------------------------------
    if user_id := job_metadata.get("user_id"):
        user = await dao.get_user_by_id(user_id)
        if user is not None:
            return user

    # 2. Embedded guest user (JWT) --------------------------------------
    guest_token = job_metadata.pop("guest_token", None)
    if guest_token:
        guest = await _load_guest_user_from_token(guest_token, settings, dao)
        if guest is not None:
            return guest

    # 3. Anonymous — port of ``security_manager.get_anonymous_user()``.
    # The original returned a Flask-Login ``AnonymousUserMixin`` instance
    # (``is_authenticated=False``, ``is_anonymous=True``).  Liteset's
    # equivalent is :class:`UnauthenticatedUser`, which the auth
    # middleware (``_build_anonymous_user``) hands out for unauthenticated
    # HTTP requests; we reuse it here so that downstream code observes
    # the same shape regardless of whether it ran inside a request or
    # inside this Celery task.
    return UnauthenticatedUser()


async def _load_guest_user_from_token(
    guest_token: str,
    settings: Any,
    dao: Any,
) -> Any | None:
    """Decode a guest JWT token and build a :class:`GuestUser`.

    Mirrors :meth:`SupersetAuthMiddleware._resolve_guest_from_jwt`:

    1. Parse the token with the dedicated guest secret/algo if configured,
       otherwise the application secret key.
    2. Validate the required ``user`` / ``resources`` / ``rls_rules``
       claims.
    3. Look up the configured ``GUEST_ROLE_NAME`` role and merge its
       permissions into the resulting :class:`GuestUser` so RBAC checks
       behave identically to the live request path.

    Returns ``None`` when the token is invalid, expired, or missing
    required claims (anonymous-equivalent fall-through).
    """
    from superset.security.guest import GuestUser, parse_guest_token

    secret_key = getattr(settings, "guest_token_jwt_secret", "") or _resolve_secret(
        settings
    )
    algorithm = getattr(settings, "guest_token_jwt_algo", "HS256")

    audience_setting = getattr(settings, "guest_token_jwt_audience", None)
    if callable(audience_setting):
        audience = audience_setting() or ""
    else:
        audience = audience_setting or ""
    audience = str(audience) if audience else ""

    payload = parse_guest_token(
        guest_token,
        secret_key,
        algorithm=algorithm,
        audience=audience,
    )
    if payload is None:
        return None

    # Required claims (match middleware ``_resolve_guest_from_jwt``)
    if (
        payload.get("user") is None
        or payload.get("resources") is None
        or payload.get("rls_rules") is None
    ):
        logger.warning("Guest token missing required claims; ignoring")
        return None

    guest_user = GuestUser.from_token_payload(payload)

    role_name = getattr(settings, "guest_role_name", "Guest")
    try:
        role = await dao.get_role_by_name(role_name)
        if role is not None:
            role_perms = await dao.get_permissions_for_role_name(role_name)
            guest_user.permissions = guest_user.permissions | role_perms
    except Exception:
        logger.warning(
            "Failed to load Guest role '%s' for async query task",
            role_name,
            exc_info=True,
        )

    return guest_user


def _resolve_secret(settings: Any) -> str:
    """Return the application secret key as a plain string."""
    secret_key = getattr(settings, "secret_key", "")
    if hasattr(secret_key, "get_secret_value"):
        secret_key = secret_key.get_secret_value()
    return str(secret_key) if secret_key else ""


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


@celery_app.task(
    name="load_chart_data_into_cache",
    soft_time_limit=query_timeout,
)
def load_chart_data_into_cache(
    job_metadata: dict[str, Any],
    form_data: dict[str, Any],
) -> None:
    """Execute chart query and store result in cache.

    Ported from the original ``load_chart_data_into_cache`` task.
    Creates an AsyncQueryContext from form_data, runs the ChartDataCommand
    with ``cache=True``, and updates the job status via Redis Streams.
    """
    from superset.commands.chart.data.get_data_command import ChartDataCommand
    from superset.common.query_context_processor import AsyncQueryContextProcessor
    from superset.config import SupersetSettings
    from superset.db.session import get_sync_session
    from superset.security.dao import AsyncSecurityDAO
    from superset.security.manager import AsyncSecurityManager
    from superset.utils.core import (
        reset_form_data,
        set_current_user,
        set_form_data,
    )

    session = get_sync_session()
    # Bind form_data to the current task context so that
    # ``jinja_context.get_dataset_id_from_context`` (and any other
    # template helper that falls back to ``g.form_data`` in the
    # original) can resolve the dataset from this task's payload.
    form_data_token = set_form_data(form_data)
    try:
        settings = SupersetSettings()  # type: ignore[call-arg]

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

                ds = (
                    session.execute(sa_select(SqlaTable).where(SqlaTable.id == ds_id))
                    .scalars()
                    .one_or_none()
                )
                datasource = ds
            except (ValueError, IndexError):
                pass

        # Build the processor and command using async wrappers run
        # synchronously via asyncio.run().  User identity (regular user
        # or :class:`GuestUser`) is resolved inside the async block and
        # bound to the request-scoped :class:`ContextVar` so that RLS,
        # permissions and audit logging see the right principal — the
        # async port of the original ``override_user(...)`` wrapper.
        async def _execute() -> dict[str, Any]:
            from superset.db.session import create_session_factory, get_engine

            engine = get_engine()
            factory = create_session_factory(engine)
            async with factory() as async_session:
                user = await _load_user_from_job_metadata(
                    job_metadata, async_session, settings
                )
                set_current_user(user)
                dao = AsyncSecurityDAO(async_session)
                feature_flags = getattr(settings, "feature_flags", {})
                embedded_enabled = getattr(
                    settings, "embedded_superset", False
                ) or feature_flags.get("EMBEDDED_SUPERSET", False)
                sec_mgr = AsyncSecurityManager(
                    dao=dao,
                    embedded_superset_enabled=embedded_enabled,
                )
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
        reset_form_data(form_data_token)
        session.close()


@celery_app.task(
    name="load_explore_json_into_cache",
    soft_time_limit=query_timeout,
)
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
    from superset.utils.core import (
        reset_form_data,
        set_current_user,
        set_form_data,
    )
    from superset.utils.hashing import md5_sha_from_dict
    from superset.utils.json import json_int_dttm_ser

    cache_key_prefix = "ejr-"  # ejr: explore_json request

    session = get_sync_session()
    # Same rationale as ``load_chart_data_into_cache``: expose form_data
    # to template helpers (``g.form_data`` equivalent) for the duration
    # of this task, then reset on exit to prevent cross-task leakage.
    form_data_token = set_form_data(form_data)
    try:
        settings = SupersetSettings()  # type: ignore[call-arg]

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
            raise ValueError("The dataset associated with this chart no longer exists")

        # Load datasource from DB
        from sqlalchemy import select as sa_select

        from superset.models.connectors import SqlaTable

        datasource = (
            session.execute(sa_select(SqlaTable).where(SqlaTable.id == datasource_id))
            .scalars()
            .one_or_none()
        )

        if datasource is None:
            raise ValueError(
                f"Datasource {datasource_id} ({datasource_type}) not found"
            )

        # Deep copy form_data before viz modifies it
        original_form_data = copy.deepcopy(form_data)

        # Build viz object and run query (async, via asyncio.run).
        # Resolve user identity inside the async block (regular user via
        # ``user_id`` or :class:`GuestUser` via ``guest_token``) and bind
        # it to the request-scoped :class:`ContextVar` — the async port
        # of the original ``override_user(...)`` wrapper around the body.
        async def _execute() -> dict[str, Any]:
            from superset.db.session import create_session_factory, get_engine
            from superset.viz import get_viz as _get_viz

            engine = get_engine()
            factory = create_session_factory(engine)
            async with factory() as async_session:
                user = await _load_user_from_job_metadata(
                    job_metadata, async_session, settings
                )
                set_current_user(user)

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
            cache_timeout = getattr(settings, "cache_default_timeout", 300) or 300
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
        logger.warning("A timeout occurred while loading explore json, error: %s", ex)
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
        reset_form_data(form_data_token)
        session.close()
