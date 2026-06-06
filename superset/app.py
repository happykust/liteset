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

import json  # noqa: TID251 — superset must not depend on superset for core imports
import logging
import os
import sys
from pathlib import Path
from typing import Any

from litestar import get, Litestar
from litestar.config.compression import CompressionConfig
from litestar.config.cors import CORSConfig
from litestar.contrib.jinja import JinjaTemplateEngine
from litestar.datastructures import State
from litestar.di import Provide
from litestar.exceptions import ValidationException as _ValidationException
from litestar.openapi import OpenAPIConfig
from litestar.openapi.plugins import SwaggerRenderPlugin
from litestar.response import Response
from litestar.static_files import create_static_files_router
from litestar.template.config import TemplateConfig
from sqlalchemy.engine import make_url
from sqlalchemy.exc import (
    DataError as _DataError,
    DBAPIError as _DBAPIError,
    IntegrityError as _IntegrityError,
    StatementError as _StatementError,
)

from superset.config import SupersetSettings
from superset.constants import CHANGE_ME_SECRET_KEY
from superset.controllers.auth import AuthController
from superset.controllers.security import SecurityController
from superset.controllers.spa import SPAController
from superset.db.session import (
    create_db_engine,
    create_session_factory,
    dispose_engine,
)
from superset.dependencies import (
    get_current_user,
    provide_async_session,
    provide_request_cache,
    provide_security_manager,
)
from superset.exceptions import (
    data_error_handler,
    generic_exception_handler,
    integrity_error_handler,
    statement_error_handler,
    superset_exception_handler,
    SupersetException,
    validation_error_handler,
)
from superset.logging import configure_logging
from superset.middleware.async_token import AsyncTokenMiddleware
from superset.middleware.auth import SupersetAuthMiddleware
from superset.middleware.http_headers import HTTPHeadersMiddleware
from superset.middleware.locale import LocaleMiddleware
from superset.middleware.proxy_fix import ProxyFixMiddleware
from superset.middleware.request_context import RequestContextMiddleware
from superset.middleware.security_headers import SecurityHeadersMiddleware


def _build_exception_handlers() -> dict[Any, Any]:
    """Exception → handler map for the Litestar app.

    * ``ValidationException`` (msgspec / Litestar request-body validation)
      is mapped to ``422`` — matches original FAB/Marshmallow behaviour
      that contract tests expect.
    * SQLAlchemy ``IntegrityError`` is mapped to ``422`` so unique- and
      foreign-key violations don't surface as 500s.
    * SQLAlchemy ``DataError`` (string-too-long, NOT-NULL, type mismatch)
      is mapped to ``400`` — overrunning a VARCHAR cap or sending the wrong
      type is a payload bug, not a server bug. Upstream catches it
      up-front via marshmallow ``Length(...)`` validators; absent those
      caps in the msgspec schemas the asyncpg error reached 500.
    """
    # Dataset validation accumulates per-field errors and emits a
    # ``{"message": {field: [messages]}}`` 422 body 1:1 with upstream FAB
    # ``response_422(message=ex.normalized_messages())``. Registered more
    # specifically than ``SupersetException`` so it wins via MRO resolution;
    # scoped strictly to datasets (every other command keeps flat strings).
    from superset.commands.dataset.exceptions import (
        dataset_invalid_error_handler,
        DatasetInvalidError,
    )

    return {
        DatasetInvalidError: dataset_invalid_error_handler,
        SupersetException: superset_exception_handler,
        _ValidationException: validation_error_handler,
        _IntegrityError: integrity_error_handler,
        _DataError: data_error_handler,
        # asyncpg wraps PG-side ``StringDataRightTruncationError`` (sqlstate
        # 22001) as a raw ``DBAPIError`` rather than the more specific
        # ``DataError`` — so registering only ``DataError`` misses it. Use the
        # broader DBAPIError handler; it inspects the underlying ``orig``
        # exception's sqlstate (22xxx class = data exception → 400) and falls
        # through to the generic 500 for anything else.
        _DBAPIError: data_error_handler,
        # SA StatementError (parent of DBAPIError) catches bind-param
        # conversion failures BEFORE the DB sees the value — e.g. a
        # malformed UUID string fed to a UUID column crashes in
        # ``process_bind_param`` (uuid.UUID(value) → ValueError) and is
        # re-raised as ``StatementError(builtins.ValueError)``. Treat it
        # as a 400 client-side payload bug.
        _StatementError: statement_error_handler,
        Exception: generic_exception_handler,
    }


logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_manifest(manifest_path: Path) -> dict[str, Any]:
    if manifest_path.exists():
        return json.loads(manifest_path.read_text())
    return {}


def _make_manifest_lookup(manifest: dict[str, Any], asset_type: str) -> Any:
    entrypoints = manifest.get("entrypoints", {})

    def lookup(ctx: dict[str, Any], bundle_name: str) -> list[str]:
        entry = entrypoints.get(bundle_name, {})
        if isinstance(entry, dict):
            return entry.get(asset_type, [])
        return []

    return lookup


@get("/api/v1/health", opt={"exclude_from_auth": True})
async def health_check() -> Response[str]:
    return Response(content="OK", media_type="text/plain")


@get("/health", opt={"exclude_from_auth": True})
async def health() -> Response[str]:
    return Response(content="OK", media_type="text/plain")


