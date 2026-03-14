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

import json
import logging
from pathlib import Path
from typing import Any

from litestar import Litestar, get
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
from liteset.logging import configure_logging
from liteset.controllers.spa import SPAController
from liteset.db.session import create_db_engine, create_session_factory, dispose_engine
from liteset.dependencies import provide_async_session
from liteset.exceptions import (
    LitesetException,
    generic_exception_handler,
    liteset_exception_handler,
)

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_manifest(manifest_path: Path) -> dict[str, Any]:
    if manifest_path.exists():
        return json.loads(manifest_path.read_text())
    return {}


def _make_manifest_lookup(
    manifest: dict[str, Any], asset_type: str
) -> Any:
    def lookup(ctx: dict[str, Any], bundle_name: str) -> list[str]:
        entry = manifest.get(bundle_name, {})
        if isinstance(entry, dict):
            return entry.get(asset_type, [])
        return []

    return lookup


@get("/api/v1/health")
async def health_check() -> dict[str, str]:
    return {"status": "OK"}


async def on_startup(app: Litestar) -> None:
    settings: LitesetSettings = app.state.settings
    configure_logging(settings)
    engine = create_db_engine(settings.sqlalchemy_database_uri)
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)
    logger.info(
        "Liteset started with DB: %s",
        make_url(settings.sqlalchemy_database_uri).render_as_string(hide_password=True),
    )


async def on_shutdown(app: Litestar) -> None:
    if hasattr(app.state, "engine"):
        await dispose_engine(app.state.engine)
        logger.info("Database engine disposed")


def create_app(
    settings: LitesetSettings | None = None,
    enable_flask_fallback: bool = True,
) -> Litestar:
    if settings is None:
        settings = LitesetSettings()

    route_handlers: list[Any] = [health_check, SPAController]

    if enable_flask_fallback:
        try:
            from liteset.fallback import create_flask_fallback

            route_handlers.append(create_flask_fallback())
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

    route_handlers.extend([
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
    ])

    # TODO(liteset/data-layer): register AuthMiddleware after auth methods are implemented
    #   middleware=[LitesetAuthMiddleware]
    # TODO(liteset/data-layer): add current_user dependency
    #   "current_user": Provide(get_current_user)
    # TODO(liteset/data-layer): add CSRFConfig after session system is ready
    #   csrf_config=CSRFConfig(secret=settings.secret_key, ...)
    # TODO(liteset/cleanup): add security headers middleware (CSP, HSTS, X-Frame-Options)
    #   replaces flask-talisman

    return Litestar(
        route_handlers=route_handlers,
        dependencies={"session": Provide(provide_async_session)},
        on_startup=[on_startup],
        on_shutdown=[on_shutdown],
        exception_handlers={
            LitesetException: liteset_exception_handler,
            Exception: generic_exception_handler,
        },
        openapi_config=OpenAPIConfig(
            title="Superset API", version="v1", path="/schema"
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
