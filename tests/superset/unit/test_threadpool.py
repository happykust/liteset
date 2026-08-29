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
"""Sizing and admission control for the blocking-work thread pools."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from superset.utils import threadpool


@pytest.fixture(autouse=True)
def _clean_pools():
    yield
    threadpool.shutdown_executors()


def test_configured_sizes_win_over_computed_defaults() -> None:
    settings = SimpleNamespace(
        asyncio_max_worker_threads=7,
        sqllab_max_worker_threads=3,
    )
    assert threadpool.default_pool_size(settings) == 7
    assert threadpool.sqllab_pool_size(settings) == 3


def test_computed_defaults_are_bounded() -> None:
    settings = SimpleNamespace(
        asyncio_max_worker_threads=None,
        sqllab_max_worker_threads=None,
    )
    assert 16 <= threadpool.default_pool_size(settings) <= 64
    assert 8 <= threadpool.sqllab_pool_size(settings) <= 32


async def test_install_default_executor_replaces_loop_default() -> None:
    settings = SimpleNamespace(asyncio_max_worker_threads=5)
    executor = threadpool.install_default_executor(settings)
    try:
        assert executor._max_workers == 5
        # ``asyncio.to_thread`` must land on the installed pool.
        name = await asyncio.to_thread(lambda: threading.current_thread().name)
        assert name.startswith("liteset-offload")
    finally:
        executor.shutdown(wait=False)


async def test_sqllab_slot_is_held_until_the_thread_finishes() -> None:
    """A timed-out query keeps its slot until the driver call returns.

    ``asyncio.wait_for`` cancels the awaiting future, never the thread.  If
    the semaphore were released on cancellation the pool would admit a new
    query while the abandoned one still occupies a thread — precisely the
    over-admission that makes a saturated worker fall over.
    """
    settings = SimpleNamespace(sqllab_max_worker_threads=1)
    threadpool.install_sqllab_executor(settings)
    # Enlarge the underlying pool beyond what the size-1 admission semaphore
    # permits. With ``ThreadPoolExecutor(max_workers=1)`` (what
    # ``install_sqllab_executor`` itself would create), the pool alone
    # would already block a second submission from running concurrently
    # with the first, satisfying the assertions below regardless of
    # whether the semaphore holds its slot correctly -- e.g. even if it
    # were released as soon as ``asyncio.wait_for`` cancels the awaiting
    # coroutine. Reaching into the private pool handle is deliberate: it
    # decouples pool concurrency from semaphore admission so only the
    # semaphore's behaviour can explain what this test observes.
    threadpool._sqllab_executor = ThreadPoolExecutor(
        max_workers=4, thread_name_prefix="liteset-sqllab-test"
    )

    release_first = threading.Event()
    second_started = threading.Event()

    def _first() -> str:
        release_first.wait(timeout=5)
        return "first"

    def _second() -> str:
        second_started.set()
        return "second"

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(threadpool.run_sqllab_blocking(_first), timeout=0.1)

    # The abandoned thread still holds the only slot.
    queued = asyncio.ensure_future(threadpool.run_sqllab_blocking(_second))
    await asyncio.sleep(0.1)
    assert not second_started.is_set()

    # Once it finishes, the slot is handed on and the queued call proceeds.
    release_first.set()
    assert await asyncio.wait_for(queued, timeout=5) == "second"


async def test_run_sqllab_blocking_without_pool_falls_back_to_to_thread() -> None:
    """CLI / Celery / test contexts never install the pool."""
    threadpool.shutdown_executors()
    assert await threadpool.run_sqllab_blocking(lambda: 42) == 42


async def test_each_job_runs_in_its_own_context_copy() -> None:
    """A pooled thread must not carry one request's identity into the next.

    ``asyncio.to_thread`` copies the caller's context per call; a bare
    ``executor.submit`` does not. ``execute_sql_statements`` binds the user
    with ``set_current_user`` inside the worker and never resets it, so
    without a per-job copy that value survives on the reused thread and the
    next job — the cost-estimate path, which never rebinds a user — resolves
    RLS and impersonation as the previous requester.
    """
    from types import SimpleNamespace

    from superset.utils.core import get_current_user, set_current_user

    threadpool.install_sqllab_executor(SimpleNamespace(sqllab_max_worker_threads=1))

    # The worker sees the identity of the request that submitted it.
    set_current_user(SimpleNamespace(id=1, username="alice"))
    seen = await threadpool.run_sqllab_blocking(
        lambda: getattr(get_current_user(), "username", None)
    )
    assert seen == "alice"

    # A user bound inside one job dies with that job's context copy.
    def _job_binding_a_user() -> str:
        set_current_user(SimpleNamespace(id=9, username="mallory"))
        return "bound"

    assert await threadpool.run_sqllab_blocking(_job_binding_a_user) == "bound"

    set_current_user(None)
    leaked = await threadpool.run_sqllab_blocking(
        lambda: getattr(get_current_user(), "username", None)
    )
    assert leaked is None, "identity leaked across jobs on a reused pool thread"
