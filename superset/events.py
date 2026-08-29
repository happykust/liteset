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
"""Audit event logging for Superset.

Replaces Flask's request-scoped ``g.user`` / ``request`` /
``has_request_context`` primitives with ``ContextVar``-based equivalents
defined in :mod:`superset.utils.core` (``get_current_user``,
``get_logs_context``) and Litestar-shaped requests passed in explicitly.

Class hierarchy:

* :class:`EventLogger` (``AbstractEventLogger`` in the original) — the
  abstract base class that drives every event-logger implementation.
  Implements the full ``log_with_context`` / ``log_context`` /
  ``log_this`` / ``log_this_with_context`` /
  ``log_this_with_extra_payload`` / ``curate_payload`` /
  ``curate_form_data`` / ``collect_request_payload`` /
  ``get_logger_from_status`` API.
* :class:`StdOutEventLogger` — prints every event to stdout.
* :class:`AsyncDBEventLogger` (``DBEventLogger`` in the original) —
  persists ``Log`` rows to the metadata DB via a fire-and-forget
  ``asyncio.create_task`` so audit logging never blocks the request
  loop.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
import logging
import textwrap
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, cast, Literal, TYPE_CHECKING

from sqlalchemy import insert
from sqlalchemy.exc import SQLAlchemyError

from superset.utils.core import (
    get_current_request,
    get_current_user,
    LoggerLevel,
    to_int,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

logger = logging.getLogger(__name__)


# Upstream constant — used to strip the legacy raw-rison query
# parameter from the audit payload (the rison-decoded version is also
# present, and we never want both).  The original imported this from
# the upstream constants module; we reproduce the literal value here so
# the Liteset port can drop that dependency entirely.
API_URI_RIS_KEY = "q"

# INTENTIONAL, security-positive divergence from upstream Superset (which
# persists the full raw request body to ``logs.json`` unfiltered —
# ``superset_old/utils/log.py`` builds ``records`` straight from
# ``collect_request_payload()`` with no redaction, and this port inherited
# that as-is). A database create/update body carries the inline URI
# password, the real (unmasked) ``masked_encrypted_extra``
# (BigQuery/GSheets service-account keys, Snowflake keys, OAuth2
# ``client_secret``), ``server_cert``, and ``ssh_tunnel.password`` /
# ``.private_key`` — all of which would otherwise be written unencrypted to
# the ``Log`` table indefinitely, readable by any holder of ``can_read Log``,
# and echoed to stdout under ``StdOutEventLogger``. This denylist is applied
# to every persisted/printed audit record (see ``_scrub_sensitive_payload``
# below), not just database endpoints, since ``collect_request_payload``
# merges the *entire* request body/query-string generically for any action.
_SENSITIVE_PAYLOAD_KEYS: frozenset[str] = frozenset(
    {
        "sqlalchemy_uri",
        "password",
        "masked_encrypted_extra",
        "encrypted_extra",
        "server_cert",
        "ssh_tunnel",
    }
)


def _scrub_sensitive_payload(record: dict[str, Any]) -> dict[str, Any]:
    """Drop credential-bearing keys before a record is persisted or printed.

    Shallow by design — ``_SENSITIVE_PAYLOAD_KEYS`` targets the top-level
    request-body fields ``collect_request_payload`` merges in wholesale
    (``sqlalchemy_uri``, ``password``, ``masked_encrypted_extra``,
    ``encrypted_extra``, ``server_cert``, ``ssh_tunnel``); it is not a
    general-purpose redactor.
    """
    if not any(key in record for key in _SENSITIVE_PAYLOAD_KEYS):
        return record
    return {k: v for k, v in record.items() if k not in _SENSITIVE_PAYLOAD_KEYS}


class EventLogger(ABC):
    """Abstract event logger.

    Named :class:`EventLogger` for consistency with prior Liteset code;
    ``AbstractEventLogger`` is a re-export alias in :mod:`superset.utils.log`.
    """

    curated_payload_params: set[str] = {
        "force",
        "standalone",
        "runAsync",
        "json",
        "csv",
        "queryLimit",
        "select_as_cta",
    }

    curated_form_data_params: set[str] = {
        "dashboardId",
        "sliceId",
        "viz_type",
        "force",
        "compare_lag",
        "forecastPeriods",
        "granularity_sqla",
        "legendType",
        "legendOrientation",
        "show_legend",
        "time_grain_sqla",
    }

    def __call__(
        self,
        action: str,
        object_ref: str | None = None,
        log_to_statsd: bool = True,
        duration: timedelta | None = None,
        **payload_override: Any,
    ) -> EventLogger:
        # pylint: disable=W0201
        self.action = action
        self.object_ref = object_ref
        self.log_to_statsd = log_to_statsd
        self.duration = duration
        self.payload_override = payload_override
        return self

    def __enter__(self) -> None:
        # pylint: disable=W0201
        self.start = datetime.now()

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.log_with_context(
            action=getattr(self, "action", "event"),
            object_ref=getattr(self, "object_ref", None),
            log_to_statsd=getattr(self, "log_to_statsd", True),
            duration=datetime.now() - self.start,
            **getattr(self, "payload_override", {}),
        )

    @classmethod
    def curate_payload(cls, payload: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in payload.items() if k in cls.curated_payload_params}

    @classmethod
    def curate_form_data(cls, payload: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in payload.items() if k in cls.curated_form_data_params}

    async def collect_request_payload(  # noqa: C901  # complex business logic
        self, request: Any | None = None
    ) -> dict[str, Any]:
        """Build the audit payload from a Litestar :class:`Request`.

        Async equivalent of the original
        ``collect_request_payload``::

            payload = {
                "path": request.path,
                **request.form.to_dict(),
                **request.args.to_dict(),
            }
            if request.is_json:
                payload.update(request.get_json(...))

        Litestar's ``form()`` / ``json()`` accessors are coroutines, so
        we must be async here.  When ``request is None`` (Celery task,
        CLI, no bound HTTP request) the original returned ``{}`` — we
        mirror that contract.
        """
        if request is None:
            return {}

        payload: dict[str, Any] = {}

        url = getattr(request, "url", None)
        if url is not None:
            payload["path"] = getattr(url, "path", str(url))
        elif hasattr(request, "path"):
            payload["path"] = request.path

        # Litestar exposes ``request.form()`` as a coroutine; tests that
        # pass plain dict-shaped objects may instead expose
        # ``request.form`` as a property returning a dict.  Handle both.
        form_attr = getattr(request, "form", None)
        if callable(form_attr):
            try:
                form = await form_attr()
                if hasattr(form, "items"):
                    for key, value in form.items():
                        payload[key] = value
            except Exception:  # noqa: BLE001, S110
                pass
        elif form_attr is not None and hasattr(form_attr, "items"):
            for key, value in form_attr.items():
                payload[key] = value

        # query params overwrite POST body, mirroring ``request.args.to_dict()``
        query_params = getattr(request, "query_params", None)
        if query_params:
            try:
                multi_items = getattr(query_params, "multi_items", None)
                if callable(multi_items):
                    qp_dict: dict[str, Any] = {}
                    for key, value in multi_items():
                        qp_dict[key] = value
                    payload.update(qp_dict)
                else:
                    payload.update(dict(query_params))
            except Exception:  # noqa: BLE001
                try:
                    payload.update(dict(query_params.items()))
                except Exception:  # noqa: BLE001, S110
                    pass

        is_json_request = bool(
            getattr(request, "is_json", False)
            or "json"
            in (
                (request.headers.get("content-type") or "")
                if hasattr(request, "headers")
                else ""
            ).lower()
        )
        if is_json_request:
            json_fn = getattr(request, "json", None)
            if callable(json_fn):
                try:
                    body = await json_fn()
                    if isinstance(body, dict):
                        payload.update(body)
                except Exception:  # noqa: BLE001, S110
                    pass
            elif isinstance(json_fn, dict):
                payload.update(json_fn)

        # ``request.route_handler`` is the Litestar equivalent of Flask's
        # ``request.url_rule``; fall back for tests that use mock objects.
        route_handler = getattr(request, "route_handler", None)
        if route_handler is not None:
            url_rule = getattr(route_handler, "name", None) or str(route_handler)
            if url_rule and url_rule != payload.get("path"):
                payload["url_rule"] = url_rule
        else:
            url_rule_attr = getattr(request, "url_rule", None)
            if url_rule_attr is not None:
                rule_str = str(url_rule_attr)
                if rule_str and rule_str != payload.get("path"):
                    payload["url_rule"] = rule_str

        # Remove the raw rison ``q=`` string when the decoded ``rison`` object
        # has already been merged in via ``payload_override`` (avoid duplicates).
        if "rison" in payload and API_URI_RIS_KEY in payload:
            del payload[API_URI_RIS_KEY]
        if "rison" in payload and not payload["rison"]:
            del payload["rison"]

        return payload

    @classmethod
    def get_logger_from_status(
        cls,
        status: int,
    ) -> tuple[Callable[..., None], str]:
        log_map = {
            "2": LoggerLevel.INFO,
            "3": LoggerLevel.INFO,
            "4": LoggerLevel.WARNING,
            "5": LoggerLevel.EXCEPTION,
        }
        log_level = log_map[str(status)[0]]
        return getattr(logger, log_level), log_level

    @abstractmethod
    def log(  # pylint: disable=too-many-arguments
        self,
        user_id: int | None,
        action: str,
        dashboard_id: int | None = None,
        duration_ms: int | None = None,
        slice_id: int | None = None,
        referrer: str | None = None,
        curated_payload: dict[str, Any] | None = None,
        curated_form_data: dict[str, Any] | None = None,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        """Persist a single audit event (subclasses implement)."""

    def log_with_context(  # pylint: disable=too-many-arguments
        self,
        action: str,
        duration: timedelta | None = None,
        object_ref: str | None = None,
        log_to_statsd: bool = True,
        database: Any | None = None,
        request: Any | None = None,
        **payload_override: Any,
    ) -> None:
        """Sync entry-point: build and dispatch a fully-populated audit event.

        ``g.user`` / ``request`` are replaced by ContextVars populated by
        RequestContextMiddleware; ``request=`` kwarg is an escape hatch for
        callers that already have one in scope.

        No event loop → ``asyncio.run`` (Celery/CLI).
        Running loop → ``loop.create_task`` fire-and-forget (Litestar handler).
        """
        if request is None:
            request = get_current_request()

        coro = self._alog_with_context(
            action=action,
            duration=duration,
            object_ref=object_ref,
            log_to_statsd=log_to_statsd,
            database=database,
            request=request,
            **payload_override,
        )

        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None

        if running is None:
            # Celery/CLI: block on the coroutine.  Must also drain pending
            # fire-and-forget tasks before ``asyncio.run`` exits — the runtime
            # cancels still-pending tasks on teardown, silently dropping every
            # Celery/CLI audit record.
            async def _run_and_drain() -> None:
                await coro
                drain = getattr(self, "drain_pending_logs", None)
                if drain is not None:
                    await drain()

            try:
                asyncio.run(_run_and_drain())
            except Exception:  # noqa: BLE001
                logger.warning("Audit log dispatch failed", exc_info=True)
            return

        try:
            running.create_task(coro)
        except Exception:  # noqa: BLE001
            coro.close()
            logger.warning(
                "Failed to schedule audit log on running event loop",
                exc_info=True,
            )

    async def alog_with_context(  # pylint: disable=too-many-arguments
        self,
        action: str,
        duration: timedelta | None = None,
        object_ref: str | None = None,
        log_to_statsd: bool = True,
        database: Any | None = None,
        request: Any | None = None,
        **payload_override: Any,
    ) -> None:
        """Async entry-point — preferred for async controllers."""
        if request is None:
            request = get_current_request()
        await self._alog_with_context(
            action=action,
            duration=duration,
            object_ref=object_ref,
            log_to_statsd=log_to_statsd,
            database=database,
            request=request,
            **payload_override,
        )

    async def _alog_with_context(  # pylint: disable=too-many-locals,too-many-arguments  # noqa: C901  # complex business logic
        self,
        *,
        action: str,
        duration: timedelta | None,
        object_ref: str | None,
        log_to_statsd: bool,
        database: Any | None,
        request: Any | None,
        **payload_override: Any,
    ) -> None:
        # Lazy imports so ``superset.events`` stays importable without a
        # configured app (CLI, alembic, etc.).
        from superset.extensions import stats_logger_manager
        from superset.jinja_context import get_form_data

        referrer: str | None = None
        if request is not None:
            ref = getattr(request, "referrer", None) or (
                request.headers.get("referer") if hasattr(request, "headers") else None
            )
            if ref:
                referrer = str(ref)[:1000]

        duration_ms = (
            int(duration.total_seconds() * 1000) if duration is not None else None
        )

        user = get_current_user()
        user_id = getattr(user, "id", None) if user is not None else None
        if user_id is not None:
            try:
                user_id = int(user_id)
            except (TypeError, ValueError):
                user_id = None

        payload: dict[str, Any] = {}
        if request is not None:
            try:
                payload = await self.collect_request_payload(request)
            except Exception:  # noqa: BLE001
                payload = {}

        if object_ref:
            payload["object_ref"] = object_ref
        if payload_override:
            payload.update(payload_override)

        dashboard_id = to_int(payload.get("dashboard_id"))

        database_params: dict[str, Any] = {
            "database_id": payload.get("database_id"),
        }
        if database is not None and type(database).__name__ == "Database":
            database_params = {
                "database_id": getattr(database, "id", None),
                "engine": getattr(database, "backend", None),
                "database_driver": getattr(database, "driver", None),
            }

        form_data: dict[str, Any] = {}
        if "form_data" in payload:
            try:
                form_data = get_form_data()
            except Exception:  # noqa: BLE001
                form_data = {}
            payload["form_data"] = form_data
            slice_id = form_data.get("slice_id")
        else:
            slice_id = payload.get("slice_id")

        slice_id = to_int(slice_id)

        if log_to_statsd:
            stats_logger_manager.instance.incr(action)

        # ``explode=<key>`` with a JSON-encoded list → log one record per entry.
        records: list[dict[str, Any]]
        try:
            explode_by = payload.get("explode")
            records = json.loads(payload.get(explode_by))  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001
            records = [payload]

        # See ``_SENSITIVE_PAYLOAD_KEYS`` above: every concrete ``log()``
        # implementation either persists ``records`` verbatim (AsyncDBEvent
        # Logger -> Log.json) or prints it (StdOutEventLogger's **kwargs) —
        # neither goes through ``curate_payload``'s allow-list, which is
        # request-classification only, not a security boundary.
        records = [
            _scrub_sensitive_payload(r) if isinstance(r, dict) else r for r in records
        ]

        self.log(
            user_id,
            action,
            records=records,
            dashboard_id=dashboard_id,
            slice_id=slice_id,
            duration_ms=duration_ms,
            referrer=referrer,
            curated_payload=self.curate_payload(payload),
            curated_form_data=self.curate_form_data(form_data),
            **database_params,
        )

    @contextmanager
    def log_context(
        self,
        action: str,
        object_ref: str | None = None,
        log_to_statsd: bool = True,
        **kwargs: Any,
    ) -> Iterator[Callable[..., None]]:
        """Time a block and dispatch ``log_with_context`` on exit.

        Yields a callable that callers may use to update the payload before
        the block exits.
        """
        payload_override = kwargs.copy()
        start = datetime.now()
        yield lambda **kw: payload_override.update(kw)
        duration = datetime.now() - start
        action_str = payload_override.pop("action", action)
        self.log_with_context(
            action_str,
            duration,
            object_ref,
            log_to_statsd,
            **payload_override,
        )

    def _wrapper(
        self,
        f: Callable[..., Any],
        action: str | Callable[..., str] | None = None,
        object_ref: str | Callable[..., str] | Literal[False] | None = None,
        allow_extra_payload: bool | None = False,
        **wrapper_kwargs: Any,
    ) -> Callable[..., Any]:
        """Build the actual decorator — shared by all ``log_this*`` flavours."""
        is_async = inspect.iscoroutinefunction(f)

        @functools.wraps(f)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            action_str = (
                action(*args, **kwargs) if callable(action) else action
            ) or f.__name__
            object_ref_str = (
                object_ref(*args, **kwargs) if callable(object_ref) else object_ref
            ) or (f.__qualname__ if object_ref is not False else None)
            with self.log_context(
                action=action_str,
                object_ref=object_ref_str,
                **wrapper_kwargs,
            ) as log:
                log(**kwargs)
                if allow_extra_payload:
                    value = f(*args, add_extra_log_payload=log, **kwargs)
                else:
                    value = f(*args, **kwargs)
            return value

        @functools.wraps(f)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            action_str = (
                action(*args, **kwargs) if callable(action) else action
            ) or f.__name__
            object_ref_str = (
                object_ref(*args, **kwargs) if callable(object_ref) else object_ref
            ) or (f.__qualname__ if object_ref is not False else None)
            with self.log_context(
                action=action_str,
                object_ref=object_ref_str,
                **wrapper_kwargs,
            ) as log:
                log(**kwargs)
                if allow_extra_payload:
                    value = await f(*args, add_extra_log_payload=log, **kwargs)
                else:
                    value = await f(*args, **kwargs)
            return value

        return async_wrapper if is_async else sync_wrapper

    def log_this(self, f: Callable[..., Any]) -> Callable[..., Any]:
        return self._wrapper(f)

    def log_this_with_context(self, **kwargs: Any) -> Callable[..., Any]:
        def decorator(f: Callable[..., Any]) -> Callable[..., Any]:
            return self._wrapper(f, **kwargs)

        return decorator

    def log_this_with_extra_payload(self, f: Callable[..., Any]) -> Callable[..., Any]:
        """Inject ``add_extra_log_payload`` kwarg into the wrapped function."""
        return self._wrapper(f, allow_extra_payload=True)


class _StructuredLoggerLogger(EventLogger):
    """Default fallback — emits ``logger.debug`` until
    ``configure_event_logger`` is called."""

    def log(  # pylint: disable=too-many-arguments  # noqa: C901  # complex business logic
        self,
        user_id: int | None = None,
        action: str | None = None,
        dashboard_id: int | None = None,
        duration_ms: int | None = None,
        slice_id: int | None = None,
        referrer: str | None = None,
        curated_payload: dict[str, Any] | None = None,
        curated_form_data: dict[str, Any] | None = None,
        *args: Any,
        object_ref: str | None = None,
        extra: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        payload: dict[str, Any] = {"action": action}
        if user_id is not None:
            payload["user_id"] = user_id
        if dashboard_id is not None:
            payload["dashboard_id"] = dashboard_id
        if slice_id is not None:
            payload["slice_id"] = slice_id
        if duration_ms is not None:
            payload["duration_ms"] = duration_ms
        if referrer is not None:
            payload["referrer"] = referrer
        if object_ref is not None:
            payload["object_ref"] = object_ref
        if curated_payload:
            payload["curated_payload"] = curated_payload
        if curated_form_data:
            payload["curated_form_data"] = curated_form_data
        if extra:
            payload.update(extra)
        if kwargs:
            payload.update(kwargs)
        logger.debug("event_log %s", payload)


class StdOutEventLogger(EventLogger):
    """Event logger that prints every event to stdout."""

    def log(  # pylint: disable=too-many-arguments
        self,
        user_id: int | None = None,
        action: str | None = None,
        dashboard_id: int | None = None,
        duration_ms: int | None = None,
        slice_id: int | None = None,
        referrer: str | None = None,
        curated_payload: dict[str, Any] | None = None,
        curated_form_data: dict[str, Any] | None = None,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        data: dict[str, Any] = dict(  # noqa: C408
            user_id=user_id,
            action=action,
            dashboard_id=dashboard_id,
            duration_ms=duration_ms,
            slice_id=slice_id,
            referrer=referrer,
            curated_payload=curated_payload,
            curated_form_data=curated_form_data,
            **kwargs,
        )
        print("StdOutEventLogger: ", data)


class AsyncDBEventLogger(EventLogger):
    """Event logger that persists ``Log`` records to the metadata DB.

    Each :meth:`log` call schedules a fire-and-forget
    ``asyncio.Task`` that opens a short-lived ``AsyncSession``, inserts
    the ``Log`` rows, commits, and closes — guaranteeing audit logging
    never blocks the request loop.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._session_factory = session_factory
        # Strong refs to in-flight tasks: prevents GC of fire-and-forget
        # writes mid-execution, and lets ``drain_pending_logs`` block until
        # every row is committed before the Celery/CLI loop closes.
        self._pending_persists: set[asyncio.Task[None]] = set()

    def log(  # pylint: disable=too-many-arguments
        self,
        user_id: int | None = None,
        action: str | None = None,
        dashboard_id: int | None = None,
        duration_ms: int | None = None,
        slice_id: int | None = None,
        referrer: str | None = None,
        curated_payload: dict[str, Any] | None = None,
        curated_form_data: dict[str, Any] | None = None,
        *args: Any,
        records: list[dict[str, Any]] | None = None,
        extra: dict[str, Any] | None = None,
        object_ref: str | None = None,
        **kwargs: Any,
    ) -> None:
        # Always emit a structured log line regardless of DB persistence.
        payload_for_logger: dict[str, Any] = {
            "action": action,
            "user_id": user_id,
            "dashboard_id": dashboard_id,
            "slice_id": slice_id,
            "duration_ms": duration_ms,
            "object_ref": object_ref,
        }
        if extra:
            payload_for_logger.update(extra)
        if curated_payload:
            payload_for_logger["curated_payload"] = curated_payload
        if curated_form_data:
            payload_for_logger["curated_form_data"] = curated_form_data
        if kwargs:
            payload_for_logger.update(kwargs)
        logger.debug("event_log %s", payload_for_logger)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        task = loop.create_task(
            self._persist(
                action=action or "event",
                user_id=user_id,
                duration_ms=duration_ms,
                dashboard_id=dashboard_id,
                slice_id=slice_id,
                referrer=referrer,
                records=records,
                extra=extra,
            )
        )
        self._pending_persists.add(task)
        task.add_done_callback(self._pending_persists.discard)

    async def drain_pending_logs(self) -> None:
        """Await every in-flight ``_persist`` task before the sync loop closes."""
        while self._pending_persists:
            await asyncio.gather(*list(self._pending_persists), return_exceptions=True)

    async def _persist(  # pylint: disable=too-many-arguments
        self,
        *,
        action: str,
        user_id: int | None,
        duration_ms: int | None,
        dashboard_id: int | None,
        slice_id: int | None,
        referrer: str | None,
        records: list[dict[str, Any]] | None,
        extra: dict[str, Any] | None,
    ) -> None:
        """Bulk-insert ``Log`` rows in a dedicated short-lived session.

        Passes dicts (not ORM instances) to ``AsyncSession.execute`` so
        SQLAlchemy compiles a single executemany-style statement.
        """
        from superset.models.core import Log

        if not records:
            records = [extra] if extra else [{}]

        # Naive UTC — matches ``Log.dttm`` (TIMESTAMP WITHOUT TIME ZONE).
        dttm = datetime.now(tz=timezone.utc).replace(tzinfo=None)

        # Superset's JSON serializer is datetime/NaN-aware; stdlib json.dumps
        # raises on a datetime in the audit payload.
        from superset.utils import json as superset_json

        rows: list[dict[str, Any]] = []
        for record in records:
            try:
                json_string: str | None = superset_json.dumps(record)
            except Exception:  # noqa: BLE001
                json_string = None
            rows.append(
                {
                    "action": action,
                    "json": json_string,
                    "dashboard_id": dashboard_id or record.get("dashboard_id"),
                    "slice_id": slice_id or record.get("slice_id"),
                    "duration_ms": duration_ms,
                    "referrer": referrer,
                    "user_id": user_id,
                    "dttm": dttm,
                }
            )

        if not rows:
            return

        session: AsyncSession = self._session_factory()
        try:
            await session.execute(insert(Log), rows)
            await session.commit()
        except SQLAlchemyError as ex:
            logger.error("AsyncDBEventLogger failed to log event(s)")
            logger.exception(ex)
            try:
                await session.rollback()
            except Exception:  # noqa: BLE001
                logger.error(
                    "AsyncDBEventLogger failed to rollback the session after failure"
                )
        finally:
            await session.close()


