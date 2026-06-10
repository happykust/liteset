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
from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import (
    async_sessionmaker,
    AsyncEngine,
    AsyncSession,
    create_async_engine as _create_async_engine,
)
from sqlalchemy.orm import Session


def create_db_engine(
    url: str,
    echo: bool = False,
    pool_size: int = 5,
    max_overflow: int = 10,
    pool_timeout: int = 30,
    pool_recycle: int = 3600,
    pool_pre_ping: bool = True,
    **extra_engine_options: Any,
) -> AsyncEngine:
    """Create an async SQLAlchemy engine.

    The named parameters represent the most common engine-creation knobs.
    Any additional keyword arguments supplied via ``SQLALCHEMY_ENGINE_OPTIONS``
    (e.g. ``isolation_level``, ``connect_args``, ``execution_options``) are
    forwarded verbatim to ``create_async_engine`` via ``**extra_engine_options``.

    No isolation_level is injected automatically.  The original
    ``SupersetAppInitializer.set_db_default_isolation`` called
    ``db.engine.execution_options(isolation_level=...)`` without storing the
    returned OptionEngine, making it a silent no-op.  Callers who need a
    specific isolation level must pass it explicitly via
    ``SQLALCHEMY_ENGINE_OPTIONS``.
    """
    global _engine  # noqa: PLW0603
    kwargs: dict[str, Any] = {"echo": echo, **extra_engine_options}
    if not url.startswith("sqlite"):
        kwargs.setdefault("pool_size", pool_size)
        kwargs.setdefault("max_overflow", max_overflow)
        kwargs.setdefault("pool_timeout", pool_timeout)
        kwargs.setdefault("pool_recycle", pool_recycle)
        kwargs.setdefault("pool_pre_ping", pool_pre_ping)

    engine = _create_async_engine(url, **kwargs)
    _engine = engine
    return engine


def create_session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


_engine: AsyncEngine | None = None


def get_engine() -> AsyncEngine:
    """Return the module-level engine reference.

    Raises RuntimeError if no engine has been created yet via
    :func:`create_db_engine`.
    """
    if _engine is None:
        raise RuntimeError("No engine has been created yet")
    return _engine


async def dispose_engine(engine: AsyncEngine) -> None:
    await engine.dispose()


# ---------------------------------------------------------------------------
# Sync session helpers (for Celery workers)
# ---------------------------------------------------------------------------


def get_sync_session() -> Session:
    """Return the thread-local sync Session (delegates to ``superset.db.session``).

    Historical duplicate: this module once built its OWN non-scoped
    sessionmaker, handing every caller a brand-new Session with no
    ``remove_sync_session()`` lifecycle — leaking pool connections for any
    Celery-facing code that imported it from here. Delegate to the canonical
    scoped-session registry instead so both entry points share one
    thread-local Session.
    """
    from superset.db.session import get_sync_session as _canonical

    return _canonical()
