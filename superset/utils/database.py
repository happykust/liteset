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


def _sync_oauth2_access_token(database: Any, spec: Any) -> str | None:
    """Resolve the current user's OAuth2 access token for ``database`` (sync).

    1:1 with upstream ``Database.get_sqla_engine``: when the database carries an
    OAuth2 client config and a current user is bound, fetch the stored per-user
    token so it can be threaded into impersonation (e.g. the Trino Bearer
    ``http_session``). Returns ``None`` when the database isn't OAuth2, no user
    is bound, or no valid token exists — impersonation then proceeds tokenless.
    """
    try:
        get_config = getattr(database, "get_oauth2_config", None)
        oauth2_config = get_config() if get_config else None
        if not oauth2_config:
            return None
        from superset.utils.core import get_user_id

        user_id = get_user_id()
        if user_id is None:
            return None
        from superset.utils.oauth2 import sync_get_oauth2_access_token

        return sync_get_oauth2_access_token(
            oauth2_config, database.id, user_id, spec
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("OAuth2 access-token resolution skipped: %s", exc)
        return None


def _get_query_source_from_request() -> Any:
    """Resolve the :class:`QuerySource` from the in-flight request, or ``None``.

    1:1 with ``superset_old/utils/core.py::get_query_source_from_request``,
    which inspected Flask's thread-local ``request.referrer``.  In the async
    port the active request is bound to a ContextVar
    (``superset.utils.core.get_current_request``); Litestar exposes the
    ``Referer`` header as ``request.headers.get("referer")``.  Returns ``None``
    when there is no request / no referrer (Celery, CLI), matching upstream's
    ``if not request or not request.referrer`` guard.
    """
    try:
        from superset.utils.core import get_current_request, QuerySource

        request = get_current_request()
        if request is None:
            return None
        referrer = None
        headers = getattr(request, "headers", None)
        if headers is not None:
            referrer = headers.get("referer") or headers.get("referrer")
        if not referrer:
            return None
        if "/superset/dashboard/" in referrer:
            return QuerySource.DASHBOARD
        if "/explore/" in referrer:
            return QuerySource.CHART
        if "/sqllab/" in referrer:
            return QuerySource.SQL_LAB
    except Exception:  # noqa: BLE001
        return None
    return None


def _apply_connection_hooks(
    database: Any,
    sqlalchemy_url: Any,
    engine_kwargs: dict[str, Any],
    source: Any | None,
) -> tuple[Any, dict[str, Any]]:
    """Apply the encrypted-extra merge and ``DB_CONNECTION_MUTATOR`` hooks.

    1:1 with the tail of ``superset_old/models/core.py::_get_sqla_engine``
    (lines 536-547), applied right before ``create_engine``:

    1. ``database.update_params_from_encrypted_extra(engine_kwargs)`` — the
       generic wrapper delegates to the engine spec, merging encrypted-extra
       connect args into the engine kwargs.
    2. ``DB_CONNECTION_MUTATOR(url, params, effective_username,
       security_manager, source)`` — when configured, lets operators rewrite
       the URL / engine kwargs.  ``source`` falls back to the request-derived
       value exactly as upstream (``source or get_query_source_from_request()``).

    Returns the (possibly mutated) ``(sqlalchemy_url, engine_kwargs)`` tuple.
    No-op (identity) when encrypted_extra is empty and the mutator is unset.
    """
    # (1) Merge encrypted-extra connect args via the engine-spec wrapper.
    try:
        if hasattr(database, "update_params_from_encrypted_extra"):
            database.update_params_from_encrypted_extra(engine_kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.debug("update_params_from_encrypted_extra skipped: %s", exc)

    # (2) DB_CONNECTION_MUTATOR.
    try:
        from superset.config import SupersetSettings

        settings = SupersetSettings()  # type: ignore[call-arg]
        mutator = getattr(settings, "db_connection_mutator", None)
    except Exception:  # noqa: BLE001
        mutator = None

    if mutator:
        source = source or _get_query_source_from_request()
        effective_username = _impersonation_username(
            database.get_effective_user(sqlalchemy_url)
            if hasattr(database, "get_effective_user")
            else None
        )
        try:
            from superset.security.manager import get_sync_security_manager_proxy

            security_manager = get_sync_security_manager_proxy()
        except Exception:  # noqa: BLE001
            security_manager = None
        sqlalchemy_url, engine_kwargs = mutator(
            sqlalchemy_url,
            engine_kwargs,
            effective_username,
            security_manager,
            source,
        )

    return sqlalchemy_url, engine_kwargs


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
    source: Any | None = None,
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
                    access_token = _sync_oauth2_access_token(database, spec)
                    url_obj, _ek = spec.impersonate_user(
                        database,
                        effective,
                        access_token,
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

        # Apply the encrypted-extra merge + DB_CONNECTION_MUTATOR hooks right
        # before ``create_engine`` — 1:1 with upstream
        # ``Database._get_sqla_engine`` (superset_old/models/core.py:536-547).
        # The mutator receives a ``URL`` object (as upstream does); render the
        # possibly-mutated URL back to a password-revealing string for
        # ``create_engine`` (``str(URL)`` masks the password under SA 2.0).
        from sqlalchemy.engine import make_url
        from sqlalchemy.engine.url import URL

        sqlalchemy_url, engine_kwargs = _apply_connection_hooks(
            database,
            make_url(sync_uri),
            engine_kwargs,
            source,
        )
        if isinstance(sqlalchemy_url, URL):
            sync_uri = sqlalchemy_url.render_as_string(hide_password=False)
        else:
            sync_uri = str(sqlalchemy_url)

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
    # driver-agnostic), so use ``database.db_engine_spec``. The OAuth2
    # access_token is intentionally NOT resolved here: every OAuth2-impersonation
    # engine (Trino/BigQuery/Snowflake/Databricks/GSheets) is sync-only and
    # connects via ``get_sync_engine`` (where the token IS threaded). An async
    # driver implies a non-OAuth2 backend (postgres/mysql), so None is correct.
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

    # Apply the encrypted-extra merge + DB_CONNECTION_MUTATOR hooks right
    # before ``create_async_engine`` — 1:1 with upstream
    # ``Database._get_sqla_engine`` (superset_old/models/core.py:536-547). A
    # mutator that rewrites the URL / connect args must be respected on the
    # async runtime path too, otherwise it would be silently ignored for
    # postgres/mysql. ``pool_pre_ping``/``pool_size``/``max_overflow`` are
    # async-engine-only kwargs and are intentionally kept out of the
    # mutator-visible ``engine_kwargs`` (upstream's mutator never saw them).
    from sqlalchemy.engine import make_url
    from sqlalchemy.engine.url import URL

    async_engine_kwargs: dict[str, Any] = {"connect_args": connect_args}
    mutated_url, async_engine_kwargs = _apply_connection_hooks(
        database,
        make_url(adjusted_uri),
        async_engine_kwargs,
        None,
    )
    if isinstance(mutated_url, URL):
        adjusted_uri = mutated_url.render_as_string(hide_password=False)
    else:
        adjusted_uri = str(mutated_url)
    connect_args = async_engine_kwargs.get("connect_args", connect_args)

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
