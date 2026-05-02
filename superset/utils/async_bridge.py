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
"""Sync→async bridge helpers.

Several legacy code paths in the Liteset port still ship synchronous
APIs but need to drive an async backend underneath:

* ``memoized_func`` (in :mod:`superset.utils.cache`) decorates sync
  functions whose call sites pre-date the async migration; they still
  need to read/write through the async :class:`AsyncCacheProtocol`.
* The thumbnail / screenshot pipeline in :mod:`superset.utils.screenshots`
  uses Selenium / Playwright / Pillow which are pure-sync libraries; the
  ``_SyncThumbnailCacheAdapter`` shim hits the same async cache.

Both code paths historically kept their own private ``_run_async``
helpers with subtly-different deadlock-detection logic.  This module
centralises that helper so:

1. There is **one canonical implementation** of the deadlock guard —
   future fixes only need to land here.
2. The implementation is **portable across event-loop policies**
   (stdlib asyncio, ``uvloop``, any custom subclass).  We deliberately
   avoid private CPython-asyncio internals (``BaseEventLoop`` exposes a
   thread-id attribute that ``uvloop.Loop`` does not), and instead
   detect the on-loop-thread condition via the stable public
   :func:`asyncio.get_running_loop` API: it succeeds iff the calling
   thread is currently inside a coroutine on a loop.
3. Cross-thread scheduling is opt-in via ``parent_loop=`` so callers
   that *know* they were spawned by ``asyncio.to_thread`` from a still-
   running parent loop can route their cache reads back to that loop
   instead of spinning a brand-new one.  This avoids creating a fresh
   asyncio Redis connection pool per worker invocation.

Usage
-----

::

    from superset.utils.async_bridge import run_async

    # 1) Plain sync caller — Celery worker, CLI, alembic migration.
    result = run_async(some_coro())

    # 2) Sync caller dispatched via ``asyncio.to_thread`` — capture the
    #    parent loop in the async dispatcher and forward it through:
    parent_loop = asyncio.get_running_loop()
    result = await asyncio.to_thread(
        some_sync_fn, parent_loop=parent_loop,
    )
    # ...inside ``some_sync_fn``:
    def some_sync_fn(*, parent_loop=None):
        return run_async(some_coro(), parent_loop=parent_loop)

    # 3) Misuse — calling from inside a coroutine on the same thread —
    #    raises ``RuntimeError`` with an actionable message rather than
    #    deadlocking the event loop.
    async def handler():
        run_async(coro())  # -> RuntimeError
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Coroutine

logger = logging.getLogger(__name__)


def run_async(
    coro: Coroutine[Any, Any, Any],
    *,
    parent_loop: asyncio.AbstractEventLoop | None = None,
) -> Any:
    """Run ``coro`` to completion from synchronous code.

    Parameters
    ----------
    coro:
        The coroutine to execute.  Ownership of ``coro`` transfers to
        this function — on the deadlock-guard path we ``coro.close()``
        the awaitable to suppress the ``"coroutine was never awaited"``
        warning before raising.
    parent_loop:
        Optional event loop running in *another* thread.  When supplied,
        the coroutine is scheduled on that loop via
        :func:`asyncio.run_coroutine_threadsafe` and we block on the
        returned :class:`concurrent.futures.Future` until the result is
        available.  Used by callers spawned through
        :func:`asyncio.to_thread`/``run_in_executor`` that want to keep
        loop-affine resources (connection pools, etc.) attached to the
        parent loop.

    Returns
    -------
    Any
        Whatever ``coro`` resolves to.  Exceptions raised inside the
        coroutine propagate to the caller unchanged so surrounding
        retry/fallback logic can react to them.

    Raises
    ------
    RuntimeError
        If the caller is itself running inside a coroutine on a live
        event loop in the *current* thread (which would deadlock the
        loop while ``run_coroutine_threadsafe`` waited for a result the
        loop is too busy to produce).  Detection works on stdlib asyncio
        AND :mod:`uvloop` because we use the stable public
        :func:`asyncio.get_running_loop` API rather than reaching for
        any private thread-id attribute (``uvloop`` does not expose one).
    """
    if parent_loop is not None:
        # Cross-thread schedule onto a known parent loop.  This branch
        # implicitly assumes the caller is NOT executing on
        # ``parent_loop``'s thread — passing ``parent_loop`` from the
        # very thread it runs on would deadlock.  Callers in Liteset
        # always reach this branch from a worker thread spawned by
        # ``asyncio.to_thread``, where the parent loop runs on a
        # different thread by construction, so the precondition holds.
        future = asyncio.run_coroutine_threadsafe(coro, parent_loop)
        return future.result()

    try:
        # Public API: succeeds iff *this* thread is currently executing
        # a coroutine on a running loop.  Raises ``RuntimeError`` when
        # there's no loop attached to the current thread — exactly the
        # signal we need for the "spin a fresh loop" branch.
        asyncio.get_running_loop()
    except RuntimeError:
        # No loop in this thread — safe to start a fresh one.  This is
        # the common case for Celery worker threads, CLI invocations,
        # and alembic migration scripts.
        return asyncio.run(coro)

    # We are inside an active coroutine on the current thread.  Calling
    # ``run_coroutine_threadsafe`` here would schedule on the same loop
    # whose thread we are blocking — guaranteed deadlock.  Close the
    # coroutine to suppress the ``coroutine was never awaited`` warning
    # and surface a fast, actionable failure.
    coro.close()
    raise RuntimeError(
        "run_async called from inside a running event loop on the same "
        "thread — this would deadlock waiting for a coroutine that "
        "cannot make progress.  Either:\n"
        "  * make the caller async and ``await`` the coroutine directly, or\n"
        "  * dispatch the sync caller via ``asyncio.to_thread`` "
        "(or ``run_in_executor``) so it runs off-loop, optionally "
        "forwarding ``parent_loop=asyncio.get_running_loop()`` to the "
        "worker so cache reads route back to the parent loop."
    )


__all__ = ["run_async"]
