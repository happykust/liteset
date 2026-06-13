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

from sqlalchemy import create_engine as _create_sync_engine
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.ext.asyncio import (
    async_sessionmaker,
    AsyncEngine,
    AsyncSession,
    create_async_engine as _create_async_engine,
)
from sqlalchemy.orm import scoped_session, Session, sessionmaker
from sqlalchemy.pool import NullPool

# Dialects that should default to READ COMMITTED when the caller hasn't
# already specified ``isolation_level`` in SQLALCHEMY_ENGINE_OPTIONS.
# This mirrors ``SupersetAppInitializer.set_db_default_isolation``.
_READ_COMMITTED_DIALECTS: frozenset[str] = frozenset({"postgresql", "mysql"})


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
    Named parameters take precedence when the same key appears in both.

    If ``isolation_level`` is not already present in ``extra_engine_options``
    and the target dialect is PostgreSQL or MySQL, ``isolation_level`` is set
    to ``"READ COMMITTED"`` at engine-creation time so that **all** connections
    drawn from the pool use that level.  This is a 1:1 port of the original
    ``SupersetAppInitializer.set_db_default_isolation`` which called
    ``db.engine.execution_options(isolation_level=...)`` — setting it once on
    the engine rather than on individual connections.
    """
    global _engine  # noqa: PLW0603
    kwargs: dict[str, Any] = {"echo": echo, **extra_engine_options}
    if not url.startswith("sqlite"):
        kwargs.setdefault("pool_size", pool_size)
        kwargs.setdefault("max_overflow", max_overflow)
        kwargs.setdefault("pool_timeout", pool_timeout)
        kwargs.setdefault("pool_recycle", pool_recycle)
        kwargs.setdefault("pool_pre_ping", pool_pre_ping)

    # Set READ COMMITTED at engine level for PG/MySQL when not already specified.
    if "isolation_level" not in kwargs:
        try:
            dialect_name = make_url(url).get_dialect().name
        except Exception:  # noqa: BLE001
            dialect_name = ""
        if dialect_name in _READ_COMMITTED_DIALECTS:
            kwargs["isolation_level"] = "READ COMMITTED"

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


def create_worker_engine(url: str, **extra_engine_options: Any) -> AsyncEngine:
    """Create the process-wide async engine for a Celery worker process.

    Unlike :func:`create_db_engine` (used by the long-lived web process, which
    runs a single event loop and benefits from a connection pool), Celery
    prefork workers run async work via ``asyncio.run()`` — a **fresh event loop
    per task**.  asyncpg connections are bound to the loop they were opened on,
    so a *pooled* connection reused across loops raises
    ``RuntimeError: ... attached to a different loop``.  We therefore force
    :class:`~sqlalchemy.pool.NullPool`: every checkout opens a fresh connection
    and closes it on return, so nothing survives between ``asyncio.run()`` calls.

    Sets the module-global engine so :func:`get_engine` works inside tasks —
    mirroring what the Litestar ``on_startup`` hook does for the web process.
    READ COMMITTED is applied for PG/MySQL exactly as :func:`create_db_engine`.
    """
    global _engine  # noqa: PLW0603
    kwargs: dict[str, Any] = {"poolclass": NullPool, **extra_engine_options}
    if "isolation_level" not in kwargs:
        try:
            dialect_name = make_url(url).get_dialect().name
        except Exception:  # noqa: BLE001
            dialect_name = ""
        if dialect_name in _READ_COMMITTED_DIALECTS:
            kwargs["isolation_level"] = "READ COMMITTED"
    engine = _create_async_engine(url, **kwargs)
    _engine = engine
    return engine


async def dispose_engine(engine: AsyncEngine) -> None:
    await engine.dispose()


# ---------------------------------------------------------------------------
# Sync session helpers (for Celery workers)
# ---------------------------------------------------------------------------

_sync_engine: Engine | None = None
_sync_session_factory: scoped_session[Session] | None = None

_ASYNC_TO_SYNC_DRIVERS: dict[str, str] = {
    "postgresql+asyncpg": "postgresql+psycopg2",
    "sqlite+aiosqlite": "sqlite",
    "mysql+aiomysql": "mysql+pymysql",
}


def _ensure_sync_session_factory() -> scoped_session[Session]:
    """Lazily initialise the module-level scoped_session registry.

    The sync engine is created once per process, converting the async database
    URI from :class:`~superset.config.SupersetSettings` into its synchronous
    equivalent (e.g. ``asyncpg`` -> ``psycopg2``).

    Returns a :class:`~sqlalchemy.orm.scoped_session` so that all callers in
    the same thread (task body *and* the ``task_postrun`` teardown handler)
    share the **same** :class:`~sqlalchemy.orm.Session` object — exactly
    mirroring the upstream ``db.session`` which is itself a
    ``scoped_session`` proxy.
    """
    global _sync_engine, _sync_session_factory  # noqa: PLW0603
    if _sync_engine is None:
        if _engine is not None:
            # ``str(engine.url)`` masks the password as ``***`` in SA 2.0+,
            # so a downstream connection would silently fail auth.  Render
            # the URL explicitly with the password preserved.
            sync_uri = _engine.url.render_as_string(hide_password=False)
        else:
            # Celery workers and ``superset`` CLI invocations boot without
            # going through the Litestar startup hook, so ``_engine`` is
            # None.  Resolve the URI via ``SupersetSettings`` which honours
            # both LITESET_* env vars *and* SUPERSET_CONFIG_PATH — going
            # straight to ``os.environ`` would silently fall back to
            # sqlite when the URI lives in superset_config.py.
            from superset.config import SupersetSettings

            sync_uri = SupersetSettings().sqlalchemy_database_uri  # type: ignore[call-arg]
        for async_prefix, sync_prefix in _ASYNC_TO_SYNC_DRIVERS.items():
            if sync_uri.startswith(async_prefix):
                sync_uri = sync_uri.replace(async_prefix, sync_prefix, 1)
                break
        _sync_engine = _create_sync_engine(sync_uri)
        _sync_session_factory = scoped_session(sessionmaker(bind=_sync_engine))
    assert _sync_session_factory is not None  # noqa: S101
    return _sync_session_factory


def get_sync_session() -> Session:
    """Return the thread-local sync :class:`~sqlalchemy.orm.Session`.

    Uses a :class:`~sqlalchemy.orm.scoped_session` registry so that all code
    executing in the same Celery worker thread — including both the task body
    and the ``task_postrun`` teardown handler — receives the **same** Session
    object.  This matches the behaviour of the upstream ``db.session``
    (which is also a ``scoped_session``) in the original Superset.

    Call :func:`remove_sync_session` in ``task_postrun`` to deregister the
    thread-local session and release its connection back to the pool.
    """
    return _ensure_sync_session_factory()()


def remove_sync_session() -> None:
    """Remove the thread-local sync session from the scoped registry.

    Equivalent to the upstream ``db.session.remove()``.  Should be
    called once per Celery task in the ``task_postrun`` signal handler so
    that the connection is released and the thread-local registry entry is
    cleared before the worker thread is reused.

    Safe to call even if the session was already closed by task code — the
    scoped_session.remove() call is idempotent in that case.
    """
    if _sync_session_factory is not None:
        _sync_session_factory.remove()