@get("/healthcheck", opt={"exclude_from_auth": True})
async def healthcheck() -> Response[str]:
    return Response(content="OK", media_type="text/plain")


@get("/ping", opt={"exclude_from_auth": True})
async def ping() -> Response[str]:
    return Response(content="OK", media_type="text/plain")


@get("/healthz", opt={"exclude_from_auth": True})
async def readiness_probe(state: State) -> Response[dict[str, Any]]:
    """Readiness probe — checks DB and Redis connectivity."""
    checks: dict[str, str] = {}

    # Check database connectivity
    try:
        engine = getattr(state, "engine", None)
        if engine is not None:
            from sqlalchemy import text

            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            checks["database"] = "ok"
        else:
            checks["database"] = "not configured"
    except Exception as exc:
        checks["database"] = f"error: {exc}"

    # Check Redis connectivity
    try:
        redis = getattr(state, "redis", None)
        if redis is not None:
            await redis.ping()
            checks["redis"] = "ok"
        else:
            checks["redis"] = "not configured"
    except Exception as exc:
        checks["redis"] = f"error: {exc}"

    healthy = all(v == "ok" or v == "not configured" for v in checks.values())
    body = {"status": "OK" if healthy else "ERROR", "checks": checks}
    return Response(body, status_code=200 if healthy else 503)


