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

import asyncio
import logging
import sys
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager, nullcontext
from typing import Any, cast

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

# Sentinel: distinguishes "caller passed effective_username=None" (no logged-in
# user) from "caller did not pass effective_username at all" (compute on-demand).
_UNSET: Any = object()


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

    When the feature flag is on, the effective username is rewritten to the
    local-part of the user's email, resolved from the current request via
    ``get_user_email()``.
    """
    if not effective:
        return effective
    try:
        from superset.utils.feature_flags import feature_flag_manager

        if not feature_flag_manager.is_feature_enabled("IMPERSONATE_WITH_EMAIL_PREFIX"):
            return effective
        from superset.utils.core import get_user_email

        email = get_user_email()
        if email:
            return email.split("@")[0]
    except Exception:  # noqa: BLE001
        logger.debug("_impersonation_username failed", exc_info=True)
    return effective


def _sync_oauth2_access_token(database: Any, spec: Any) -> str | None:
    """Resolve the current user's OAuth2 access token for ``database`` (sync).

    When the database carries an OAuth2 client config and a current user is
    bound, fetch the stored per-user token so it can be threaded into
    impersonation (e.g. the Trino Bearer ``http_session``). Returns ``None``
    when the database isn't OAuth2, no user is bound, or no valid token exists
    — impersonation then proceeds tokenless.
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

        return sync_get_oauth2_access_token(oauth2_config, database.id, user_id, spec)
    except Exception as exc:  # noqa: BLE001
        logger.debug("OAuth2 access-token resolution skipped: %s", exc)
        return None


def _get_query_source_from_request() -> Any:
    """Resolve the :class:`QuerySource` from the in-flight request, or ``None``.

    The active request is looked up via ``superset.utils.core.get_current_request``;
    the Referer header is accessed as ``request.headers.get("referer")``.
    Returns ``None`` when there is no request or no referrer (Celery, CLI).
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
    effective_username: Any = _UNSET,
) -> tuple[Any, dict[str, Any]]:
    """Apply the encrypted-extra merge and ``DB_CONNECTION_MUTATOR`` hooks.

    Applied right before ``create_engine``:

    1. ``database.update_params_from_encrypted_extra(engine_kwargs)`` — the
       generic wrapper delegates to the engine spec, merging encrypted-extra
       connect args into the engine kwargs.
    2. ``DB_CONNECTION_MUTATOR(url, params, effective_username,
       security_manager, source)`` — when configured, lets operators rewrite
       the URL / engine kwargs.  ``source`` falls back to the request-derived
       value (``source or _get_query_source_from_request()``).

    ``effective_username`` must be the value captured BEFORE ``impersonate_user``
    rewrites the URL — computed once from the post-adjust, pre-impersonation URL
    and reused for both ``impersonate_user`` and ``DB_CONNECTION_MUTATOR``.
    Callers should pass the pre-impersonation value; when ``_UNSET`` is passed
    (backward-compat default), the value is computed on-demand from the supplied URL.

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
        # Use the caller-supplied pre-impersonation value when available;
        # fall back to on-demand computation for callers that do not supply it.
        if effective_username is _UNSET:
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
def _sync_check_for_oauth2(database: Any) -> Iterator[None]:
    """Sync context manager that triggers the OAuth2 dance on auth failure.

    Wraps a block and, when the raised exception indicates OAuth2 re-auth is
    needed, triggers :meth:`start_oauth2_dance` before re-raising.

    ``start_oauth2_dance`` is declared ``async`` but performs no actual
    ``await`` — it only builds the authorization URL and raises
    :class:`OAuth2RedirectError`.  We drive the coroutine synchronously
    (same technique as ``superset.tasks.sql_lab._check_for_oauth2``).
    """
    try:
        yield
    except Exception as ex:  # noqa: BLE001
        try:
            spec = getattr(database, "db_engine_spec", None)
            is_oauth2 = (
                hasattr(database, "is_oauth2_enabled") and database.is_oauth2_enabled()
            )
            if is_oauth2 and spec is not None and spec.needs_oauth2(ex):
                dance = spec.start_oauth2_dance(database)
                if hasattr(dance, "send"):  # coroutine — drive it synchronously
                    try:
                        dance.send(None)
                    except StopIteration:
                        pass
        except Exception:  # noqa: BLE001
            # OAuth2RedirectError (or OAuth2Error) is raised by the dance to
            # start the authorization flow; let it propagate.  Any other
            # failure during the check is swallowed so we surface the
            # original query/connection error below.
            if "OAuth2" in type(sys.exc_info()[1]).__name__:
                raise
        raise


