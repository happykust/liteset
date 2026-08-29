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
"""Thread-pool sizing for the blocking work the ASGI app offloads.

Large parts of the port stay synchronous — sync engine specs for engines
without an asyncio driver, Jinja rendering, SQLGlot passes, screenshot
capture — and reach the event loop through ``asyncio.to_thread``.  That
helper uses the loop's *default* executor, whose CPython default of
``min(32, cpu_count + 4)`` is 8 threads on a 4-vCPU container: small enough
that a handful of slow queries silently queue every other offload behind
them.

Two pools are configured here:

* the **default executor**, sized for I/O-bound offloads, and
* a **dedicated SQL Lab pool**, so a thread left running by a query that
  outlived its timeout (``asyncio.wait_for`` cancels the future, never the
  thread) cannot starve chart rendering and authentication.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any

logger = logging.getLogger(__name__)

#: Upper bound for the computed defaults.  Threads here mostly wait on
#: sockets, but each one can hold a pooled DB connection, so the ceiling
#: keeps the metadata/analytics pools from being oversubscribed.
_MAX_DEFAULT_THREADS = 64
_MIN_DEFAULT_THREADS = 16
_THREADS_PER_CPU = 4

#: SQL Lab is deliberately smaller: these threads each hold an analytics-DB
#: connection for the whole query.
_MAX_SQLLAB_THREADS = 32
_MIN_SQLLAB_THREADS = 8


def _computed(minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, (os.cpu_count() or 1) * _THREADS_PER_CPU))


def default_pool_size(settings: Any) -> int:
    """Resolve ``ASYNCIO_MAX_WORKER_THREADS``, falling back to a computed size."""
    if configured := getattr(settings, "asyncio_max_worker_threads", None):
        return int(configured)
    return _computed(_MIN_DEFAULT_THREADS, _MAX_DEFAULT_THREADS)


def sqllab_pool_size(settings: Any) -> int:
    """Resolve ``SQLLAB_MAX_WORKER_THREADS``, falling back to a computed size."""
    if configured := getattr(settings, "sqllab_max_worker_threads", None):
        return int(configured)
    return _computed(_MIN_SQLLAB_THREADS, _MAX_SQLLAB_THREADS)


def install_default_executor(settings: Any) -> ThreadPoolExecutor:
    """Replace the running loop's default executor with a sized pool."""
    size = default_pool_size(settings)
    executor = ThreadPoolExecutor(
        max_workers=size,
        thread_name_prefix="liteset-offload",
    )
    asyncio.get_running_loop().set_default_executor(executor)
    logger.info("Default asyncio executor sized to %d threads", size)
    return executor


_sqllab_executor: ThreadPoolExecutor | None = None
_sqllab_semaphore: asyncio.Semaphore | None = None


def install_sqllab_executor(settings: Any) -> ThreadPoolExecutor:
    """Create the dedicated SQL Lab pool and its admission semaphore."""
    global _sqllab_executor, _sqllab_semaphore  # noqa: PLW0603

    size = sqllab_pool_size(settings)
    _sqllab_executor = ThreadPoolExecutor(
        max_workers=size,
        thread_name_prefix="liteset-sqllab",
    )
    # Admission control: without it, requests queue inside the executor with
    # no visibility and no way to shed load.  The semaphore lets the command
    # layer fail fast with a real error instead of hanging.
    _sqllab_semaphore = asyncio.Semaphore(size)
    logger.info("SQL Lab executor sized to %d threads", size)
    return _sqllab_executor


def shutdown_executors() -> None:
    """Release the SQL Lab pool.

    ``wait=False`` because a thread may still be blocked in a driver call
    that outlived its timeout; shutdown must not hang on it.
    """
    global _sqllab_executor, _sqllab_semaphore  # noqa: PLW0603

    if _sqllab_executor is not None:
        _sqllab_executor.shutdown(wait=False, cancel_futures=True)
    _sqllab_executor = None
    _sqllab_semaphore = None


async def run_sqllab_blocking(func: Any, /, *args: Any) -> Any:
    """Run *func* on the SQL Lab pool, honouring the admission semaphore.

    The slot is released from the worker thread's completion callback rather
    than by the awaiting coroutine.  This matters because SQL Lab wraps the
    call in ``asyncio.wait_for``: a timeout cancels the *future* while the
    thread keeps running the driver call to completion.  Releasing on
    cancellation would hand the slot to a new query while the old one still
    holds a thread, so the pool would over-admit exactly when it is already
    saturated.

    Each job runs inside its own copy of the caller's context.  That is not
    optional: ``asyncio.to_thread`` copies the context per call, and a bare
    ``executor.submit`` does not — so replacing one with the other silently
    removed the isolation.  Pool threads are long-lived and reused, and
    ``execute_sql_statements`` binds the executing user with
    ``set_current_user`` inside the worker without ever resetting it, so
    without a fresh context per job that value persists on the thread and the
    *next* job on it — the cost-estimate path, which never rebinds a user —
    resolves RLS and impersonation as the previous requester.

    Falls back to :func:`asyncio.to_thread` when the pool has not been
    installed (CLI commands, Celery workers, tests), so callers do not need
    to care whether they run inside the ASGI app.
    """
    executor = _sqllab_executor
    semaphore = _sqllab_semaphore
    if executor is None or semaphore is None:
        return await asyncio.to_thread(func, *args)

    loop = asyncio.get_running_loop()
    # Snapshot the caller's context now, on the event loop, and run the job
    # inside it — mirroring what ``asyncio.to_thread`` does internally.  Any
    # ContextVar the worker sets lands in this throwaway copy and dies with
    # the job instead of leaking onto the pooled thread.
    context = contextvars.copy_context()
    await semaphore.acquire()
    try:
        future = executor.submit(context.run, func, *args)
    except BaseException:
        semaphore.release()
        raise

    def _release(_future: Any) -> None:
        try:
            loop.call_soon_threadsafe(semaphore.release)
        except RuntimeError:
            # Loop already closed during shutdown — nothing left to admit.
            logger.debug("SQL Lab pool slot released after loop shutdown")

    future.add_done_callback(_release)
    return await asyncio.wrap_future(future, loop=loop)
