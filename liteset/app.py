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

import json  # noqa: TID251 — liteset must not depend on superset for core imports
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
from litestar.static_files import create_static_files_router
from litestar.template.config import TemplateConfig
from sqlalchemy.engine import make_url

from liteset.config import LitesetSettings
from liteset.controllers.security import SecurityController
from liteset.controllers.spa import SPAController
from liteset.db.session import (
    create_db_engine,
    create_session_factory,
    dispose_engine,
)
from liteset.dependencies import (
    get_current_user,
    provide_async_session,
    provide_request_cache,
    provide_security_manager,
)
from liteset.exceptions import (
    generic_exception_handler,
    liteset_exception_handler,
    LitesetException,
)
from liteset.logging import configure_logging
from liteset.middleware.auth import LitesetAuthMiddleware
from liteset.middleware.csrf import create_csrf_config

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_manifest(manifest_path: Path) -> dict[str, Any]:
    if manifest_path.exists():
        return json.loads(manifest_path.read_text())
    return {}


def _make_manifest_lookup(manifest: dict[str, Any], asset_type: str) -> Any:
    def lookup(ctx: dict[str, Any], bundle_name: str) -> list[str]:
        entry = manifest.get(bundle_name, {})
        if isinstance(entry, dict):
            return entry.get(asset_type, [])
        return []

    return lookup


@get("/api/v1/health", opt={"exclude_from_auth": True})
async def health_check() -> dict[str, str]:
    return {"status": "OK"}


async def on_startup(app: Litestar) -> None:
    settings: LitesetSettings = app.state.settings
    configure_logging(settings)
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

    logger.info(
        "Liteset started with DB: %s",
        make_url(settings.sqlalchemy_database_uri).render_as_string(hide_password=True),
    )


async def on_shutdown(app: Litestar) -> None:
    if hasattr(app.state, "engine"):
        await dispose_engine(app.state.engine)
        logger.info("Database engine disposed")
    if hasattr(app.state, "redis") and app.state.redis is not None:
        await app.state.redis.close()
        logger.info("Redis connection closed")


def create_app(
    settings: LitesetSettings | None = None,
    enable_flask_fallback: bool = True,
) -> Litestar:
    if settings is None:
        # secret_key is resolved at runtime from env vars or superset_config.py
        settings = LitesetSettings()  # type: ignore[call-arg]

    # Import core API controllers (Phase 4)
    from liteset.controllers.chart import ChartController
    from liteset.controllers.dashboard import DashboardController
    from liteset.controllers.dashboard_filter_state import (
        DashboardFilterStateController,
    )
    from liteset.controllers.database import DatabaseController
    from liteset.controllers.dataset import DatasetController
    from liteset.controllers.query import QueryController
    from liteset.controllers.saved_query import SavedQueryController
    from liteset.controllers.sqllab import SqlLabController
    from liteset.controllers.sqllab_permalink import SqlLabPermalinkController

    # Import remaining API controllers (Phase 5)
    from liteset.controllers.advanced_data_type import AdvancedDataTypeController
    from liteset.controllers.annotation import AnnotationController
    from liteset.controllers.annotation_layer import AnnotationLayerController
    from liteset.controllers.async_event import AsyncEventsController
    from liteset.controllers.available_domains import AvailableDomainsController
    from liteset.controllers.cache import CacheController
    from liteset.controllers.css_template import CssTemplateController
    from liteset.controllers.embedded_dashboard import EmbeddedDashboardController
    from liteset.controllers.explore import ExploreController
    from liteset.controllers.explore_form_data import ExploreFormDataController
    from liteset.controllers.explore_permalink import ExplorePermalinkController
    from liteset.controllers.import_export import ImportExportController
    # LegacyApiController deferred — its /api/v1 path prefix overlaps
    # with existing controllers. Will be added with path aliases in Phase 7.
    # from liteset.controllers.legacy_api import LegacyApiController
    from liteset.controllers.log import LogController
    from liteset.controllers.report import ReportScheduleController
    from liteset.controllers.report_log import ReportExecutionLogController
    from liteset.controllers.rls import RLSController
    from liteset.controllers.tag import TagController
    from liteset.controllers.theme import ThemeController
    from liteset.controllers.user import UserController, UserRegistrationsController
    from liteset.controllers.user_me import CurrentUserController

    route_handlers: list[Any] = [
        health_check,
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
        AsyncEventsController,
        RLSController,
        ImportExportController,
        # LegacyApiController,  # deferred — path overlap (see import)
    ]
    startup_hooks: list[Any] = [on_startup]

    if enable_flask_fallback:
        try:
            from liteset.fallback import create_flask_fallback, init_flask_fallback

            route_handlers.append(create_flask_fallback())
            startup_hooks.append(init_flask_fallback)
        except ImportError:
            logger.warning("Flask fallback not available")

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
            exclude_paths=["/api/v1/health", "/api/v1/security/csrf_token/"],
        )

    # TODO(liteset/cleanup): add security headers middleware
    #   (CSP, HSTS, X-Frame-Options) — replaces flask-talisman

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
        },
        middleware=[LitesetAuthMiddleware],
        csrf_config=csrf_config,
        on_startup=startup_hooks,
        on_shutdown=[on_shutdown],
        exception_handlers={
            LitesetException: liteset_exception_handler,
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
