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
import re
import sys
from pathlib import Path
from typing import Any, cast, TYPE_CHECKING

if TYPE_CHECKING:
    from redis.asyncio import Redis

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
from superset.middleware.app_root import AppRootMiddleware
from superset.middleware.async_token import AsyncTokenMiddleware
from superset.middleware.auth import SupersetAuthMiddleware
from superset.middleware.http_headers import HTTPHeadersMiddleware
from superset.middleware.locale import LocaleMiddleware
from superset.middleware.proxy_fix import ProxyFixMiddleware
from superset.middleware.rate_limit import RateLimitMiddleware
from superset.middleware.request_context import RequestContextMiddleware
from superset.middleware.security_headers import SecurityHeadersMiddleware


def _build_exception_handlers() -> dict[Any, Any]:
    """Exception → handler map for the Litestar app.

    * ``ValidationException`` (msgspec / Litestar request-body validation)
      is mapped to ``422`` — matches the original Marshmallow behaviour
      that contract tests expect.
    * SQLAlchemy ``IntegrityError`` is mapped to ``422`` so unique- and
      foreign-key violations don't surface as 500s.
    * SQLAlchemy ``DataError`` (string-too-long, NOT-NULL, type mismatch)
      is mapped to ``400`` — overrunning a VARCHAR cap or sending the wrong
      type is a payload bug, not a server bug. Upstream catches it
      up-front via marshmallow ``Length(...)`` validators; absent those
      caps in the msgspec schemas the asyncpg error reached 500.
    """
    # Accumulating *InvalidError* validation errors emit a
    # ``{"message": {field: [messages]}}`` 422 body.
    # Registered more specifically than ``SupersetException`` so they win via
    # MRO resolution; scoped to the resources whose commands raise field-keyed
    # errors (dataset, dashboard, database, annotation layer) — every other
    # command keeps flat strings.
    from superset.commands.annotation_layer.exceptions import (
        AnnotationLayerInvalidError,
    )
    from superset.commands.dashboard.exceptions import DashboardInvalidError
    from superset.commands.database.exceptions import DatabaseInvalidError
    from superset.commands.dataset.exceptions import (
        dataset_invalid_error_handler,
        DatasetInvalidError,
    )
    from superset.commands.report_exceptions import ReportScheduleInvalidError

    return {
        DatasetInvalidError: dataset_invalid_error_handler,
        DashboardInvalidError: dataset_invalid_error_handler,
        DatabaseInvalidError: dataset_invalid_error_handler,
        AnnotationLayerInvalidError: dataset_invalid_error_handler,
        # ReportScheduleInvalidError overrides normalized_messages() to emit
        # the {field: [messages]} mapping (e.g. {"owners": [...]}).
        ReportScheduleInvalidError: dataset_invalid_error_handler,
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


async def _seed_one_theme(
    session_factory: Any,
    theme_name: str,
    theme_config: dict[str, Any],
) -> None:
    """Upsert one system theme in its own independent transaction.

    Each call opens, commits (or rolls back), and closes its own DB session so
    that a failure on one theme never reverts a previously committed sibling.
    """
    import json as _json

    from sqlalchemy import select

    from superset.models.core import Theme

    async with session_factory() as _session:
        # Handle UUID-only references by copying the referenced theme's definition.
        if "uuid" in theme_config and len(theme_config) == 1:
            original_uuid = theme_config["uuid"]
            ref_stmt = select(Theme).where(Theme.uuid == original_uuid)
            ref_result = await _session.execute(ref_stmt)
            referenced_theme = ref_result.scalars().first()
            if referenced_theme and referenced_theme.json_data:
                try:
                    theme_config = _json.loads(referenced_theme.json_data)
                    theme_config["NOTE"] = (
                        f"Copied at startup from theme UUID "
                        f"{original_uuid} based on config reference"
                    )
                    logger.debug(
                        "Copied theme definition from UUID %s for system theme %s",
                        original_uuid,
                        theme_name,
                    )
                except (ValueError, TypeError) as ex:
                    logger.error(
                        "Failed to parse theme JSON for UUID %s: %s",
                        original_uuid,
                        ex,
                    )
                    return
            else:
                logger.error(
                    "Referenced theme with UUID %s not found for system theme %s",
                    original_uuid,
                    theme_name,
                )
                return

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


async def on_startup(app: Litestar) -> None:  # noqa: C901
    settings: SupersetSettings = app.state.settings

    # ── check_secret_key ──────────────────────────────────────────────────
    # Refuse to start (or warn in debug/test mode) when SECRET_KEY is the
    # default placeholder.
    _check_secret_key(settings)

    # ── i18n: load translation catalogs ───────────────────────────────────
    # Load all available language packs from ``superset/translations/`` and
    # register them with the module-level ``i18n`` catalog so that
    # ``gettext`` / ``lazy_gettext`` calls work from the first request.
    try:
        from superset.i18n import (
            init_plural_data as _init_plural_data,
            init_translations as _init_translations,
        )

        _translations_root = Path(__file__).parent / "translations"
        _catalogs: dict[str, dict[str, str]] = {}
        _plural_tables: dict[str, dict[str, list[str]]] = {}
        _plural_rules: dict[str, str] = {}
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
                            _plural_table: dict[str, list[str]] = {}
                            for _k, _v in _domain_data.items():
                                if _k == "":
                                    # metadata entry — extract Plural-Forms
                                    if isinstance(_v, dict):
                                        _pf = _v.get("plural_forms") or _v.get(
                                            "Plural-Forms", ""
                                        )
                                        _m = re.search(
                                            r"plural\s*=\s*(.+?);?\s*$", str(_pf)
                                        )
                                        if _m:
                                            _plural_rules[_lang_dir.name] = _m.group(1)
                                    continue
                                if isinstance(_v, list) and len(_v) >= 1:
                                    _catalog[_k] = _v[0] if _v[0] else _k
                                    if len(_v) >= 2:
                                        _plural_table[_k] = [
                                            _form if _form else "" for _form in _v
                                        ]
                                elif isinstance(_v, str):
                                    _catalog[_k] = _v
                            if _plural_table:
                                _plural_tables[_lang_dir.name] = _plural_table
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
                        logger.debug(
                            "Failed to load translation catalog %s",
                            _lang_dir.name,
                            exc_info=True,
                        )
        _init_translations(_catalogs)
        _init_plural_data(_plural_tables, _plural_rules)
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
                    "ENABLE_TIME_ROTATE": getattr(
                        settings, "enable_time_rotate", False
                    ),
                    "TIME_ROTATE_LOG_LEVEL": getattr(
                        settings, "time_rotate_log_level", 20
                    ),
                    "FILENAME": getattr(settings, "log_filename", ""),
                    "ROLLOVER": getattr(settings, "rollover", "midnight"),
                    "INTERVAL": getattr(settings, "log_interval", 1),
                    "BACKUP_COUNT": getattr(settings, "backup_count", 30),
                },
                getattr(settings, "debug", False),
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                "LOGGING_CONFIGURATOR failed; falling through to structlog",
                exc_info=True,
            )

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
    # Wire the module-level ``superset.events.event_logger`` singleton now
    # that the session factory exists.  If EVENT_LOGGER is set to a custom
    # instance in superset_config.py we use that; otherwise we default to
    # AsyncDBEventLogger(session_factory) which persists audit rows to the
    # metadata DB.
    try:
        import superset.events as _events_mod
        from superset.events import (
            configure_event_logger,
            get_event_logger_from_cfg_value,
        )

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

    # Initialize a dedicated Redis client for Global Async Query event streams.
    # Only when GLOBAL_ASYNC_QUERIES is enabled; when disabled we fall back to
    # the shared auth-cache Redis so DI providers reading ``state.event_redis``
    # still work without a NoneType dereference.
    if feature_flag_manager.is_feature_enabled("GLOBAL_ASYNC_QUERIES"):
        # Raises UnsupportedCacheBackendError for unsupported CACHE_TYPE —
        # hard startup failure on misconfiguration is intentional.
        app.state.event_redis = _build_gaq_redis(settings)
    else:
        app.state.event_redis = app.state.redis

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
        # behaviour identical to the original Superset.
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
            # Each theme is processed in its own independent transaction so
            # partial success is preserved when one theme's DB operation fails
            # and the other succeeds.
            for theme_name, theme_config in theme_seeds:
                if callable(theme_config):
                    theme_config = theme_config()
                if not isinstance(theme_config, dict):
                    continue
                try:
                    await _seed_one_theme(
                        app.state.session_factory, theme_name, theme_config
                    )
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "Theme seeding skipped (DB may not be migrated yet)",
                        exc_info=True,
                    )
    except Exception:  # noqa: BLE001
        logger.debug("sync_config_to_db failed (non-fatal)", exc_info=True)

    # ── FLASK_APP_MUTATOR ──────────────────────────────────────────────────
    # Call the user-supplied last-mile hook with the Litestar app object.
    # Mirrors ``SupersetAppInitializer.init_app_in_ctx`` which calls the
    # configured app-mutator with ``self.superset_app``.
    flask_app_mutator = getattr(settings, "flask_app_mutator", None)
    if callable(flask_app_mutator):
        try:
            flask_app_mutator(app)
        except Exception:  # noqa: BLE001
            logger.warning("FLASK_APP_MUTATOR raised an exception", exc_info=True)

    # Initialize active WebSocket connections tracker
    app.state.active_websockets = {}

    # Start periodic channel cleanup only when GLOBAL_ASYNC_QUERIES is enabled.
    # Starting it when GAQ is disabled would sweep streams that were never
    # written — harmless but wasteful.
    _cleanup_redis = app.state.event_redis
    if (
        feature_flag_manager.is_feature_enabled("GLOBAL_ASYNC_QUERIES")
        and _cleanup_redis is not None
    ):
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
            redis=_cleanup_redis,
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
    # Close the dedicated event Redis client (if distinct from the auth cache client).
    event_redis = getattr(app.state, "event_redis", None)
    main_redis = getattr(app.state, "redis", None)
    if event_redis is not None and event_redis is not main_redis:
        try:
            await event_redis.close()
            logger.info("Event Redis connection closed")
        except Exception:  # noqa: BLE001
            logger.debug("Failed to close event Redis connection", exc_info=True)
    if hasattr(app.state, "redis") and app.state.redis is not None:
        await app.state.redis.close()
        logger.info("Redis connection closed")


