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
"""Backward-compatibility shim — primary implementation lives in
:mod:`superset.events`.

The original ``superset_old/utils/log.py`` is fully ported into
:mod:`superset.events` (which is async-native and Flask-free).  This
module re-exports those primitives plus a handful of pure helpers
(:func:`stats_timing`, :func:`get_logger_from_status`,
:func:`get_event_logger_from_cfg_value`, :func:`collect_request_payload`,
:func:`logs_context`).

Importing from ``superset.utils.log`` continues to work unchanged so any
existing call site (third-party plugins, ported tasks, the legacy
``DBEventLogger`` config alias) keeps functioning.
"""

from __future__ import annotations

import functools
import inspect
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Callable, TYPE_CHECKING

# Re-exports from the async-native event-logger module.
from superset.events import (
    API_URI_RIS_KEY,
    AsyncDBEventLogger,
    configure_event_logger,
    event_logger,
    EventLogger,
    get_event_logger_from_cfg_value,
    log_this_with_context,
    StdOutEventLogger,
)
from superset.utils.core import get_user_id, LoggerLevel, to_int
from superset.utils.dates import now_as_float

if TYPE_CHECKING:
    from superset.stats_logger import BaseStatsLogger

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# AbstractEventLogger / DBEventLogger -- legacy aliases.
# ---------------------------------------------------------------------------
#
# The original ``superset_old/utils/log.py`` exported three concrete
# classes:
#
#   * ``AbstractEventLogger`` -- the abstract base class.
#   * ``DBEventLogger``       -- the SQLAlchemy-persisting impl.
#   * ``StdOutEventLogger``   -- the stdout-printing impl.
#
# In the Liteset port these live under :mod:`superset.events` with the
# updated names ``EventLogger`` / ``AsyncDBEventLogger`` /
# ``StdOutEventLogger``.  Keep the old import paths working for plugins,
# tests, and any documentation that references the legacy names.

AbstractEventLogger = EventLogger
DBEventLogger = AsyncDBEventLogger


# ---------------------------------------------------------------------------
# stats_timing — context manager (1:1 port from
# ``superset_old.utils.decorators.stats_timing``).
# ---------------------------------------------------------------------------


@contextmanager
def stats_timing(stats_key: str, stats_logger: BaseStatsLogger) -> Iterator[float]:
    """Provide a transactional scope around a series of operations."""
    start_ts = now_as_float()
    try:
        yield start_ts
    finally:
        stats_logger.timing(stats_key, now_as_float() - start_ts)


# ---------------------------------------------------------------------------
# get_logger_from_status -- maps HTTP status codes → log methods.
# ---------------------------------------------------------------------------


def get_logger_from_status(
    status: int,
) -> tuple[Callable[..., None], str]:
    """Return ``(logger_method, level_name)`` for the given HTTP status.

    Identical to the original implementation; kept here so legacy callers
    can keep doing ``from superset.utils.log import get_logger_from_status``.
    Delegates to :meth:`EventLogger.get_logger_from_status` to keep a
    single source of truth.
    """
    return EventLogger.get_logger_from_status(status)


# ---------------------------------------------------------------------------
# collect_request_payload — async port of the original Flask helper.
# ---------------------------------------------------------------------------


async def collect_request_payload(request: Any | None = None) -> dict[str, Any]:
    """Async request-payload collector.

    Mirrors ``superset_old/utils/log.py:collect_request_payload`` but
    awaits Litestar's ``form()`` / ``json()`` coroutines so the audit
    payload includes form/JSON body fields.  Delegates to
    :meth:`EventLogger.collect_request_payload` (single source of truth).

    The function is tolerant of Litestar-shaped requests as well as the
    plain dict-shaped objects used in tests; missing accessors degrade
    gracefully to whatever can be read synchronously.
    """
    # Use the ``EventLogger`` implementation directly so the legacy
    # function shape matches the original 1:1.
    return await event_logger.collect_request_payload(request)


# Legacy back-compat alias kept for callers (rare, but documented in the
# original) who imported the async name explicitly.
collect_request_payload_async = collect_request_payload


# ---------------------------------------------------------------------------
# Legacy decorator alias.
# ---------------------------------------------------------------------------


def log_this(f: Callable[..., Any]) -> Callable[..., Any]:
    """Convenience decorator — wraps ``f`` with :func:`log_this_with_context`.

    Mirrors the original ``AbstractEventLogger.log_this`` so callers that
    used to do ``@event_logger.log_this`` keep working.
    """
    return log_this_with_context()(f)


# ---------------------------------------------------------------------------
# Async-aware logs_context decorator
# ---------------------------------------------------------------------------


def logs_context(
    context_func: Callable[..., dict[Any, Any]] | None = None,
    **ctx_kwargs: Any,
) -> Callable[..., Any]:
    """Add structured fields to the per-task logs-context dict.

    Replaces the original Flask ``g.logs_context`` with an
    ``asyncio.ContextVar`` (see :func:`superset.utils.core.get_logs_context`).
    Behaviour is identical: the decorator collects values from the
    decorated function's kwargs, optional ``ctx_kwargs`` and an optional
    ``context_func`` callable, then merges them into the per-task context
    dict before invoking the wrapped function.
    """

    available_keys = {
        "slice_id",
        "dashboard_id",
        "dataset_id",
        "execution_id",
        "report_schedule_id",
    }

    def decorate(f: Callable[..., Any]) -> Callable[..., Any]:
        is_async = inspect.iscoroutinefunction(f)

        def _build_payload(
            args: tuple[Any, ...], kwargs: dict[str, Any]
        ) -> dict[str, Any]:
            payload: dict[str, Any] = {
                k: v for k, v in kwargs.items() if k in available_keys and v is not None
            }
            try:
                payload.update(
                    {
                        k: v
                        for k, v in ctx_kwargs.items()
                        if k in available_keys and v is not None
                    }
                )
                if context_func is not None:
                    payload.update(
                        {
                            k: v
                            for k, v in context_func(*args, **kwargs).items()
                            if k in available_keys and v is not None
                        }
                    )
            except (TypeError, KeyError, AttributeError):
                logger.warning("Invalid data was passed to the logs_context decorator")
            return payload

        if is_async:

            @functools.wraps(f)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                from superset.utils.core import get_logs_context

                get_logs_context().update(_build_payload(args, kwargs))
                return await f(*args, **kwargs)

            return async_wrapper

        @functools.wraps(f)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            from superset.utils.core import get_logs_context

            get_logs_context().update(_build_payload(args, kwargs))
            return f(*args, **kwargs)

        return sync_wrapper

    return decorate


__all__ = [
    "API_URI_RIS_KEY",
    "AbstractEventLogger",
    "AsyncDBEventLogger",
    "DBEventLogger",
    "EventLogger",
    "LoggerLevel",
    "StdOutEventLogger",
    "collect_request_payload",
    "collect_request_payload_async",
    "configure_event_logger",
    "event_logger",
    "get_event_logger_from_cfg_value",
    "get_logger_from_status",
    "get_user_id",
    "log_this",
    "log_this_with_context",
    "logs_context",
    "stats_timing",
    "to_int",
]
