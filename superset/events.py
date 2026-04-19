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
"""Audit event logging for Superset API endpoints.

Provides:
- ``EventLogger`` -- stdout/logging fallback (no DB required).
- ``AsyncDBEventLogger`` -- persists ``Log`` records to the database
  using fire-and-forget ``asyncio.create_task``.
- ``log_this_with_context`` -- async-aware decorator that records
  action name, duration, and user id around handler invocations.
- ``configure_event_logger`` / ``event_logger`` -- module-level
  singleton, configurable at app startup.

Ported 1:1 from ``superset_old/utils/log.py`` (``DBEventLogger`` /
``AbstractEventLogger``), adapted for async SQLAlchemy 2.0 sessions.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, TYPE_CHECKING

from sqlalchemy.exc import SQLAlchemyError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# EventLogger -- stdout/logging fallback
# ---------------------------------------------------------------------------


class EventLogger:
    """Minimal audit event logger.

    Logs API actions as structured log records.  Used as the default
    fallback when no async session factory is available (e.g. CLI tools,
    Celery workers outside the Litestar process).
    """

    def log(
        self,
        action: str,
        *,
        object_ref: str | None = None,
        user_id: int | None = None,
        duration_ms: float | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {"action": action}
        if object_ref is not None:
            payload["object_ref"] = object_ref
        if user_id is not None:
            payload["user_id"] = user_id
        if duration_ms is not None:
            payload["duration_ms"] = round(duration_ms, 2)
        if extra:
            payload.update(extra)
        logger.debug("event_log %s", payload)

    # -- context-manager interface (used by screenshots.py etc.) -----------

    @contextmanager
    def log_context(
        self,
        action: str,
        *,
        object_ref: str | None = None,
        user_id: int | None = None,
        **extra: Any,
    ) -> Iterator[Callable[..., None]]:
        """Measure wall-clock time and log on block exit.

        Yields a callback that callers may use to attach extra payload
        fields (matching the original ``AbstractEventLogger.log_context``
        interface).
        """
        payload_override: dict[str, Any] = dict(extra)
        start = time.monotonic()
        yield lambda **kw: payload_override.update(kw)
        duration_ms = (time.monotonic() - start) * 1000
        action_str = payload_override.pop("action", action)
        self.log(
            action_str,
            object_ref=object_ref,
            user_id=user_id,
            duration_ms=duration_ms,
            extra=payload_override if payload_override else None,
        )


# ---------------------------------------------------------------------------
# AsyncDBEventLogger -- persists Log records to the database
# ---------------------------------------------------------------------------


class AsyncDBEventLogger(EventLogger):
    """Event logger that persists ``Log`` records to the database.

    Accepts an ``async_sessionmaker`` at construction time.  Each
    ``log()`` call spawns a fire-and-forget ``asyncio.Task`` that opens
    its own short-lived session, inserts the record(s), and commits.
    This ensures that logging never blocks the HTTP response.

    Ported 1:1 from ``superset_old/utils/log.py:DBEventLogger``.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._session_factory = session_factory

    # -- public interface ---------------------------------------------------

    def log(
        self,
        action: str,
        *,
        object_ref: str | None = None,
        user_id: int | None = None,
        duration_ms: float | None = None,
        extra: dict[str, Any] | None = None,
        dashboard_id: int | None = None,
        slice_id: int | None = None,
        referrer: str | None = None,
        records: list[dict[str, Any]] | None = None,
    ) -> None:
        """Schedule a fire-and-forget DB write.

        Falls back to the parent ``EventLogger.log`` (stdout) if there
        is no running event loop (e.g. inside a sync Celery worker).
        """
        # Always emit a structured log line regardless of DB persistence.
        super().log(
            action,
            object_ref=object_ref,
            user_id=user_id,
            duration_ms=duration_ms,
            extra=extra,
        )

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running event loop -- nothing more we can do.
            return

        loop.create_task(
            self._persist(
                action=action,
                user_id=user_id,
                duration_ms=int(duration_ms) if duration_ms is not None else None,
                dashboard_id=dashboard_id,
                slice_id=slice_id,
                referrer=referrer,
                records=records,
                extra=extra,
            )
        )

    # -- private persistence ------------------------------------------------

    async def _persist(
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
        """Insert ``Log`` rows inside a dedicated short-lived session.

        Mirrors the original ``DBEventLogger.log`` method:
        - Iterates over *records* (bulk insert when the frontend sends
          ``?explode=events``).
        - Falls back to a single record built from *extra* when no
          explicit records list is provided.
        - Catches and logs ``SQLAlchemyError`` so logging failures never
          propagate to callers.
        """
        from superset.models.core import Log

        if not records:
            records = [extra] if extra else [{}]

        logs: list[Log] = []
        for record in records:
            json_string: str | None
            try:
                json_string = json.dumps(record)
            except Exception:  # noqa: BLE001
                json_string = None
            logs.append(
                Log(
                    action=action,
                    json=json_string,
                    dashboard_id=dashboard_id or record.get("dashboard_id"),
                    slice_id=slice_id or record.get("slice_id"),
                    duration_ms=duration_ms,
                    referrer=referrer,
                    user_id=user_id,
                    # ``Log.dttm`` is a naive TIMESTAMP WITHOUT TIME ZONE
                    # column defaulting to ``datetime.utcnow``.  asyncpg
                    # rejects tz-aware datetimes for such columns, so we
                    # strip tzinfo to match the original Superset behaviour.
                    dttm=datetime.now(tz=timezone.utc).replace(tzinfo=None),
                )
            )

        session: AsyncSession = self._session_factory()
        try:
            session.add_all(logs)
            await session.commit()
        except SQLAlchemyError as ex:
            logging.error("AsyncDBEventLogger failed to log event(s)")
            logging.exception(ex)
            try:
                await session.rollback()
            except Exception:  # noqa: BLE001
                logging.error(
                    "AsyncDBEventLogger failed to rollback the session after failure"
                )
        finally:
            await session.close()


# ---------------------------------------------------------------------------
# log_this_with_context -- async-aware decorator
# ---------------------------------------------------------------------------


def log_this_with_context(
    *,
    action: str | Callable[..., str] | None = None,
    object_ref: str | Callable[..., str] | None = None,
    log_to_statsd: bool = True,
) -> Callable[..., Any]:
    """Decorator that logs action name, duration, and user id.

    Adapted from ``AbstractEventLogger._wrapper`` /
    ``log_this_with_context`` for async handler functions.  Resolves
    ``action`` and ``object_ref`` at call time (they may be callables).

    Usage::

        @log_this_with_context(action="chart.get")
        async def get_chart(self, ...) -> ...:
            ...
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            action_str = (
                action(*args, **kwargs) if callable(action) else action
            ) or fn.__name__

            object_ref_str = (
                object_ref(*args, **kwargs) if callable(object_ref) else object_ref
            ) or fn.__qualname__

            start = time.monotonic()
            try:
                return await fn(*args, **kwargs)
            finally:
                duration_ms = (time.monotonic() - start) * 1000

                # Try to extract user_id from a Litestar Request if
                # present in the function arguments.
                user_id: int | None = None
                request = kwargs.get("request")
                if request is not None:
                    user = getattr(request, "user", None)
                    if user is not None:
                        user_id = getattr(user, "id", None)

                event_logger.log(
                    action_str,
                    object_ref=object_ref_str,
                    user_id=user_id,
                    duration_ms=duration_ms,
                )

        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Module-level singleton & configuration
# ---------------------------------------------------------------------------

# Default to the simple stdout logger.  ``configure_event_logger`` is
# called during app startup to swap in ``AsyncDBEventLogger`` when the
# async session factory is available.
event_logger: EventLogger = EventLogger()


def configure_event_logger(
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    """Replace the module-level ``event_logger`` singleton.

    Called once during Litestar app startup (after the async engine and
    session factory have been created).  If *session_factory* is ``None``
    the logger remains the plain ``EventLogger`` (stdout only).
    """
    global event_logger  # noqa: PLW0603
    if session_factory is not None:
        event_logger = AsyncDBEventLogger(session_factory)
        logger.info("Event logger configured: AsyncDBEventLogger")
    else:
        event_logger = EventLogger()
        logger.info("Event logger configured: EventLogger (stdout only)")