def _check_secret_key(settings: SupersetSettings) -> None:
    """Refuse to start (or warn in debug/test mode) when SECRET_KEY is the
    default placeholder.
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

    _is_test = os.environ.get("SUPERSET_TESTENV", "false").lower() in (
        "true",
        "1",
        "yes",
    )
    if settings.debug or _is_test:
        logger.warning("Debug mode identified with default secret key")
        _log_default_secret_key_warning()
        return

    _log_default_secret_key_warning()
    logger.error("Refusing to start due to insecure SECRET_KEY")
    sys.exit(1)


def _validate_global_async_queries_config(settings: SupersetSettings) -> None:
    """Validate Global Async Queries config at app build time.

    Refuses to start the app if:
      1. CACHE_CONFIG or DATA_CACHE_CONFIG has a null/None cache type, or
      2. the JWT secret is shorter than 32 bytes.
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


def _apply_ssl_kwargs(target: dict[str, Any], cache_config: dict[str, Any]) -> None:
    """Merge SSL-related keys from *cache_config* into *target* in-place.

    Shared by ``_build_redis_cache_client`` and ``_build_redis_sentinel_client``
    to avoid duplicating the SSL-keyword logic.  Only sets keys whose config
    values are non-empty/non-None, mirroring the conditional assignments in the
    original ``get_cache_backend`` helper.
    """
    target["ssl"] = True
    if ssl_certfile := cache_config.get("CACHE_REDIS_SSL_CERTFILE") or None:
        target["ssl_certfile"] = ssl_certfile
    if ssl_keyfile := cache_config.get("CACHE_REDIS_SSL_KEYFILE") or None:
        target["ssl_keyfile"] = ssl_keyfile
    if ssl_cert_reqs := cache_config.get("CACHE_REDIS_SSL_CERT_REQS", "required"):
        target["ssl_cert_reqs"] = ssl_cert_reqs
    if ssl_ca_certs := cache_config.get("CACHE_REDIS_SSL_CA_CERTS") or None:
        target["ssl_ca_certs"] = ssl_ca_certs