async def _resolve_ssh_tunnel(database: Any) -> Any:
    """Resolve ``database``'s configured SSH tunnel via its live ORM session.

    Mirrors upstream's ``ssh_tunnel = override_ssh_tunnel or
    DatabaseDAO.get_ssh_tunnel(self.id)`` (``superset_old/models/core.py:445``)
    for the *async* connection path, where ``database`` is reached in the
    same event loop/task it was loaded in, so reusing its own
    ``AsyncSession`` is safe.

    Returns ``None`` (no tunnel) whenever resolution isn't possible --
    e.g. ``database`` has no live session attached -- rather than raising,
    so this is safe to call unconditionally.
    """
    database_id = getattr(database, "id", None)
    if not database_id:
        return None
    try:
        from sqlalchemy.ext.asyncio import async_object_session

        session = async_object_session(database)
        if session is None:
            return None

        from superset.db.daos.database import AsyncDatabaseDAO

        return await AsyncDatabaseDAO(session).get_ssh_tunnel(database_id)
    except Exception:  # noqa: BLE001
        logger.debug(
            "Could not resolve SSH tunnel for database id=%s",
            database_id,
            exc_info=True,
        )
        return None


async def _resolve_ssh_tunnel_standalone(database: Any) -> Any:
    """Resolve ``database``'s configured SSH tunnel from a throwaway session.

    Used by the *sync* bridge (:func:`_setup_ssh_tunnel` via
    :func:`superset.utils.async_bridge.run_async`), which runs this
    coroutine on a freshly-spun event loop in a worker thread. Reusing the
    ``AsyncSession`` that loaded ``database`` (bound to the caller's own,
    different, event loop/thread) would hand an asyncpg connection to a
    loop it was never created on. A short-lived session against the
    metadata database sidesteps that entirely -- SSH-tunnelled databases
    are a narrow slice of deployments, so the extra round-trip is an
    acceptable, safe trade-off.
    """
    database_id = getattr(database, "id", None)
    if not database_id:
        return None
    try:
        from superset.config import SupersetSettings
        from superset.db.daos.database import AsyncDatabaseDAO
        from superset.db.engine import create_db_engine, create_session_factory

        settings = SupersetSettings()  # type: ignore[call-arg]
        engine = create_db_engine(settings.sqlalchemy_database_uri)
        try:
            session_factory = create_session_factory(engine)
            async with session_factory() as session:
                return await AsyncDatabaseDAO(session).get_ssh_tunnel(database_id)
        finally:
            await engine.dispose()
    except Exception:  # noqa: BLE001
        logger.debug(
            "Could not resolve SSH tunnel for database id=%s",
            database_id,
            exc_info=True,
        )
        return None


def _setup_ssh_tunnel(
    database: Any,
    sqlalchemy_uri: str,
    override_ssh_tunnel: Any,
) -> tuple[Any, Any]:
    """Return ``(ssh_manager, tunnel_cm)`` for the SSH tunnel.

    Returns ``(None, nullcontext())`` when no tunnel is configured. Honours
    an explicit ``override_ssh_tunnel``; otherwise resolves the database's
    own configured tunnel (see :func:`_resolve_ssh_tunnel_standalone`) so a
    bastion-only database isn't contacted directly just because no caller
    on this path happened to pass one in -- previously only
    ``test_connection``/``sync_permissions``/the importers ever did.
    """
    if override_ssh_tunnel is None:
        from superset.utils.async_bridge import run_async

        try:
            override_ssh_tunnel = run_async(_resolve_ssh_tunnel_standalone(database))
        except RuntimeError:
            # Called directly on an event-loop thread (no off-loop
            # dispatch via asyncio.to_thread) -- bridging here would risk
            # a deadlock. Fall back to "no tunnel" rather than crash the
            # caller; matches pre-fix behaviour for this narrow case.
            override_ssh_tunnel = None

    if override_ssh_tunnel is not None:
        from superset.extensions import ssh_manager_factory

        ssh_manager: Any = ssh_manager_factory.instance
        tunnel_cm: Any = ssh_manager.create_tunnel(
            ssh_tunnel=override_ssh_tunnel,
            sqlalchemy_database_uri=sqlalchemy_uri,
        )
        return ssh_manager, tunnel_cm
    return None, nullcontext()


