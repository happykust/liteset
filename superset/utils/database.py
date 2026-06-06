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
"""Async engine utilities for creating connections to user databases.

These are *not* for the Superset metadata database but for the data
source databases registered in the ``dbs`` table.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager, nullcontext
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from superset.db.engine_specs import get_async_engine_spec
from superset.db.engine_specs.base import BaseAsyncEngineSpec

logger = logging.getLogger(__name__)

# Map common sync driver prefixes to their async equivalents.
_SYNC_TO_ASYNC_DRIVERS: dict[str, str] = {
    "postgresql://": "postgresql+asyncpg://",
    "postgresql+psycopg2://": "postgresql+asyncpg://",
    "mysql://": "mysql+asyncmy://",
    "mysql+pymysql://": "mysql+asyncmy://",
    "sqlite://": "sqlite+aiosqlite://",
}


def _to_async_uri(uri: str) -> str:
    """Convert a synchronous SQLAlchemy URI to its async equivalent."""
    for sync_prefix, async_prefix in _SYNC_TO_ASYNC_DRIVERS.items():
        if uri.startswith(sync_prefix):
            return uri.replace(sync_prefix, async_prefix, 1)
    # Already async or unknown — return as-is
    return uri


def database_has_async_driver(database: Any) -> bool:
    """Return ``True`` if the database can be reached over an async driver.

    Only the backends in :data:`_SYNC_TO_ASYNC_DRIVERS` (postgres / mysql /
    sqlite) have async SQLAlchemy drivers; engines like Trino or ClickHouse
    are sync-only (their DBAPI has no asyncio support).  Callers that need a
    connection for metadata introspection use this to decide between the
    async path (:func:`get_async_connection` + ``conn.run_sync``) and a
    thread-offloaded sync path (:func:`get_sync_connection`).
    """
    uri = getattr(database, "sqlalchemy_uri", "") or ""
    # Already an async prefix, or convertible to one → async-capable.
    if any(uri.startswith(prefix) for prefix in _SYNC_TO_ASYNC_DRIVERS.values()):
        return True
    return _to_async_uri(uri) != uri


def get_engine_spec_for_database(database: Any) -> type[BaseAsyncEngineSpec]:
    """Resolve the async engine spec for a Database model object."""
    backend = getattr(database, "backend", "")
    if not backend:
        uri = getattr(database, "sqlalchemy_uri", "")
        if "://" in uri:
            backend = uri.split("://")[0].split("+")[0]
    return get_async_engine_spec(backend)


def _impersonation_username(effective: str | None) -> str | None:
    """Apply ``IMPERSONATE_WITH_EMAIL_PREFIX`` to an effective username.

    1:1 with upstream ``Database.get_sqla_engine`` (``superset_old/models/core.py``):
    when the feature flag is on, the effective username is rewritten to the
    local-part of the user's email. Upstream looks the user up by username;
    the port resolves the *current request* user's email directly via the
    user context-var (``get_user_email``) — which is exactly where
    ``Database.get_effective_user`` sourced the username from
    (``get_username()``), so the two agree whenever impersonation targets the
    logged-in user.
    """
    if not effective:
        return effective
    try:
        from superset.utils.feature_flags import feature_flag_manager

        if not feature_flag_manager.is_feature_enabled(
            "IMPERSONATE_WITH_EMAIL_PREFIX"
        ):
            return effective
        from superset.utils.core import get_user_email

        email = get_user_email()
        if email:
            return email.split("@")[0]
    except Exception:  # noqa: BLE001
        pass
    return effective


def _to_sync_uri(uri: str) -> str:
    """Convert an async SQLAlchemy URI to its sync equivalent.

    Inverse of :func:`_to_async_uri`. Used by :func:`get_sync_engine`
    so a Database model registered with an async-only URI (e.g.
    ``postgresql+asyncpg://``) can still be inspected via the
    synchronous SQLAlchemy engine when the helpers ``ExploreMixin``
    code path needs ``with database.get_sqla_engine() as engine``.
    """
    async_to_sync = {v: k for k, v in _SYNC_TO_ASYNC_DRIVERS.items()}
    for async_prefix, sync_prefix in async_to_sync.items():
        if uri.startswith(async_prefix):
            return uri.replace(async_prefix, sync_prefix, 1)
    return uri


@contextmanager
def get_sync_engine(
    database: Any,
    catalog: str | None = None,
    schema: str | None = None,
    nullpool: bool = True,
    override_ssh_tunnel: Any | None = None,
) -> Iterator[Engine]:
    """Yield a synchronous SQLAlchemy ``Engine`` for the given database.

    1:1 with ``Database.get_sqla_engine`` in
    ``superset_old/models/core.py``
    (line 568). Used by helpers.ExploreMixin code paths that compile
    a SELECT statement and read ``engine.dialect`` for identifier
    quoting and ``%%`` double-percent fixup detection.

    The engine is disposed when the context exits so we don't hold
    onto pooled connections after the helper finishes.
    """
    uri = getattr(database, "sqlalchemy_uri_decrypted", None) or getattr(
        database, "sqlalchemy_uri", ""
    )
    sqlalchemy_uri = str(uri)

    # SSH tunnel: when one is supplied, open it, rewrite the URL to the local
    # bind endpoint, and tear it down when the engine context exits — mirroring
    # ``superset_old`` ``Database.get_sqla_engine``'s ssh_context_manager.  Only
    # an explicit ``override_ssh_tunnel`` is honoured here; the original also
    # auto-resolved the stored tunnel via the (sync) ``DatabaseDAO.get_ssh_tunnel``,
    # which the async port leaves to callers (they pass it as the override).
    if override_ssh_tunnel is not None:
        from superset.extensions import ssh_manager_factory

        ssh_manager: Any = ssh_manager_factory.instance
        tunnel_cm: Any = ssh_manager.create_tunnel(
            ssh_tunnel=override_ssh_tunnel,
            sqlalchemy_database_uri=sqlalchemy_uri,
        )
    else:
        ssh_manager = None
        tunnel_cm = nullcontext()

    with tunnel_cm as tunnel_server:
        if tunnel_server is not None and ssh_manager is not None:
            logger.info(
                "[SSH] Using tunnel at %s", tunnel_server.local_bind_address
            )
            sqlalchemy_uri = str(
                ssh_manager.build_sqla_url(sqlalchemy_uri, tunnel_server)
            )

        sync_uri = _to_sync_uri(sqlalchemy_uri)

        # Engine extra params from the database's ``extra`` JSON.  The
        # heavy ``adjust_engine_params`` flow (BigQuery, Hive…) is
        # intentionally bypassed here — those specs require Flask app
        # context which is not available in the async runtime.  Liteset
        # users that need engine-spec-specific connect args set them via
        # ``extra.engine_params.connect_args`` in the dataset configuration.
        connect_args: dict[str, Any] = {}
        try:
            extra = database.get_extra() if hasattr(database, "get_extra") else {}
            engine_params = (extra or {}).get("engine_params") or {}
            connect_args = engine_params.get("connect_args") or {}
        except Exception:  # noqa: BLE001
            connect_args = {}

        # Catalog/schema scoping: engines that carry the catalog (and schema) in
        # the URL — Trino: ``trino://host/<catalog>/<schema>`` — need the
        # requested namespace merged into the URI, otherwise the inspector binds
        # to the URI's *default* catalog and a non-default catalog/schema browses
        # the wrong namespace.  Apply the engine spec's (pure-URI) adjust hook
        # only when a catalog/schema override is supplied; defensively fall back
        # to the raw URI if the spec needs unavailable context.
        if (catalog or schema) and not database_has_async_driver(database):
            try:
                from sqlalchemy.engine import make_url

                from superset.db_engine_specs import get_engine_spec

                # The *sync* engine spec carries the catalog/schema-aware
                # ``adjust_engine_params(uri, connect_args, catalog, schema)``
                # (the async spec's hook has a different, catalog-less
                # signature), so resolve it directly rather than via
                # ``get_engine_spec_for_database`` (which returns the async one).
                sync_spec = getattr(database, "db_engine_spec", None)
                if sync_spec is None:
                    backend = (sync_uri.split("://", 1)[0] or "").split("+")[0]
                    sync_spec = get_engine_spec(backend, "")
                adjusted_url, connect_args = sync_spec.adjust_engine_params(
                    make_url(sync_uri),
                    connect_args,
                    catalog=catalog,
                    schema=schema,
                )
                sync_uri = adjusted_url.render_as_string(hide_password=False)
            except Exception as exc:  # noqa: BLE001
                logger.debug("adjust_engine_params skipped for sync engine: %s", exc)

        # Impersonation: when ``impersonate_user`` is enabled, let the engine
        # spec rewrite the URL / connect_args to run queries as the effective
        # user (the current request user via ``get_effective_user``) — 1:1 with
        # upstream ``Database.get_sqla_engine``. The OAuth2 ``access_token`` is
        # not threaded into the sync path (deferred); the ``connect_args["user"]``
        # impersonation (Trino/Presto/Hive) works without it.
        if getattr(database, "impersonate_user", False):
            try:
                from sqlalchemy.engine import make_url

                spec = getattr(database, "db_engine_spec", None)
                if spec is not None and hasattr(spec, "impersonate_user"):
                    url_obj = make_url(sync_uri)
                    effective = _impersonation_username(
                        database.get_effective_user(url_obj)
                    )
                    url_obj, _ek = spec.impersonate_user(
                        database,
                        effective,
                        None,
                        url_obj,
                        {"connect_args": connect_args},
                    )
                    sync_uri = url_obj.render_as_string(hide_password=False)
                    connect_args = _ek.get("connect_args", connect_args)
            except Exception as exc:  # noqa: BLE001
                logger.debug("impersonation skipped for sync engine: %s", exc)

        engine_kwargs: dict[str, Any] = {"connect_args": connect_args}
        if nullpool:
            from sqlalchemy.pool import NullPool

            engine_kwargs["poolclass"] = NullPool

        engine = create_engine(sync_uri, **engine_kwargs)
        try:
            yield engine
        finally:
            engine.dispose()


@contextmanager
def get_sync_connection(
    database: Any,
) -> Iterator[tuple[Connection, type[BaseAsyncEngineSpec]]]:
    """Yield a synchronous SQLAlchemy ``Connection`` and async engine spec.

    Used by :class:`SqlaTable` for metadata introspection paths that
    cannot be async (column reflection, virtual-dataset
    ``LIMIT 0`` probe). Returns the async engine spec so callers can
    still use type-mapping helpers shared between sync and async.
    """
    spec = get_engine_spec_for_database(database)
    with get_sync_engine(database) as engine:
        with engine.connect() as conn:
            yield conn, spec


@asynccontextmanager
async def get_async_connection(
    database: Any,
) -> AsyncIterator[tuple[AsyncConnection, type[BaseAsyncEngineSpec]]]:
    """Create a temporary async connection to a user database.

    Yields ``(conn, engine_spec)`` so callers can use the engine spec
    methods that need a connection.

    The connection and engine are disposed after the context exits.
    """
    # Use the *decrypted* URI so the encrypted ``password`` column is
    # merged back in.  ``sqlalchemy_uri`` is stored masked
    # (``XXXXXXXXXX``) per the original Apache Superset contract, so
    # connecting with it would always fail authentication.
    uri = getattr(database, "sqlalchemy_uri_decrypted", None) or getattr(
        database, "sqlalchemy_uri", ""
    )
    async_uri = _to_async_uri(uri)

    engine_spec = get_engine_spec_for_database(database)
    adjusted_uri, connect_args = engine_spec.adjust_engine_params(async_uri)

    # Impersonation (1:1 upstream ``Database.get_sqla_engine``): rewrite the
    # URL / connect_args to run as the effective user. The impersonation hook
    # lives on the *sync* engine spec (it's a pure URL/connect_args transform,
    # driver-agnostic), so use ``database.db_engine_spec``. OAuth2 access_token
    # is not threaded here (deferred).
    if getattr(database, "impersonate_user", False):
        try:
            from sqlalchemy.engine import make_url

            sync_spec = getattr(database, "db_engine_spec", None)
            if sync_spec is not None and hasattr(sync_spec, "impersonate_user"):
                url_obj = make_url(adjusted_uri)
                effective = _impersonation_username(
                    database.get_effective_user(url_obj)
                )
                url_obj, _ek = sync_spec.impersonate_user(
                    database, effective, None, url_obj, {"connect_args": connect_args}
                )
                adjusted_uri = url_obj.render_as_string(hide_password=False)
                connect_args = _ek.get("connect_args", connect_args)
        except Exception as exc:  # noqa: BLE001
            logger.debug("impersonation skipped for async connection: %s", exc)

    engine = create_async_engine(
        adjusted_uri,
        connect_args=connect_args,
        pool_pre_ping=True,
        pool_size=1,
        max_overflow=0,
    )
    try:
        async with engine.connect() as conn:
            yield conn, engine_spec
    finally:
        await engine.dispose()
