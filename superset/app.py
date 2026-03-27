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
from pathlib import Path
from typing import Any

from litestar import get, Litestar
from litestar.config.compression import CompressionConfig
from litestar.config.cors import CORSConfig
from litestar.contrib.jinja import JinjaTemplateEngine
from litestar.datastructures import State
from litestar.di import Provide
from litestar.openapi import OpenAPIConfig
from litestar.response import Response
from litestar.static_files import create_static_files_router
from litestar.template.config import TemplateConfig
from sqlalchemy.engine import make_url

from superset.config import SupersetSettings
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
    generic_exception_handler,
    superset_exception_handler,
    SupersetException,
)
from superset.logging import configure_logging
from superset.middleware.auth import SupersetAuthMiddleware
from superset.middleware.csrf import create_csrf_config
from superset.middleware.rate_limit import RateLimitMiddleware
from superset.middleware.security_headers import SecurityHeadersMiddleware

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
async def health_check() -> dict[str, str]:
    return {"status": "OK"}


@get("/health", opt={"exclude_from_auth": True})
async def health() -> dict[str, str]:
    return {"status": "OK"}


@get("/healthcheck", opt={"exclude_from_auth": True})
async def healthcheck() -> dict[str, str]:
    return {"status": "OK"}


@get("/ping", opt={"exclude_from_auth": True})
async def ping() -> dict[str, str]:
    return {"status": "OK"}


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


async def on_startup(app: Litestar) -> None:
    settings: SupersetSettings = app.state.settings
    configure_logging(settings)

    # Initialize feature flag manager from config
    from superset.utils.feature_flags import feature_flag_manager

    feature_flag_manager.init_from_config(settings.feature_flags)

    engine = create_db_engine(settings.sqlalchemy_database_uri)
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)

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

        manager = AsyncEventManager(redis=app.state.redis)
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
        except Exception:
            pass

    if hasattr(app.state, "engine"):
        await dispose_engine(app.state.engine)
        logger.info("Database engine disposed")
    if hasattr(app.state, "redis") and app.state.redis is not None:
        await app.state.redis.close()
        logger.info("Redis connection closed")


def create_app(
    settings: SupersetSettings | None = None,
) -> Litestar:
    if settings is None:
        # secret_key is resolved at runtime from env vars or superset_config.py
        settings = SupersetSettings()  # type: ignore[call-arg]

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
    from superset.controllers.embedded_dashboard import EmbeddedDashboardController
    from superset.controllers.explore import ExploreController
    from superset.controllers.explore_form_data import ExploreFormDataController
    from superset.controllers.explore_permalink import ExplorePermalinkController
    from superset.controllers.import_export import ImportExportController

    # LegacyApiController deferred — its /api/v1 path prefix overlaps
    # with existing controllers. Will be added with path aliases in Phase 7.
    # from superset.controllers.legacy_api import LegacyApiController
    from superset.controllers.log import LogController
    from superset.controllers.query import QueryController
    from superset.controllers.report import ReportScheduleController
    from superset.controllers.report_log import ReportExecutionLogController
    from superset.controllers.rls import RLSController
    from superset.controllers.role import RoleController
    from superset.controllers.saved_query import SavedQueryController
    from superset.controllers.sqllab import SqlLabController
    from superset.controllers.sqllab_permalink import SqlLabPermalinkController
    from superset.controllers.tag import TagController
    from superset.controllers.theme import ThemeController
    from superset.controllers.user import UserController, UserRegistrationsController
    from superset.controllers.user_me import CurrentUserController

    # Import WebSocket handler (Phase 6)
    from superset.websocket.events import AsyncQueryWebSocket

    route_handlers: list[Any] = [
        health_check,
        health,
        healthcheck,
        ping,
        readiness_probe,
        SPAController,
        SecurityController,
        # Phase 4: core API
        ChartController,
        DashboardController,
        DashboardFilterStateController,
        DatabaseController,
        DatasetController,
        QueryController,
        SavedQueryController,
        SqlLabController,
        SqlLabPermalinkController,
        # Phase 5: remaining API
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
        UserRegistrationsController,
        TagController,
        ThemeController,
        EmbeddedDashboardController,
        CacheController,
        AsyncEventController,
        RLSController,
        ImportExportController,
        # LegacyApiController,  # deferred — path overlap (see import)
        # Phase 7: cleanup — datasource and role controllers
        DatasourceController,
        RoleController,
        # Phase 6: WebSocket
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
        return _aem_mod.AsyncEventManager(redis=state.redis)

    # Build CSRF config
    csrf_config = None
    if settings.csrf_enabled:
        secret_key = settings.secret_key
        if hasattr(secret_key, "get_secret_value"):
            secret_key = secret_key.get_secret_value()
        csrf_config = create_csrf_config(
            secret=secret_key,
            cookie_name=settings.csrf_cookie_name,
            header_name=settings.csrf_header_name,
            exclude_paths=[
                "/api/v1/health",
                "/health",
                "/healthcheck",
                "/ping",
                "/healthz",
                "/api/v1/security/csrf_token/",
            ],
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
        middleware=[
            SecurityHeadersMiddleware(),
            RateLimitMiddleware(),
            SupersetAuthMiddleware,
        ],
        csrf_config=csrf_config,
        on_startup=startup_hooks,
        on_shutdown=[on_shutdown],
        exception_handlers={
            SupersetException: superset_exception_handler,
            Exception: generic_exception_handler,
        },
        openapi_config=OpenAPIConfig(
            title="Superset API",
            version="v1",
            path="/swagger/v1",
        ),
        cors_config=CORSConfig(allow_origins=settings.cors_allow_origins)
        if settings.cors_allow_origins
        else None,
        compression_config=CompressionConfig(backend="gzip"),
        template_config=TemplateConfig(
            directory=Path(__file__).parent / "templates",
            engine=JinjaTemplateEngine,
            engine_callback=_register_template_globals,
        ),
        state=State({"settings": settings}),
    )