def _build_redis_cache_client(cache_config: dict[str, Any]) -> Any:
    """Construct an ``redis.asyncio.Redis`` from a ``RedisCache`` config dict.

    Extracted from ``_build_gaq_redis`` to reduce its cyclomatic complexity.
    Behaviour is identical to the original inline block.
    """
    from redis.asyncio import Redis as AsyncRedis

    host = cache_config.get("CACHE_REDIS_HOST", "localhost")
    port = int(cache_config.get("CACHE_REDIS_PORT", 6379))
    db = int(cache_config.get("CACHE_REDIS_DB", 0))
    password = cache_config.get("CACHE_REDIS_PASSWORD") or None
    username = cache_config.get("CACHE_REDIS_USER") or None
    ssl = bool(cache_config.get("CACHE_REDIS_SSL", False))
    kwargs: dict[str, Any] = {
        "host": host,
        "port": port,
        "db": db,
        "decode_responses": True,
    }
    if password:
        kwargs["password"] = password
    if username:
        kwargs["username"] = username
    if ssl:
        _apply_ssl_kwargs(kwargs, cache_config)
    logger.info(
        "GAQ event stream: using RedisCache backend at %s:%s db=%s",
        host,
        port,
        db,
    )
    return AsyncRedis(**kwargs)


def _build_redis_sentinel_client(cache_config: dict[str, Any]) -> Any:
    """Construct an ``redis.asyncio.Sentinel`` master handle from a
    ``RedisSentinelCache`` config dict.

    Extracted from ``_build_gaq_redis`` to reduce its cyclomatic complexity.
    Behaviour is identical to the original inline block.
    """
    from redis.asyncio.sentinel import Sentinel as AsyncSentinel

    sentinels: list[tuple[str, int]] = cache_config.get(
        "CACHE_REDIS_SENTINELS", [("127.0.0.1", 26379)]
    )
    master: str = cache_config.get("CACHE_REDIS_SENTINEL_MASTER", "mymaster")
    password = cache_config.get("CACHE_REDIS_PASSWORD") or None
    sentinel_password = cache_config.get("CACHE_REDIS_SENTINEL_PASSWORD") or None
    db = int(cache_config.get("CACHE_REDIS_DB", 0))
    ssl = bool(cache_config.get("CACHE_REDIS_SSL", False))
    sentinel_kwargs: dict[str, Any] = {}
    if sentinel_password:
        sentinel_kwargs["password"] = sentinel_password
    # ``db`` reaches the master connection via connection kwargs forwarded
    # by Sentinel to ``master_for`` connections.
    master_kwargs: dict[str, Any] = {"decode_responses": True, "db": db}
    if password:
        master_kwargs["password"] = password
    if ssl:
        _apply_ssl_kwargs(master_kwargs, cache_config)
    sentinel = AsyncSentinel(sentinels, sentinel_kwargs=sentinel_kwargs)
    logger.info(
        "GAQ event stream: using RedisSentinelCache backend, master=%s sentinels=%s",
        master,
        sentinels,
    )
    return sentinel.master_for(master, **master_kwargs)