def _build_engine_kwargs_sync(
    database: Any,
    sync_uri: str,
    catalog: str | None,
    schema: str | None,
    source: Any | None,
    nullpool: bool,
) -> tuple[str, dict[str, Any], str | None]:
    """Build ``engine_kwargs`` for a sync engine.

    Returns ``(sync_uri, engine_kwargs, effective_username_for_mutator)``.

    Applies (in order):
    - URI validation via ``db_engine_spec.validate_database_uri``
    - ``engine_params`` from ``database.get_extra``
    - ``adjust_engine_params`` (catalog/schema scoping)
    - impersonation via ``db_engine_spec.impersonate_user``
    - NullPool when ``nullpool=True``

    The third return value is the effective username captured BEFORE
    impersonation rewrites the URL — computed once from the post-adjust,
    pre-impersonation URL and passed to both ``impersonate_user`` and
    ``DB_CONNECTION_MUTATOR``.
    """
    from sqlalchemy.engine import make_url as _make_url

    # Validate the URI.
    db_spec = getattr(database, "db_engine_spec", None)
    if db_spec is not None and hasattr(db_spec, "validate_database_uri"):
        db_spec.validate_database_uri(_make_url(sync_uri))

    # engine_params from extra.
    extra = database.get_extra(source) if hasattr(database, "get_extra") else {}
    engine_kwargs = dict((extra or {}).get("engine_params") or {})
    connect_args: dict[str, Any] = engine_kwargs.setdefault("connect_args", {})

    # adjust_engine_params.
    # No try-except: any exception propagates to the caller as a clear connection error.
    from superset.db_engine_specs import get_engine_spec

    sync_spec = getattr(database, "db_engine_spec", None)
    if sync_spec is None:
        backend = (sync_uri.split("://", 1)[0] or "").split("+")[0]
        sync_spec = get_engine_spec(backend, "")
    adjusted_url, connect_args = sync_spec.adjust_engine_params(
        _make_url(sync_uri),
        connect_args,
        catalog=catalog,
        schema=schema,
    )
    engine_kwargs["connect_args"] = connect_args
    sync_uri = adjusted_url.render_as_string(hide_password=False)

    # Capture effective_username from the post-adjust, pre-impersonation URL.
    # The same value is passed to both impersonate_user and DB_CONNECTION_MUTATOR
    # (via the 3rd return value which _apply_connection_hooks receives
    # as effective_username).
    effective_for_mutator: str | None = (
        _impersonation_username(database.get_effective_user(adjusted_url))
        if hasattr(database, "get_effective_user")
        else None
    )

    # Impersonation.
    # No try-except: if impersonate_user raises the exception must propagate
    # so the connection is aborted cleanly.
    if getattr(database, "impersonate_user", False):
        spec = getattr(database, "db_engine_spec", None)
        if spec is not None and hasattr(spec, "impersonate_user"):
            url_obj = _make_url(sync_uri)
            access_token = _sync_oauth2_access_token(database, spec)
            url_obj, engine_kwargs = spec.impersonate_user(
                database,
                effective_for_mutator,
                access_token,
                url_obj,
                engine_kwargs,
            )
            sync_uri = url_obj.render_as_string(hide_password=False)

    # NullPool.
    if nullpool:
        from sqlalchemy.pool import NullPool

        engine_kwargs["poolclass"] = NullPool

    return sync_uri, engine_kwargs, effective_for_mutator


def _resolve_engine_context_manager(
    database: Any,
    catalog: str | None,
    schema: str | None,
) -> Any:
    """Resolve the ``ENGINE_CONTEXT_MANAGER`` operator hook and return the CM.

    When the operator configures ``ENGINE_CONTEXT_MANAGER`` (or the
    Pydantic-Settings equivalent ``engine_context_manager``), return the
    result of calling it; otherwise return ``nullcontext()``.
    """
    _engine_cm = None
    try:
        from superset import config as _config

        _engine_cm = getattr(_config, "ENGINE_CONTEXT_MANAGER", None)
        if _engine_cm is None:
            try:
                _engine_cm = getattr(
                    _config.SupersetSettings(),  # type: ignore[call-arg]
                    "engine_context_manager",
                    None,
                )
            except Exception:  # noqa: BLE001
                _engine_cm = None
    except Exception:  # noqa: BLE001
        _engine_cm = None

    if _engine_cm is not None:
        return _engine_cm(database, catalog, schema)
    return nullcontext()