async def on_startup(app: Litestar) -> None:  # noqa: C901
    settings: SupersetSettings = app.state.settings

    # ── check_secret_key ──────────────────────────────────────────────────
    # 1:1 with SupersetAppInitializer.check_secret_key: refuse to start (or
    # warn in debug/test mode) when SECRET_KEY is the default placeholder.
    _check_secret_key(settings)

    # ── i18n: load translation catalogs ───────────────────────────────────
    # Load all available language packs from ``superset/translations/`` and
    # register them with the module-level ``i18n`` catalog so that
    # ``gettext`` / ``lazy_gettext`` calls work from the first request.
    try:
        from superset.i18n import init_translations as _init_translations

        _translations_root = Path(__file__).parent / "translations"
        _catalogs: dict[str, dict[str, str]] = {}
        if _translations_root.is_dir():
            for _lang_dir in _translations_root.iterdir():
                _msg_file = _lang_dir / "LC_MESSAGES" / "messages.json"
                if _msg_file.is_file():
                    try:
                        _raw = json.loads(_msg_file.read_text(encoding="utf-8"))
                        # messages.json may be in one of three shapes:
                        #   1. jed1.x: {"domain": "superset", "locale_data":
                        #        {"superset": {msgid: [msgstr, ...], "": {...}}}}
                        #   2. raw po2json: {msgid: [null, msgstr, ...]}
                        #   3. flat: {msgid: msgstr}
                        _catalog: dict[str, str] = {}
                        if (
                            isinstance(_raw, dict)
                            and "locale_data" in _raw
                            and isinstance(_raw["locale_data"], dict)
                        ):
                            # jed1.x: unwrap the inner domain dict
                            _domain_data: dict[str, Any] = {}
                            for _domain_msgs in _raw["locale_data"].values():
                                if isinstance(_domain_msgs, dict):
                                    _domain_data.update(_domain_msgs)
                            for _k, _v in _domain_data.items():
                                if _k == "":
                                    continue  # skip metadata entry
                                if isinstance(_v, list) and len(_v) >= 1:
                                    _catalog[_k] = _v[0] if _v[0] else _k
                                elif isinstance(_v, str):
                                    _catalog[_k] = _v
                        else:
                            # raw or flat format
                            for _k, _v in _raw.items():
                                if _k == "":
                                    continue  # skip metadata entry
                                if isinstance(_v, list) and len(_v) >= 2:
                                    _catalog[_k] = _v[1] if _v[1] else _k
                                elif isinstance(_v, str):
                                    _catalog[_k] = _v
                        _catalogs[_lang_dir.name] = _catalog
                    except Exception:  # noqa: BLE001
                        pass
        _init_translations(_catalogs)
        logger.info(
            "i18n: loaded %d language catalogs: %s",
            len(_catalogs),
            sorted(_catalogs),
        )
    except Exception:  # noqa: BLE001
        logger.debug("i18n init_translations failed", exc_info=True)

    # ── Step 4: configure_logging ──────────────────────────────────────────
    # If the user supplied a LOGGING_CONFIGURATOR callable, call it first so
    # it can install custom handlers before structlog takes over.  Matches
    # the original ``SupersetAppInitializer.configure_logging`` which delegates
    # to ``self.config["LOGGING_CONFIGURATOR"].configure_logging(...)``.
    logging_configurator = getattr(settings, "logging_configurator", None)
    if logging_configurator is not None and callable(
        getattr(logging_configurator, "configure_logging", None)
    ):
        try:
            logging_configurator.configure_logging(
                {
                    "LOG_LEVEL": getattr(settings, "log_level", "INFO"),
                    "LOG_FORMAT": getattr(settings, "log_format", ""),
                    "ENABLE_TIME_ROTATE": getattr(settings, "enable_time_rotate", False),
                    "TIME_ROTATE_LOG_LEVEL": getattr(settings, "time_rotate_log_level", 20),
                    "FILENAME": getattr(settings, "log_filename", ""),
                    "ROLLOVER": getattr(settings, "rollover", "midnight"),
                    "INTERVAL": getattr(settings, "log_interval", 1),
                    "BACKUP_COUNT": getattr(settings, "backup_count", 30),
                },
                getattr(settings, "debug", False),
            )
        except Exception:  # noqa: BLE001
            pass  # fall through to structlog

    configure_logging(settings)

    # ── Step 5: configure_feature_flags ────────────────────────────────────
    from superset.utils.feature_flags import feature_flag_manager

    feature_flag_manager.init_from_config(
        settings.feature_flags,
        get_feature_flags_func=getattr(settings, "get_feature_flags_func", None),
        is_feature_enabled_func=getattr(settings, "is_feature_enabled_func", None),
    )

    # ── Step 11: setup_event_logger (part A) ──────────────────────────────
    # Resolve the EVENT_LOGGER config value now so it's ready for part B
    # (after the session factory has been created).  The module-level
    # singleton in superset.events is updated in part B.
    event_logger_cfg = getattr(settings, "event_logger", None)

    # ── Step 22: configure_auth_provider ───────────────────────────────────
    # Initialise the machine-auth provider factory (used by the Selenium /
    # Playwright webdriver helpers and by the Celery report task to mint
    # session cookies for headless browsers).  Must happen before any
    # controller or background task touches ``machine_auth_provider_factory``.
    from superset.extensions import machine_auth_provider_factory

    machine_auth_provider_factory.init_app(app)

    # ── Step 7: setup_db ───────────────────────────────────────────────────
    # Pass user-supplied SQLALCHEMY_ENGINE_OPTIONS to the engine factory so
    # connection-pool tuning, pre-ping, isolation level etc. are honoured.
    engine_options: dict[str, Any] = (
        getattr(settings, "sqlalchemy_engine_options", {}) or {}
    )
    engine = create_db_engine(settings.sqlalchemy_database_uri, **engine_options)
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)
    # NOTE: isolation_level="READ COMMITTED" is applied at engine-creation
    # time inside create_db_engine (superset/db/session.py) for PG/MySQL when
    # not already set by SQLALCHEMY_ENGINE_OPTIONS.  No per-connection
    # override is needed here.

    # ── Step 11: setup_event_logger (part B) ──────────────────────────────
    # Now that the session factory exists, wire the module-level
    # ``superset.events.event_logger`` singleton.  This mirrors the original
    # ``SupersetAppInitializer.setup_event_logger`` which stored the resolved
    # logger in ``_event_logger["event_logger"]`` so every controller that
    # did ``from superset.utils.log import event_logger`` got the DB-backed
    # impl, not the debug fallback.
    #
    # If EVENT_LOGGER is set to a custom instance in superset_config.py we
    # use that; otherwise we default to AsyncDBEventLogger(session_factory)
    # which persists audit rows to the metadata DB.
    try:
        from superset.events import configure_event_logger, get_event_logger_from_cfg_value
        import superset.events as _events_mod

        if event_logger_cfg is not None:
            # User supplied a custom EventLogger instance or class.
            _resolved = get_event_logger_from_cfg_value(event_logger_cfg)
            _events_mod.event_logger = _resolved
            app.state.event_logger = _resolved
            logger.info(
                "Event logger configured from EVENT_LOGGER setting: %s",
                type(_resolved).__name__,
            )
        else:
            # Default: AsyncDBEventLogger backed by the metadata DB.
            configure_event_logger(session_factory=app.state.session_factory)
            app.state.event_logger = _events_mod.event_logger
    except Exception:  # noqa: BLE001
        logger.warning("Failed to configure event logger", exc_info=True)
        app.state.event_logger = None

    # Initialize Redis for auth user cache
    if settings.redis_url:
        try:
            from redis.asyncio import Redis

            app.state.redis = Redis.from_url(settings.redis_url, decode_responses=True)
            logger.info("Redis connected for auth cache")
        except Exception:
            logger.warning("Failed to connect to Redis — auth cache disabled")
            app.state.redis = None
    else:
        app.state.redis = None

    # ── Step 16: configure_cache ───────────────────────────────────────────
    # Initialise the multi-cache holder used by ``utils.cache.memoized_func``,
    # ``utils.cache.set_and_log_cache``, ``viz.py``, ``screenshots.py``, and
    # the ``commands.chart_data`` / ``commands.sqllab`` async pipelines.
    from superset.extensions import (
        cache_manager,
        ssh_manager_factory,
        stats_logger_manager,
    )

    cache_manager.init_app(
        redis=app.state.redis,
        cache_default_timeout=settings.cache_default_timeout,
        cache_config=settings.cache_config,
        data_cache_config=settings.data_cache_config,
        thumbnail_cache_config=settings.thumbnail_cache_config,
        filter_state_cache_config=settings.filter_state_cache_config,
        explore_form_data_cache_config=settings.explore_form_data_cache_config,
        # Pass the raw Redis URL so the cache manager can build its
        # *own* sync Redis client for Celery / Selenium / Playwright
        # call sites.  Sharing the async client across event loops
        # would deadlock on cross-loop awaits — distinct sync/async
        # clients pointing at the same Redis cluster keep keyspace
        # behaviour identical to the original Flask Superset.
        redis_url=settings.redis_url or None,
        # Required for ``CACHE_TYPE='SupersetMetastoreCache'`` (the
        # default for FILTER_STATE / EXPLORE_FORM_DATA in upstream
        # config.py).  The metastore cache opens a dedicated session per
        # operation against the metadata DB ``key_value`` table.
        session_factory=app.state.session_factory,
    )
    # ── Step 25: configure_stats_manager ───────────────────────────────────
    stats_logger_manager.configure(settings.stats_logger)

    # ── Step 9: configure_celery ───────────────────────────────────────────
    # Pass CELERY_CONFIG to the Celery app so user-provided beat schedules,
    # broker URLs etc. take effect.  The original ``configure_celery`` also
    # assigned ``AppContextTask`` as the base class — not needed in ASGI.
    celery_config = getattr(settings, "celery_config", None)
    if celery_config is not None:
        try:
            from superset.tasks.celery_app import app as celery_app

            celery_app.config_from_object(celery_config)
            logger.debug("Celery configured from CELERY_CONFIG setting")
        except Exception:  # noqa: BLE001
            logger.warning("Failed to apply CELERY_CONFIG to Celery app", exc_info=True)

    # ── Step 24: configure_ssh_manager ─────────────────────────────────────
    try:
        ssh_manager_factory.init_app(settings)
    except Exception:  # noqa: BLE001
        logger.warning("SSH manager init failed", exc_info=True)

    # ── Step 18: configure_sqlglot_dialects ────────────────────────────────
    # Merge user-supplied SQLGLOT_DIALECTS_EXTENSIONS into the global dict.
    # Mirrors ``SupersetAppInitializer.configure_sqlglot_dialects`` 1:1.
    sqlglot_extensions = getattr(settings, "sqlglot_dialects_extensions", None) or {}
    if sqlglot_extensions:
        try:
            if callable(sqlglot_extensions):
                sqlglot_extensions = sqlglot_extensions()
            from superset.sql.parse import SQLGLOT_DIALECTS

            SQLGLOT_DIALECTS.update(sqlglot_extensions)
            logger.debug(
                "Registered %d sqlglot dialect extension(s)", len(sqlglot_extensions)
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "Failed to register SQLGLOT_DIALECTS_EXTENSIONS", exc_info=True
            )

    # ── Step 21: configure_data_sources ────────────────────────────────────
    # Import datasource modules to trigger their side-effect registration.
    # Mirrors ``SupersetAppInitializer.configure_data_sources`` 1:1.
    module_datasource_map: dict[str, list[str]] = {
        **getattr(settings, "default_module_ds_map", {}),
        **getattr(settings, "additional_module_ds_map", {}),
    }
    for module_name, class_names in module_datasource_map.items():
        try:
            __import__(module_name, fromlist=[str(s) for s in class_names])
        except ImportError:
            logger.warning("Could not import datasource module %s", module_name)

    # ── sync_config_to_db ──────────────────────────────────────────────────
    # Seed system themes and register SQLA tagging event listeners.
    # Async equivalent of ``SupersetApp.sync_config_to_db()``.
    try:
        if feature_flag_manager.is_feature_enabled("TAGGING_SYSTEM"):
            try:
                from superset.tags.core import register_sqla_event_listeners

                register_sqla_event_listeners()
            except Exception:  # noqa: BLE001
                logger.debug(
                    "Could not register tagging event listeners", exc_info=True
                )

        # Seed system themes asynchronously when theme config is present.
        theme_default = getattr(settings, "theme_default", None)
        theme_dark = getattr(settings, "theme_dark", None)
        theme_seeds: list[tuple[str, Any]] = []
        if theme_default:
            theme_seeds.append(("THEME_DEFAULT", theme_default))
        if theme_dark:
            theme_seeds.append(("THEME_DARK", theme_dark))

        if theme_seeds:
            try:
                import json as _json

                from sqlalchemy import select

                from superset.models.core import Theme

                async with app.state.session_factory() as _session:
                    for theme_name, theme_config in theme_seeds:
                        if callable(theme_config):
                            theme_config = theme_config()
                        if not isinstance(theme_config, dict):
                            continue
                        stmt = select(Theme).where(
                            Theme.theme_name == theme_name,
                            Theme.is_system.is_(True),
                        )
                        existing = (await _session.execute(stmt)).scalars().first()
                        json_data = _json.dumps(theme_config)
                        if existing:
                            existing.json_data = json_data
                        else:
                            _session.add(
                                Theme(
                                    theme_name=theme_name,
                                    json_data=json_data,
                                    is_system=True,
                                )
                            )
                    await _session.commit()
            except Exception:  # noqa: BLE001
                logger.debug(
                    "Theme seeding skipped (DB may not be migrated yet)",
                    exc_info=True,
                )
    except Exception:  # noqa: BLE001
        logger.debug("sync_config_to_db failed (non-fatal)", exc_info=True)

    # ── FLASK_APP_MUTATOR ──────────────────────────────────────────────────
    # Call the user-supplied last-mile hook with the Litestar app object.
    # Mirrors ``SupersetAppInitializer.init_app_in_ctx`` which calls
    # ``flask_app_mutator(self.superset_app)``.
    flask_app_mutator = getattr(settings, "flask_app_mutator", None)
    if callable(flask_app_mutator):
        try:
            flask_app_mutator(app)
        except Exception:  # noqa: BLE001
            logger.warning("FLASK_APP_MUTATOR raised an exception", exc_info=True)

    # Initialize active WebSocket connections tracker
    app.state.active_websockets = {}

    # Start periodic channel cleanup if Redis is available
    if app.state.redis is not None:
        import asyncio

        from superset.async_events.manager import AsyncEventManager

        async def periodic_cleanup(manager: AsyncEventManager) -> None:
            """Background loop that cleans up stale channel streams."""
            while True:
                await asyncio.sleep(120)
                try:
                    await manager.cleanup_stale_channels(max_idle_seconds=120)
                    await manager.cleanup_global_stream()
                except Exception:
                    logger.exception("Error during channel cleanup")

        _aem_prefix = getattr(
            settings,
            "global_async_queries_redis_stream_prefix",
            "async-events-",
        )
        manager = AsyncEventManager(
            redis=app.state.redis,
            stream_prefix=_aem_prefix,
            # Derive the firehose key from the prefix (1:1 upstream
            # ``f"{self._stream_prefix}full"``) so a custom prefix is honored
            # end-to-end rather than pinned to ``async-events-full``.
            global_stream_key=f"{_aem_prefix}full",
            global_stream_limit=getattr(
                settings,
                "global_async_queries_redis_stream_limit_firehose",
                1_000_000,
            ),
            channel_stream_limit=getattr(
                settings,
                "global_async_queries_redis_stream_limit",
                1_000,
            ),
        )
        app.state.cleanup_task = asyncio.create_task(periodic_cleanup(manager))

    logger.info(
        "Superset started with DB: %s",
        make_url(settings.sqlalchemy_database_uri).render_as_string(hide_password=True),
    )


