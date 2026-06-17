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
"""Async-friendly decorators.

Public API kept stable so existing call sites keep working:

* :func:`statsd_gauge` — emit ``ok`` / ``warning`` / ``error`` gauges
  around a function call (sync **and** async).
* :func:`logs_context` — re-export from :mod:`superset.utils.log` (the
  shim layer around :func:`get_logs_context`).
* :func:`stats_timing` — re-export from :mod:`superset.utils.log`
  (originally lived here).
* :func:`debounce` / :func:`arghash` — pure helpers, unchanged.
* :func:`suppress_logging` — pure helper, unchanged.
* :func:`on_error` / :func:`transaction` — async-friendly transactional
  decorator.  In Liteset the AsyncSession lifecycle is normally handled
  by middleware (``commit`` on success, ``rollback`` on error), so the
  wrapper degrades gracefully when no session is bound to the call.
* :func:`on_security_exception` — maps a security exception to a
  Litestar-compatible JSON response payload.

The legacy WSGI stack is no longer imported.
"""

from __future__ import annotations

import functools
import inspect
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable, TYPE_CHECKING

from sqlalchemy.exc import SQLAlchemyError

from superset.utils.core import error_msg_from_exception

# Re-exports — keep the old import paths working for plugins.
from superset.utils.log import logs_context, stats_timing  # noqa: F401

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


def statsd_gauge(metric_prefix: str | None = None) -> Callable[..., Any]:
    """Emit ``<prefix>.ok`` / ``.warning`` / ``.error`` gauges around a call.

    * On success the ``.ok`` gauge is set to ``1``.
    * On failure with ``ex.status < 500`` (e.g. ``HTTPException(404)``)
      the ``.warning`` gauge is set, otherwise ``.error``.
    * The original exception is always re-raised.

    Resolves ``stats_logger_manager`` lazily from
    :mod:`superset.extensions` so the import never crashes when this
    module is loaded before app startup.
    """

    def decorate(f: Callable[..., Any]) -> Callable[..., Any]:
        prefix_name = metric_prefix or f.__name__
        is_async = inspect.iscoroutinefunction(f)

        def _emit_ok() -> None:
            from superset.extensions import stats_logger_manager

            stats_logger_manager.instance.gauge(f"{prefix_name}.ok", 1)

        def _emit_failure(ex: BaseException) -> None:
            from superset.extensions import stats_logger_manager

            status = getattr(ex, "status", None)
            if status is not None and status < 500:
                stats_logger_manager.instance.gauge(f"{prefix_name}.warning", 1)
            else:
                stats_logger_manager.instance.gauge(f"{prefix_name}.error", 1)

        if is_async:

            @functools.wraps(f)
            async def async_wrapped(*args: Any, **kwargs: Any) -> Any:
                try:
                    result = await f(*args, **kwargs)
                except Exception as ex:
                    _emit_failure(ex)
                    raise
                _emit_ok()
                return result

            return async_wrapped

        @functools.wraps(f)
        def sync_wrapped(*args: Any, **kwargs: Any) -> Any:
            try:
                result = f(*args, **kwargs)
            except Exception as ex:
                _emit_failure(ex)
                raise
            _emit_ok()
            return result

        return sync_wrapped

    return decorate


def arghash(args: Any, kwargs: Any) -> int:
    """Simple hash of positional + keyword arguments (used by :func:`debounce`)."""
    sorted_args = tuple(
        x if hasattr(x, "__repr__") else x for x in [*args, *sorted(kwargs.items())]
    )
    return hash(sorted_args)


def debounce(duration: float | int = 0.1) -> Callable[..., Any]:
    """Ensure a function called with the same arguments executes only once
    per ``duration`` seconds (default: 100 ms).
    """

    def decorate(f: Callable[..., Any]) -> Callable[..., Any]:
        last: dict[str, Any] = {"t": None, "input": None, "output": None}
        is_async = inspect.iscoroutinefunction(f)

        if is_async:

            @functools.wraps(f)
            async def async_wrapped(*args: Any, **kwargs: Any) -> Any:
                now = time.time()
                updated_hash = arghash(args, kwargs)
                if (
                    last["t"] is None
                    or now - last["t"] >= duration
                    or last["input"] != updated_hash
                ):
                    result = await f(*args, **kwargs)
                    last["t"] = time.time()
                    last["input"] = updated_hash
                    last["output"] = result
                    return result
                return last["output"]

            return async_wrapped

        @functools.wraps(f)
        def sync_wrapped(*args: Any, **kwargs: Any) -> Any:
            now = time.time()
            updated_hash = arghash(args, kwargs)
            if (
                last["t"] is None
                or now - last["t"] >= duration
                or last["input"] != updated_hash
            ):
                result = f(*args, **kwargs)
                last["t"] = time.time()
                last["input"] = updated_hash
                last["output"] = result
                return result
            return last["output"]

        return sync_wrapped

    return decorate


@contextmanager
def suppress_logging(
    logger_name: str | None = None,
    new_level: int = logging.CRITICAL,
) -> Iterator[None]:
    """Temporarily raise ``logger_name``'s level to *new_level*.

    Use with care; restoring the previous level happens on context exit.
    """
    target_logger = logging.getLogger(logger_name)
    original_level = target_logger.getEffectiveLevel()
    target_logger.setLevel(new_level)
    try:
        yield
    finally:
        target_logger.setLevel(original_level)