def _create_sync_engine(
    database: Any, sync_uri: str, engine_kwargs: dict[str, Any]
) -> Engine:
    """Call ``create_engine`` and map DBAPI exceptions to Superset types."""
    try:
        return create_engine(sync_uri, **engine_kwargs)
    except Exception as ex:
        spec = getattr(database, "db_engine_spec", None)
        if spec is not None and hasattr(spec, "get_dbapi_mapped_exception"):
            raise spec.get_dbapi_mapped_exception(ex) from ex
        raise


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

    Used by helpers.ExploreMixin code paths that compile a SELECT statement
    and read ``engine.dialect`` for identifier quoting and ``%%``
    double-percent fixup detection.

    The engine is disposed when the context exits so we don't hold
    onto pooled connections after the helper finishes.
    """
    uri = getattr(database, "sqlalchemy_uri_decrypted", None) or getattr(
        database, "sqlalchemy_uri", ""
    )
    sqlalchemy_uri = str(uri)

    # SSH tunnel setup — see _setup_ssh_tunnel.
    ssh_manager, tunnel_cm = _setup_ssh_tunnel(
        database, sqlalchemy_uri, override_ssh_tunnel
    )

    with tunnel_cm as tunnel_server:
        if tunnel_server is not None and ssh_manager is not None:
            logger.info("[SSH] Using tunnel at %s", tunnel_server.local_bind_address)
            sqlalchemy_uri = str(
                ssh_manager.build_sqla_url(sqlalchemy_uri, tunnel_server)
            )

        sync_uri = _to_sync_uri(sqlalchemy_uri)

        # ENGINE_CONTEXT_MANAGER wraps ALL parameter preparation + create_engine
        # (adjust_engine_params, impersonation, update_params_from_encrypted_extra,
        # DB_CONNECTION_MUTATOR, and create_engine are all inside the CM).
        _outer_cm = _resolve_engine_context_manager(database, catalog, schema)

        engine: Engine | None = None
        try:
            with _outer_cm:
                # Build engine_kwargs (validate URI, extra params,
                # adjust_engine_params, impersonation, nullpool) — see
                # _build_engine_kwargs_sync.  The 3rd element is the
                # effective_username captured BEFORE impersonation so
                # DB_CONNECTION_MUTATOR receives the correct pre-impersonation value.
                sync_uri, engine_kwargs, eff_user = _build_engine_kwargs_sync(
                    database, sync_uri, catalog, schema, source, nullpool
                )

                # Apply the encrypted-extra merge + DB_CONNECTION_MUTATOR hooks
                # right before ``create_engine``.
                # The mutator receives a ``URL`` object; render the possibly-mutated
                # URL back to a password-revealing string for ``create_engine``
                # (``str(URL)`` masks the password under SA 2.0).
                # Pass the pre-impersonation effective_username so the mutator
                # receives the correct pre-impersonation value.
                from sqlalchemy.engine import make_url
                from sqlalchemy.engine.url import URL

                sqlalchemy_url, engine_kwargs = _apply_connection_hooks(
                    database,
                    make_url(sync_uri),
                    engine_kwargs,
                    source,
                    eff_user,
                )
                if isinstance(sqlalchemy_url, URL):
                    sync_uri = sqlalchemy_url.render_as_string(hide_password=False)
                else:
                    sync_uri = str(sqlalchemy_url)

                with _sync_check_for_oauth2(database):
                    engine = _create_sync_engine(database, sync_uri, engine_kwargs)
                    yield engine
        finally:
            if engine is not None:
                engine.dispose()


def _run_prequeries_sync(database: Any, conn: Connection) -> None:
    """Run ``db_engine_spec.get_prequeries`` on a freshly-opened connection.

    Mirrors ``Database.get_raw_connection``/``get_df`` (``superset/models/
    core.py``), which already run these for SQL Lab, so prequery-only
    session setup (e.g. Postgres' ``SET search_path``) also applies on the
    metadata/estimate probes that go through :func:`get_sync_connection`.
    """
    db_spec = getattr(database, "db_engine_spec", None)
    if db_spec is None or not hasattr(db_spec, "get_prequeries"):
        return
    from sqlalchemy import text as sa_text

    for prequery in db_spec.get_prequeries(database=database):
        conn.execute(sa_text(prequery))


async def _run_prequeries_async(database: Any, conn: AsyncConnection) -> None:
    """Async counterpart of :func:`_run_prequeries_sync`.

    Closes the gap for chart/dashboard queries, which reach the database
    exclusively through :func:`get_async_connection`: without this,
    ``impersonate_user`` on engines that implement impersonation *only* as
    a prequery (e.g. StarRocks' ``EXECUTE AS "<user>" WITH NO REVERT;``)
    silently runs every chart query as the connection's service account
    instead of the impersonated user.
    """
    db_spec = getattr(database, "db_engine_spec", None)
    if db_spec is None or not hasattr(db_spec, "get_prequeries"):
        return
    from sqlalchemy import text as sa_text

    for prequery in db_spec.get_prequeries(database=database):
        await conn.execute(sa_text(prequery))


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
            _run_prequeries_sync(database, conn)
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
    async_uri = _to_async_uri(cast("str", uri))

    # SSH tunnel -- no caller on this path ever supplies an override, so
    # resolve the database's own configured tunnel (see
    # ``_resolve_ssh_tunnel``); previously this path never tunnelled at
    # all. Tunnel setup/teardown is blocking (paramiko/sshtunnel), so it's
    # dispatched to a worker thread rather than stalling the event loop.
    ssh_tunnel = await _resolve_ssh_tunnel(database)
    ssh_manager: Any = None
    tunnel_cm: Any = nullcontext()
    if ssh_tunnel is not None:
        from superset.extensions import ssh_manager_factory

        ssh_manager = ssh_manager_factory.instance
        tunnel_cm = ssh_manager.create_tunnel(
            ssh_tunnel=ssh_tunnel,
            sqlalchemy_database_uri=async_uri,
        )

    tunnel_server = await asyncio.to_thread(tunnel_cm.__enter__)
    try:
        if tunnel_server is not None and ssh_manager is not None:
            logger.info("[SSH] Using tunnel at %s", tunnel_server.local_bind_address)
            async_uri = str(ssh_manager.build_sqla_url(async_uri, tunnel_server))

        # Validate the URI against the operator's DB_SQLA_URI_VALIDATOR /
        # per-spec disallow_uri_query_params -- matches the sync path
        # (_build_engine_kwargs_sync) so chart/dashboard queries are gated
        # by the same host allow-list / tenancy checks as SQL Lab, instead
        # of bypassing them entirely.
        db_spec_for_validation = getattr(database, "db_engine_spec", None)
        if db_spec_for_validation is not None and hasattr(
            db_spec_for_validation, "validate_database_uri"
        ):
            from sqlalchemy.engine import make_url as _make_url_validate

            db_spec_for_validation.validate_database_uri(_make_url_validate(async_uri))

        engine_spec = get_engine_spec_for_database(database)

        # Seed connect_args from database.extra["engine_params"] BEFORE
        # adjust_engine_params -- mirrors the sync path's precedence
        # (_build_engine_kwargs_sync: engine_kwargs starts from
        # extra.engine_params, then adjust_engine_params/impersonation layer
        # on top). Without this an operator-pinned connect_args (e.g.
        # sslmode=verify-full) was silently dropped for every chart query
        # while SQL Lab honoured it.
        extra = database.get_extra() if hasattr(database, "get_extra") else {}
        engine_params = dict((extra or {}).get("engine_params") or {})
        base_connect_args: dict[str, Any] = dict(
            engine_params.pop("connect_args", None) or {}
        )

        adjusted_uri, connect_args = engine_spec.adjust_engine_params(
            async_uri, base_connect_args
        )

        # Capture effective_username from the post-adjust, pre-impersonation URL
        # (computed before impersonate_user runs) so DB_CONNECTION_MUTATOR
        # receives the correct value.
        from sqlalchemy.engine import make_url as _make_url_pre_imp

        effective_for_mutator: str | None = (
            _impersonation_username(
                database.get_effective_user(_make_url_pre_imp(adjusted_uri))
            )
            if hasattr(database, "get_effective_user")
            else None
        )

        # Impersonation: rewrite the URL / connect_args to run as the effective
        # user. The impersonation hook lives on the *sync* engine spec (it's a
        # pure URL/connect_args transform, driver-agnostic), so use
        # ``database.db_engine_spec``. The OAuth2 access_token is intentionally
        # NOT resolved here: every OAuth2-impersonation engine (Trino/BigQuery/
        # Snowflake/Databricks/GSheets) is sync-only and connects via
        # ``get_sync_engine`` (where the token IS threaded). An async driver
        # implies a non-OAuth2 backend (postgres/mysql), so None is correct.
        if getattr(database, "impersonate_user", False):
            try:
                from sqlalchemy.engine import make_url

                sync_spec = getattr(database, "db_engine_spec", None)
                if sync_spec is not None and hasattr(sync_spec, "impersonate_user"):
                    url_obj = make_url(adjusted_uri)
                    url_obj, _ek = sync_spec.impersonate_user(
                        database,
                        effective_for_mutator,
                        None,
                        url_obj,
                        {"connect_args": connect_args},
                    )
                    adjusted_uri = url_obj.render_as_string(hide_password=False)
                    connect_args = _ek.get("connect_args", connect_args)
            except Exception as exc:  # noqa: BLE001
                logger.debug("impersonation skipped for async connection: %s", exc)

        # Apply the encrypted-extra merge + DB_CONNECTION_MUTATOR hooks right
        # before ``create_async_engine``. A mutator that rewrites the URL /
        # connect args must be respected on the async runtime path too,
        # otherwise it would be silently ignored for postgres/mysql. Pass the
        # pre-impersonation effective_for_mutator so DB_CONNECTION_MUTATOR
        # receives the correct pre-impersonation value.
        from sqlalchemy.engine import make_url
        from sqlalchemy.engine.url import URL

        async_engine_kwargs: dict[str, Any] = {
            **engine_params,
            "connect_args": connect_args,
        }
        mutated_url, async_engine_kwargs = _apply_connection_hooks(
            database,
            make_url(adjusted_uri),
            async_engine_kwargs,
            None,
            effective_for_mutator,
        )
        if isinstance(mutated_url, URL):
            adjusted_uri = mutated_url.render_as_string(hide_password=False)
        else:
            adjusted_uri = str(mutated_url)
        connect_args = async_engine_kwargs.pop("connect_args", connect_args)

        # pool_pre_ping/pool_size/max_overflow are hard-set for the async
        # runtime and always win over an operator's engine_params, to keep
        # pool behaviour predictable; every other engine_params key (plus
        # connect_args) passes through -- matches the sync path's precedence
        # (create_engine(sync_uri, **engine_kwargs)).
        for _pool_key in ("pool_pre_ping", "pool_size", "max_overflow"):
            async_engine_kwargs.pop(_pool_key, None)

        engine = create_async_engine(
            adjusted_uri,
            connect_args=connect_args,
            pool_pre_ping=True,
            pool_size=1,
            max_overflow=0,
            **async_engine_kwargs,
        )
        try:
            async with engine.connect() as conn:
                await _run_prequeries_async(database, conn)
                yield conn, engine_spec
        finally:
            await engine.dispose()
    finally:
        await asyncio.to_thread(tunnel_cm.__exit__, None, None, None)


def get_or_create_db(
    database_name: str, sqlalchemy_uri: str, always_create: bool | None = True
) -> Any:
    """Legacy helper ported for tests: gets or creates a Database."""
    import logging

    from superset.constants import EXAMPLES_DB_UUID
    from superset.db.session import get_sync_session
    from superset.models import core as models

    logger = logging.getLogger(__name__)
    session = get_sync_session()
    database = (
        session.query(models.Database).filter_by(database_name=database_name).first()
    )

    uuids = {
        "examples": EXAMPLES_DB_UUID,
    }

    if not database and always_create:
        logger.info("Creating database reference for %s", database_name)
        database = models.Database(
            database_name=database_name, uuid=uuids.get(database_name)
        )
        session.add(database)
        database.set_sqlalchemy_uri(sqlalchemy_uri)

    if database and database.sqlalchemy_uri_decrypted != sqlalchemy_uri:
        database.set_sqlalchemy_uri(sqlalchemy_uri)

    session.flush()
    return database


def get_example_database() -> Any:
    """Legacy helper ported for tests: returns the 'examples' DB."""
    from superset.config import SupersetSettings

    settings = SupersetSettings()  # type: ignore
    uri = settings.sqlalchemy_examples_uri or settings.sqlalchemy_database_uri
    return get_or_create_db("examples", uri)


def get_main_database() -> Any:
    """Legacy helper ported for tests: returns the 'main' DB."""
    from superset.config import SupersetSettings

    settings = SupersetSettings()  # type: ignore
    return get_or_create_db("main", settings.sqlalchemy_database_uri)


def remove_database(database: Any) -> None:
    """Legacy helper ported for tests: removes a database."""
    from superset.db.session import get_sync_session

    session = get_sync_session()
    session.delete(database)
    session.flush()