async def on_shutdown(app: Litestar) -> None:
    import asyncio

    # Cancel cleanup task
    cleanup_task = getattr(app.state, "cleanup_task", None)
    if cleanup_task and not cleanup_task.done():
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass

    # Close all active WebSocket connections
    active_ws = getattr(app.state, "active_websockets", {})
    for ws in list(active_ws.keys()):
        try:
            await ws.close(code=1001, reason="Server shutting down")
        except Exception:  # noqa: S110
            pass

    if hasattr(app.state, "engine"):
        await dispose_engine(app.state.engine)
        logger.info("Database engine disposed")
    if hasattr(app.state, "redis") and app.state.redis is not None:
        await app.state.redis.close()
        logger.info("Redis connection closed")


def _check_secret_key(settings: SupersetSettings) -> None:
    """Refuse to start (or warn in debug/test mode) when SECRET_KEY is the
    default placeholder.

    1:1 with ``SupersetAppInitializer.check_secret_key`` in
    ``superset_old/initialization/__init__.py``.
    """

    def _log_default_secret_key_warning() -> None:
        top_banner = 80 * "-" + "\n" + 36 * " " + "WARNING\n" + 80 * "-"
        bottom_banner = 80 * "-" + "\n" + 80 * "-"
        logger.warning(top_banner)
        logger.warning(
            "A Default SECRET_KEY was detected, please use superset_config.py "
            "to override it.\n"
            "Use a strong complex alphanumeric string and use a tool to help"
            " you generate \n"
            "a sufficiently random sequence, ex: openssl rand -base64 42 \n"
            "For more info, see: https://superset.apache.org/docs/"
            "configuration/configuring-superset#specifying-a-secret_key"
        )
        logger.warning(bottom_banner)

    _raw = settings.secret_key
    secret_key_val: str = (
        _raw.get_secret_value() if hasattr(_raw, "get_secret_value") else str(_raw)
    )
    if secret_key_val != CHANGE_ME_SECRET_KEY:
        return

    # is_test(): mirrors ``superset_old/utils/core.py::is_test()``
    _is_test = os.environ.get("SUPERSET_TESTENV", "false").lower() in ("true", "1", "yes")
    if settings.debug or _is_test:
        logger.warning("Debug mode identified with default secret key")
        _log_default_secret_key_warning()
        return

    _log_default_secret_key_warning()
    logger.error("Refusing to start due to insecure SECRET_KEY")
    sys.exit(1)