def on_error(
    ex: Exception,
    catches: tuple[type[Exception], ...] = (SQLAlchemyError,),
    reraise: type[Exception] | None = SQLAlchemyError,
) -> None:
    """Default handler used by :func:`transaction`.

    * Logs SQLAlchemy errors via :func:`logger.exception` (when the
      original exception exposes ``ex.exception`` we log the underlying
      cause, matching the original).
    * Optionally re-raises a different exception type so callers can
      surface a domain-specific error (e.g. ``RuleDeleteFailedError``).
    """
    if isinstance(ex, catches):
        if hasattr(ex, "exception"):
            logger.exception(ex.exception)
        if reraise:
            raise reraise() from ex
    else:
        raise ex


# Re-entrancy is tracked on a ``ContextVar`` so concurrent async
# requests cannot leak the "I'm already inside a transaction" flag
# across each other.  The original version used
# a request-scoped ``g.in_transaction`` which is implicitly per-request; the
# ContextVar gives us the same isolation guarantees in async-land.
_in_transaction_ctx: ContextVar[bool] = ContextVar("_in_transaction_ctx", default=False)


def transaction(  # pylint: disable=redefined-outer-name  # noqa: C901  # complex business logic
    on_error: Callable[..., Any] | None = on_error,
) -> Callable[..., Any]:
    """Wrap a Command's ``run`` method in a single-commit transaction.

    Liteset's :class:`AsyncSession` lifecycle is normally driven by
    Litestar middleware (``before_response`` commits on success, the
    exception handler rolls back on failure).  This decorator exists for
    parity with the original Superset Commands and supports two extra
    behaviours not provided by middleware alone:

    1. If the wrapped object exposes ``self.session`` / ``self._session``
       (an :class:`AsyncSession`) we ``commit`` on success and
       ``rollback`` on failure, matching the original
       ``db.session.commit()`` semantics.
    2. If the wrapped Command is already running inside another
       ``@transaction``-decorated call (re-entrant) we forward to the
       function directly so the inner block never commits halfway.
       The reentrancy flag is held on a :class:`ContextVar`, so
       concurrent async tasks never see each other's flags.

    Sync functions are supported as a convenience (the original was
    sync-only); the wrapper is always ``async`` when the wrapped function
    is asynchronous.
    """

    def decorate(func: Callable[..., Any]) -> Callable[..., Any]:  # noqa: C901  # complex business logic
        is_async = inspect.iscoroutinefunction(func)

        def _resolve_session(self: Any) -> Any:
            """Walk the canonical session-attribute chain (``self.session`` →
            ``self._session`` → ``self._dao.session``) and raise a
            :class:`RuntimeError` when none is found.

            The original Apache Superset ``@transaction`` was attached to
            Commands that all bound ``db.session`` at construction time,
            so the decorator could safely commit at the end.  In Liteset
            we depend on every Command exposing ``self.session`` (an
            :class:`AsyncSession`); when that contract is violated the
            decorator silently degrades to no-op-mode, which is exactly
            the regression mode the third-party review flagged.  Loud
            failure is the right answer.
            """
            session = getattr(self, "session", None)
            if session is None:
                session = getattr(self, "_session", None)
            if session is None:
                dao = getattr(self, "_dao", None)
                if dao is not None:
                    session = getattr(dao, "session", None)
            if session is None:
                raise RuntimeError(
                    f"@transaction() applied to {type(self).__name__}"
                    f".{func.__name__} but the instance has no session "
                    "attribute (checked .session, ._session, "
                    "._dao.session). Either set `self.session` before "
                    "the decorated method runs or remove @transaction()."
                )
            return session

        @functools.wraps(func)
        async def async_wrapped(self: Any, *args: Any, **kwargs: Any) -> Any:
            if _in_transaction_ctx.get():
                # Already inside an outer @transaction — pass through.
                return await func(self, *args, **kwargs)

            session = _resolve_session(self)

            token = _in_transaction_ctx.set(True)
            try:
                try:
                    result = await func(self, *args, **kwargs)
                    if hasattr(session, "commit"):
                        await session.commit()
                    return result
                except Exception as ex:
                    if hasattr(session, "rollback"):
                        try:
                            await session.rollback()
                        except Exception:  # noqa: BLE001
                            logger.exception("Failed to rollback async session")
                    if on_error is not None:
                        return on_error(ex)
                    raise
            finally:
                _in_transaction_ctx.reset(token)

        @functools.wraps(func)
        def sync_wrapped(self: Any, *args: Any, **kwargs: Any) -> Any:
            if _in_transaction_ctx.get():
                return func(self, *args, **kwargs)

            session = _resolve_session(self)

            token = _in_transaction_ctx.set(True)
            try:
                try:
                    result = func(self, *args, **kwargs)
                    if hasattr(session, "commit"):
                        session.commit()
                    return result
                except Exception as ex:
                    if hasattr(session, "rollback"):
                        try:
                            session.rollback()
                        except Exception:  # noqa: BLE001
                            logger.exception("Failed to rollback sync session")
                    if on_error is not None:
                        return on_error(ex)
                    raise
            finally:
                _in_transaction_ctx.reset(token)

        return async_wrapped if is_async else sync_wrapped

    return decorate


def on_security_exception(self: Any, ex: Exception) -> dict[str, Any]:
    """Helper that returns a Litestar-compatible 403 response payload.

    The async controllers use Litestar's ``Response`` directly, so this
    function returns the body dict and the caller is responsible for
    wrapping it in a Response.
    """
    del self  # only kept for API parity with the original signature
    return {"status_code": 403, "message": error_msg_from_exception(ex)}


__all__ = [
    "arghash",
    "debounce",
    "logs_context",
    "on_error",
    "on_security_exception",
    "stats_timing",
    "statsd_gauge",
    "suppress_logging",
    "transaction",
]