def get_event_logger_from_cfg_value(cfg_value: Any) -> EventLogger:
    """Validate ``EVENT_LOGGER`` config; supports legacy class-assignment syntax."""
    result: Any = cfg_value
    if inspect.isclass(cfg_value):
        logging.warning(
            textwrap.dedent(
                """
                In superset private config, EVENT_LOGGER has been assigned a class
                object. In order to accomodate pre-configured instances without a
                default constructor, assignment of a class is deprecated and may no
                longer work at some point in the future. Please assign an object
                instance of a type that implements
                superset.events.EventLogger.
                """
            )
        )
        event_logger_type = cast(type[Any], cfg_value)
        result = event_logger_type()

    if not isinstance(result, EventLogger):
        raise TypeError(
            "EVENT_LOGGER must be configured with a concrete instance "
            "of superset.events.EventLogger."
        )
    logging.debug("Configured event logger of type %s", type(result))
    return cast(EventLogger, result)


event_logger: EventLogger = _StructuredLoggerLogger()


def configure_event_logger(
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    """Replace the module-level ``event_logger`` singleton at app startup."""
    global event_logger  # noqa: PLW0603
    if session_factory is not None:
        event_logger = AsyncDBEventLogger(session_factory)
        logger.info("Event logger configured: AsyncDBEventLogger")
    else:
        event_logger = _StructuredLoggerLogger()
        logger.info("Event logger configured: structured logger fallback")


def log_this_with_context(
    *,
    action: str | Callable[..., str] | None = None,
    object_ref: str | Callable[..., str] | None = None,
    log_to_statsd: bool = True,
) -> Callable[..., Any]:
    """Module-level wrapper: defers ``event_logger`` resolution until
    decoration time."""

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        is_async = inspect.iscoroutinefunction(fn)

        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            return await event_logger.log_this_with_context(
                action=action,
                object_ref=object_ref,
                log_to_statsd=log_to_statsd,
            )(fn)(*args, **kwargs)

        @functools.wraps(fn)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            return event_logger.log_this_with_context(
                action=action,
                object_ref=object_ref,
                log_to_statsd=log_to_statsd,
            )(fn)(*args, **kwargs)

        return async_wrapper if is_async else sync_wrapper

    return decorator


__all__ = [
    "API_URI_RIS_KEY",
    "AsyncDBEventLogger",
    "EventLogger",
    "StdOutEventLogger",
    "configure_event_logger",
    "event_logger",
    "get_event_logger_from_cfg_value",
    "log_this_with_context",
]