def _validate_global_async_queries_config(settings: SupersetSettings) -> None:
    """Validate Global Async Queries config at app build time.

    1:1 with the original ``AsyncQueryManager.init_app`` guard, which only ran
    when the ``GLOBAL_ASYNC_QUERIES`` feature flag was enabled
    (``configure_async_queries`` → ``init_app``) and refused to start the app
    if:
      1. CACHE_CONFIG or DATA_CACHE_CONFIG has a null/None cache type, or
      2. the JWT secret is shorter than 32 bytes.

    Both checks mirror ``superset_old/async_events/async_query_manager.py::init_app``
    exactly.
    """
    if not getattr(settings, "global_async_queries", False):
        return

    # ── Cache-backend-null check ──────────────────────────────────────────
    # Original: ``if cache_type in [None, "null"] or data_cache_type in
    # [None, "null"]: raise Exception(...)``
    cache_type = (getattr(settings, "cache_config", None) or {}).get("CACHE_TYPE")
    data_cache_type = (getattr(settings, "data_cache_config", None) or {}).get(
        "CACHE_TYPE"
    )
    if cache_type in [None, "null"] or data_cache_type in [None, "null"]:
        raise Exception(  # noqa: TRY002
            "\nCache backends (CACHE_CONFIG, DATA_CACHE_CONFIG) must be configured"
            "\nand non-null in order to enable async queries\n"
        )

    from superset.commands.chart.data.create_async_job_command import (
        AsyncQueryTokenException,
    )
    from superset.middleware.async_token import _resolve_secret_key

    secret = _resolve_secret_key(settings)
    if len(secret) < 32:
        raise AsyncQueryTokenException(
            "Please provide a JWT secret at least 32 bytes long"
        )