def _build_gaq_redis(settings: SupersetSettings) -> Any:
    """Build an async Redis client from GLOBAL_ASYNC_QUERIES_CACHE_BACKEND config.

    Supported ``CACHE_TYPE`` values:
    - ``"RedisCache"``           → ``redis.asyncio.Redis`` (direct connection)
    - ``"RedisSentinelCache"``   → ``redis.asyncio.Sentinel`` (HA topology)

    Raises ``UnsupportedCacheBackendError`` for any other CACHE_TYPE (including
    absent/None). Only called when GLOBAL_ASYNC_QUERIES is enabled; callers are
    responsible for the feature-flag guard.
    """
    from superset.async_events.manager import UnsupportedCacheBackendError

    cache_config: dict[str, Any] = (
        getattr(settings, "global_async_queries_cache_backend", {}) or {}
    )
    cache_type = cache_config.get("CACHE_TYPE")

    if cache_type == "RedisCache":
        return _build_redis_cache_client(cache_config)

    if cache_type == "RedisSentinelCache":
        return _build_redis_sentinel_client(cache_config)

    raise UnsupportedCacheBackendError("Unsupported cache backend configuration")


def _build_cors_config(settings: SupersetSettings) -> CORSConfig | None:
    """Map the ``CORS_OPTIONS`` dict onto Litestar's ``CORSConfig``.

    When ``ENABLE_CORS`` is false, returns ``None`` (no CORS at all).

    ``CORS_OPTIONS`` → ``CORSConfig`` field mapping:
    * ``origins``              → ``allow_origins``
    * ``methods``              → ``allow_methods``
    * ``allow_headers``        → ``allow_headers``
    * ``expose_headers``       → ``expose_headers``
    * ``supports_credentials`` → ``allow_credentials``
    * ``max_age``              → ``max_age``

    Upstream defaults applied for omitted keys (``origins='*'``, all standard
    methods, ``allow_headers='*'``, ``supports_credentials=False``).

    The upstream ``resources`` per-path-scoping option has no Litestar
    equivalent and is ignored.
    """
    if not settings.enable_cors:
        return None

    opts = settings.cors_options or {}

    def _as_list(value: Any) -> list[str]:
        if isinstance(value, str):
            return [value]
        return list(value)

    # Upstream default origins is "*"; Litestar default allow_origins is ["*"].
    allow_origins = _as_list(opts.get("origins", ["*"]))
    # Upstream default methods cover all standard verbs; "*" expresses that.
    allow_methods = _as_list(opts["methods"]) if "methods" in opts else ["*"]
    # Upstream default allow_headers is "*".
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

    # When Global Async Queries is enabled, refuse to start with a weak JWT secret.
    _validate_global_async_queries_config(settings)

    # ------------------------------------------------------------------
    # Sub-path serving (APPLICATION_ROOT / SUPERSET_APP_ROOT).
    #
    # When Superset is served under a URL prefix (e.g. behind a reverse proxy
    # at ``/app/prefix`` or via the e2e ``app_root`` matrix), the handlers stay
    # registered at the root and an ASGI middleware strips the prefix off the
    # incoming path — exactly what a path-stripping reverse proxy does.
    #
    # This is deliberately NOT done via Litestar's ``path=`` (which would mount
    # every route UNDER the prefix) nor uvicorn ``--root-path`` (which prepends
    # root_path to the path and 404s on a directly-accessed server).  Both of
    # those serve ONLY prefixed paths, but the Cypress harness hits the backend
    # two different ways: ``cy.visit('/login/')`` resolves against the full
    # baseUrl and keeps the prefix (``/app/prefix/login/``), while
    # ``cy.request('/login/')`` resolves the root-relative path against the
    # ORIGIN and drops it (``/login/``).  Stripping (prefix optional) serves
    # both; the SPA bootstrap / asset URLs still carry the prefix so the
    # browser requests it and the middleware strips it back off.
    #
    # Precedence: the ``SUPERSET_APP_ROOT`` env var (set by the e2e harness)
    # wins, then the ``application_root`` config.  Normalised to ``/prefix``
    # (leading slash, no trailing slash); "/" or "" means "no prefix".
    # ------------------------------------------------------------------
    app_root_raw = os.environ.get("SUPERSET_APP_ROOT") or settings.application_root
    app_root = "/" + app_root_raw.strip("/") if app_root_raw.strip("/") else ""
    if app_root:
        # Keep the SPA bootstrap (``common.application_root``) and the asset
        # URLs emitted by the templates in sync with the public prefix.
        settings.application_root = app_root
        if not settings.static_assets_prefix:
            settings.static_assets_prefix = app_root

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
        # Mirrors talisman's ``app.jinja_env.globals['csp_nonce']``
        # (talisman.py:193).  The SecurityHeadersMiddleware generates a per-request
        # nonce and exposes it via a ContextVar (``get_csp_nonce``).  We read it
        # from there rather than the Jinja render context: ``macros.get_nonce()``
        # is reached through ``partials/asset_bundle.html``, which imports
        # ``macros.html`` *without* context, so a ``pass_context`` callable would
        # see an empty macro-local context (no ``request``) and return "" — an
        # empty nonce makes the CSP ``strict-dynamic`` policy block every
        # ``<script>`` and the SPA never boots.
        from collections.abc import Mapping

        from superset.middleware.security_headers import get_csp_nonce

        def _csp_nonce(ctx: Mapping[str, Any]) -> str:
            return get_csp_nonce()

        engine.register_template_callable(key="csp_nonce", template_callable=_csp_nonce)

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
        # Use the dedicated event_redis client when available; fall back to
        # the shared auth-cache Redis.
        _event_redis = getattr(state, "event_redis", None) or getattr(
            state, "redis", None
        )
        return _aem_mod.AsyncEventManager(
            redis=cast("Redis[Any]", _event_redis),
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

    # Build CSRF middleware (session-based, upstream-CSRF compatible)
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
        # list used dotted view-function names; we translate them to URL
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
        # (old upstream style) or plain URL paths.  We include them as-is because the
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

    app = Litestar(
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
            # RateLimitMiddleware: gated on the RATELIMIT_ENABLED master switch
            # (off by default outside production).  Applies RATELIMIT_APPLICATION
            # to every request and AUTH_RATE_LIMIT to login POSTs.  Placed after
            # ProxyFix so the client IP used as the limit key is the corrected one.
            RateLimitMiddleware(),
            LocaleMiddleware(),
            # HTTPHeadersMiddleware applies OVERRIDE_HTTP_HEADERS / HTTP_HEADERS /
            # DEFAULT_HTTP_HEADERS from settings onto every response.  Equivalent
            # to the original ``register_request_handlers`` after-request hook.
            HTTPHeadersMiddleware(),
            # RequestContextMiddleware must run BEFORE the auth middleware
            # so audit-logging code paths inside auth/guards (which fire
            # synchronously from controllers) can resolve the request /
            # form_data ContextVars.  It also must run before CSRF so the
            # CSRF check sees a body the middleware has already cached.
            RequestContextMiddleware(),
            SupersetAuthMiddleware,
            # AsyncTokenMiddleware mints / refreshes the ``async-token`` JWT
            # cookie on authenticated responses.  Gated on the same two flags
            # (GLOBAL_ASYNC_QUERIES + REGISTER_REQUEST_HANDLERS).  Placed after
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

    # Strip the application-root prefix BEFORE routing.  Litestar's own
    # middleware wrap the matched handler (they run after routing), so a
    # path-rewriting middleware must wrap the whole ASGI app from the outside.
    # The wrapper is a transparent ASGI app; callers that introspect Litestar
    # internals never set a prefix, so they always get the raw instance.
    if app_root:
        return cast("Litestar", AppRootMiddleware(app, app_root=app_root))

    return app