def _build_cors_config(settings: SupersetSettings) -> CORSConfig | None:
    """Map the upstream Flask-CORS ``CORS_OPTIONS`` dict onto Litestar's
    ``CORSConfig``.

    1:1 with ``superset_old/initialization/__init__.py::configure_middlewares``::

        if self.config["ENABLE_CORS"]:
            from flask_cors import CORS
            CORS(self.superset_app, **self.config["CORS_OPTIONS"])

    When ``ENABLE_CORS`` is false, CORS is OFF (returns ``None`` — no permissive
    default), matching Flask where ``flask_cors.CORS`` is never registered.

    Flask-CORS option -> Litestar ``CORSConfig`` field mapping:

    * ``origins``             -> ``allow_origins``
    * ``methods``             -> ``allow_methods``
    * ``allow_headers``       -> ``allow_headers``
    * ``expose_headers``      -> ``expose_headers``
    * ``supports_credentials``-> ``allow_credentials``
    * ``max_age``             -> ``max_age``

    Flask-CORS defaults are applied for keys the user omits so the wildcard
    behaviour matches upstream (Flask-CORS defaults: ``origins='*'``, all
    standard methods, ``allow_headers='*'``, ``supports_credentials=False``).

    Limitation: Flask-CORS ``resources`` (per-path scoping / regex resource
    maps) has no equivalent in Litestar's ``CORSConfig``, which is applied
    app-wide. The ``resources`` key is therefore ignored; the common upstream
    ``CORS_OPTIONS`` does not set it (it only scopes via ``origins``), so this
    matches the broad-application case. ``send_wildcard``/``vary_header`` and
    other Flask-CORS-only knobs are likewise not expressible and are ignored.
    """
    if not settings.enable_cors:
        return None

    opts = settings.cors_options or {}

    def _as_list(value: Any) -> list[str]:
        if isinstance(value, str):
            return [value]
        return list(value)

    # Flask-CORS default origins is "*"; Litestar default allow_origins is ["*"].
    allow_origins = _as_list(opts.get("origins", ["*"]))
    # Flask-CORS default methods cover all standard verbs; "*" expresses that.
    allow_methods = _as_list(opts["methods"]) if "methods" in opts else ["*"]
    # Flask-CORS default allow_headers is "*".
    allow_headers = (
        _as_list(opts["allow_headers"]) if "allow_headers" in opts else ["*"]
    )
    expose_headers = (
        _as_list(opts["expose_headers"]) if "expose_headers" in opts else []
    )

    return CORSConfig(
        allow_origins=allow_origins,
        allow_methods=allow_methods,  # type: ignore[arg-type]
        allow_headers=allow_headers,
        expose_headers=expose_headers,
        allow_credentials=bool(opts.get("supports_credentials", False)),
        max_age=int(opts["max_age"]) if opts.get("max_age") is not None else 600,
    )


def create_app(  # noqa: C901
    settings: SupersetSettings | None = None,
) -> Litestar:
    if settings is None:
        # secret_key is resolved at runtime from env vars or superset_config.py
        settings = SupersetSettings()  # type: ignore[call-arg]

    # When Global Async Queries is enabled, refuse to start with a weak JWT
    # secret (1:1 with the original AsyncQueryManager.init_app guard).
    _validate_global_async_queries_config(settings)

    # Import core API controllers (Phase 4)
    # Import remaining API controllers (Phase 5)
    from superset.controllers.advanced_data_type import AdvancedDataTypeController
    from superset.controllers.annotation import AnnotationController
    from superset.controllers.annotation_layer import AnnotationLayerController
    from superset.controllers.async_event import AsyncEventController
    from superset.controllers.available_domains import AvailableDomainsController
    from superset.controllers.cache import CacheController
    from superset.controllers.chart import ChartController
    from superset.controllers.css_template import CssTemplateController
    from superset.controllers.dashboard import DashboardController
    from superset.controllers.dashboard_filter_state import (
        DashboardFilterStateController,
    )
    from superset.controllers.database import DatabaseController
    from superset.controllers.dataset import DatasetController

    # Import datasource and role controllers (Phase 7: cleanup)
    from superset.controllers.datasource import DatasourceController
    from superset.controllers.embedded_dashboard import (
        EmbeddedDashboardController,
        EmbeddedSSRController,
    )
    from superset.controllers.explore import ExploreController
    from superset.controllers.explore_form_data import ExploreFormDataController
    from superset.controllers.explore_json import ExploreJsonController
    from superset.controllers.explore_permalink import ExplorePermalinkController
    from superset.controllers.group import GroupController
    from superset.controllers.import_export import ImportExportController
    from superset.controllers.legacy_api import LegacyApiController
    from superset.controllers.legacy_datasource import LegacyDatasourceController
    from superset.controllers.log import LogController
    from superset.controllers.menu import MenuController
    from superset.controllers.openapi import OpenApiController
    from superset.controllers.permission import PermissionController
    from superset.controllers.permission_view import PermissionViewController
    from superset.controllers.query import QueryController
    from superset.controllers.report import ReportScheduleController
    from superset.controllers.report_log import ReportExecutionLogController
    from superset.controllers.rls import RLSController
    from superset.controllers.role import RoleController
    from superset.controllers.saved_query import SavedQueryController
    from superset.controllers.sqllab import SqlLabController
    from superset.controllers.sqllab_permalink import SqlLabPermalinkController
    from superset.controllers.tab_state import (
        TableSchemaController,
        TabStateController,
    )
    from superset.controllers.tag import TagController
    from superset.controllers.theme import ThemeController
    from superset.controllers.user import (
        UserController,
        UserPublicController,
        UserRegistrationsController,
    )
    from superset.controllers.user_me import CurrentUserController
    from superset.controllers.view_menu import ViewMenuController

    # Import WebSocket handler (Phase 6)
    from superset.websocket.events import AsyncQueryWebSocket

    route_handlers: list[Any] = [
        # Legacy /superset/explore_json endpoints (deprecated, eol 5.0.0) —
        # serve deck.gl and other legacy viz types that POST/GET form_data
        # instead of /api/v1/chart/data, plus the GAQ async transport and the
        # cached-result fetch. Listed first so its explicit paths resolve
        # ahead of the SPA catch-all (/superset/{path:path}).
        ExploreJsonController,
        health_check,
        health,
        healthcheck,
        ping,
        readiness_probe,
        AuthController,
        SPAController,
        SecurityController,
        ChartController,
        DashboardController,
        DashboardFilterStateController,
        DatabaseController,
        DatasetController,
        QueryController,
        SavedQueryController,
        SqlLabController,
        SqlLabPermalinkController,
        TabStateController,
        TableSchemaController,
        AnnotationLayerController,
        AnnotationController,
        CssTemplateController,
        AvailableDomainsController,
        AdvancedDataTypeController,
        ExploreController,
        ExploreFormDataController,
        ExplorePermalinkController,
        ReportScheduleController,
        ReportExecutionLogController,
        LogController,
        CurrentUserController,
        UserController,
        UserPublicController,
        UserRegistrationsController,
        TagController,
        ThemeController,
        EmbeddedDashboardController,
        EmbeddedSSRController,
        CacheController,
        AsyncEventController,
        RLSController,
        ImportExportController,
        LegacyApiController,
        LegacyDatasourceController,
        DatasourceController,
        RoleController,
        GroupController,
        PermissionController,
        PermissionViewController,
        ViewMenuController,
        MenuController,
        OpenApiController,
        AsyncQueryWebSocket,
    ]
    startup_hooks: list[Any] = [on_startup]

    assets_dir = _PROJECT_ROOT / "superset" / "static" / "assets"
    appbuilder_dir = _PROJECT_ROOT / "superset" / "static" / "appbuilder"

    manifest_path = assets_dir / "manifest.json"
    manifest = _load_manifest(manifest_path)

    def _register_template_globals(engine: JinjaTemplateEngine) -> None:
        engine.register_template_callable(
            key="js_manifest",
            template_callable=_make_manifest_lookup(manifest, "js"),
        )
        engine.register_template_callable(
            key="css_manifest",
            template_callable=_make_manifest_lookup(manifest, "css"),
        )
        engine.register_template_callable(
            key="assets_prefix",
            template_callable=lambda ctx: settings.static_assets_prefix,
        )

    route_handlers.extend(
        [
            create_static_files_router(
                path="/static/assets",
                directories=[assets_dir],
                name="static_assets",
            ),
            create_static_files_router(
                path="/static/appbuilder",
                directories=[appbuilder_dir],
                name="static_appbuilder",
            ),
        ]
    )

    # Event manager DI provider (Phase 6: WebSocket)
    from superset.async_events import manager as _aem_mod

    async def provide_event_manager(state: State) -> Any:
        _settings = getattr(state, "settings", None)
        _aem_prefix = getattr(
            _settings,
            "global_async_queries_redis_stream_prefix",
            "async-events-",
        )
        return _aem_mod.AsyncEventManager(
            redis=state.redis,
            stream_prefix=_aem_prefix,
            # Firehose key derived from the prefix (see startup-manager note).
            global_stream_key=f"{_aem_prefix}full",
            global_stream_limit=getattr(
                _settings,
                "global_async_queries_redis_stream_limit_firehose",
                1_000_000,
            ),
            channel_stream_limit=getattr(
                _settings,
                "global_async_queries_redis_stream_limit",
                1_000,
            ),
        )

    # Build CSRF middleware (session-based, Flask-WTF compatible)
    csrf_middleware = None  # Don't use Litestar built-in CSRF config
    if settings.csrf_enabled and settings.wtf_csrf_enabled:
        from superset.middleware.csrf import (
            create_csrf_middleware,
        )

        _raw_secret = settings.secret_key
        secret_key: str = (
            _raw_secret.get_secret_value()
            if hasattr(_raw_secret, "get_secret_value")
            else str(_raw_secret)
        )
        # Start with the hard-coded baseline exempt paths (health probes + the
        # original 5 WTF_CSRF_EXEMPT_LIST entries from upstream config.py).
        # Merge the user-supplied WTF_CSRF_EXEMPT_LIST on top.  The original
        # Flask list used dotted view-function names; we translate them to URL
        # prefixes for ASGI compatibility.
        _baseline_exempt: list[str] = [
            # Health probes
            "/health",
            "/healthcheck",
            "/ping",
            "/healthz",
            # Auth forms (no token before login)
            "/login",
            "/logout",
            # Original WTF_CSRF_EXEMPT_LIST view-to-path translations:
            "/api/v1/chart/data",
            "/api/v1/dashboard/cache_dashboard_screenshot",
            "/superset/explore_json",
            "/superset/log",
            "/datasource/samples",
        ]
        # User-supplied WTF_CSRF_EXEMPT_LIST may be dotted view-function names
        # (old FAB style) or plain URL paths.  We include them as-is because the
        # CSRF middleware does prefix matching — a dotted name won't match any
        # URL, so non-path entries are harmlessly ignored.
        _user_exempt: list[str] = list(
            getattr(settings, "wtf_csrf_exempt_list", []) or []
        )
        _all_exempt = list(dict.fromkeys(_baseline_exempt + _user_exempt))

        csrf_middleware = create_csrf_middleware(
            secret=secret_key,
            header_name=getattr(
                settings,
                "csrf_header_name",
                "X-CSRFToken",
            ),
            max_age=getattr(settings, "wtf_csrf_time_limit", 604800),
            exclude_paths=_all_exempt,
            session_cookie_name=getattr(
                settings,
                "session_cookie_name",
                "session",
            ),
        )

    return Litestar(
        route_handlers=route_handlers,
        dependencies={
            "session": Provide(provide_async_session),
            "request_cache": Provide(
                provide_request_cache,
                use_cache=True,
            ),
            "current_user": Provide(get_current_user, sync_to_thread=False),
            "security_manager": Provide(provide_security_manager),
            "event_manager": Provide(provide_event_manager),
        },
        # NOTE: do NOT register a type_decoder for Protocol classes here.
        # Litestar's signature builder reacts to a matching type_decoder by
        # doing ``setattr(annotation, "_decoder", decoder)`` (see
        # ``litestar/_signature/model.py::_create_annotation`` — flagged
        # as a temporary hack against ``jcrist/msgspec#497``).  When the
        # annotation is a ``@runtime_checkable`` Protocol that ``setattr``
        # injects ``_decoder`` into the Protocol's structural attribute
        # set, which then makes every ``isinstance(obj, ThatProtocol)``
        # check fail (the value lacks ``_decoder``) — and msgspec's
        # validation falls over with::
        #
        #   ValidationError: Expected ``UserProtocol``, got ``CachedUser``
        #
        # Without our type_decoder, ``_get_decoder_for_type`` returns
        # ``None``, no ``_decoder`` is grafted onto the Protocol, and
        # msgspec uses ``runtime_checkable`` ``__instancecheck__`` as
        # intended — concrete dataclass / ORM instances pass through
        # cleanly because they expose the structural attributes.
        type_decoders=[],
        middleware=[
            *(
                [ProxyFixMiddleware(**settings.proxy_fix_config)]
                if settings.enable_proxy_fix
                else []
            ),
            SecurityHeadersMiddleware(),
            # RateLimitMiddleware(),  # disabled — too aggressive for dev/testing
            LocaleMiddleware(),
            # HTTPHeadersMiddleware applies OVERRIDE_HTTP_HEADERS / HTTP_HEADERS /
            # DEFAULT_HTTP_HEADERS from settings onto every response.  Equivalent
            # to the original Flask ``register_request_handlers`` after-request hook.
            HTTPHeadersMiddleware(),
            # RequestContextMiddleware must run BEFORE the auth middleware
            # so audit-logging code paths inside auth/guards (which fire
            # synchronously from controllers) can resolve the request /
            # form_data ContextVars.  It also must run before CSRF so the
            # CSRF check sees a body the middleware has already cached.
            RequestContextMiddleware(),
            SupersetAuthMiddleware,
            # AsyncTokenMiddleware mints / refreshes the ``async-token`` JWT
            # cookie on authenticated responses — 1:1 with the original Flask
            # ``register_request_handlers`` after-request hook.  Gated on the
            # same two flags the original used (``configure_async_queries`` ran
            # only under GLOBAL_ASYNC_QUERIES, and ``init_app`` registered the
            # handler only when REGISTER_REQUEST_HANDLERS was set).  Placed after
            # the auth middleware so ``scope["user"]`` is populated when the
            # cookie is built on ``http.response.start``.
            *(
                [AsyncTokenMiddleware()]
                if (
                    settings.global_async_queries
                    and settings.global_async_queries_register_request_handlers
                )
                else []
            ),
            *([csrf_middleware] if csrf_middleware else []),
        ],
        csrf_config=None,  # Using custom middleware
        on_startup=startup_hooks,
        on_shutdown=[on_shutdown],
        exception_handlers=_build_exception_handlers(),
        openapi_config=OpenAPIConfig(
            title=settings.app_name,
            version=settings.version_string or "v0.0.0-dev",
            path="/swagger/v1",
            render_plugins=[SwaggerRenderPlugin()],
        ),
        cors_config=_build_cors_config(settings),
        compression_config=CompressionConfig(backend="gzip"),
        template_config=TemplateConfig(
            directory=Path(__file__).parent / "templates",
            engine=JinjaTemplateEngine,
            engine_callback=_register_template_globals,
        ),
        state=State({"settings": settings}),
    )
