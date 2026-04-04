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

from litestar import get, Litestar, Request
from litestar.config.compression import CompressionConfig
from litestar.config.cors import CORSConfig
from litestar.contrib.jinja import JinjaTemplateEngine
from litestar.datastructures import State
from litestar.di import Provide
from litestar.handlers import post
from litestar.openapi import OpenAPIConfig
from litestar.response import Response
from litestar.static_files import create_static_files_router
from litestar.template.config import TemplateConfig
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from superset.config import SupersetSettings
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
    generic_exception_handler,
    superset_exception_handler,
    SupersetException,
)
from superset.logging import configure_logging
from superset.middleware.auth import SupersetAuthMiddleware
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
        except Exception:  # noqa: S110
            pass

    if hasattr(app.state, "engine"):
        await dispose_engine(app.state.engine)
        logger.info("Database engine disposed")
    if hasattr(app.state, "redis") and app.state.redis is not None:
        await app.state.redis.close()
        logger.info("Redis connection closed")


def create_app(  # noqa: C901
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
    from superset.controllers.group import GroupController
    from superset.controllers.import_export import ImportExportController

    # LegacyApiController deferred — its /api/v1 path prefix overlaps
    # with existing controllers. Will be added with path aliases in Phase 7.
    # from superset.controllers.legacy_api import LegacyApiController
    from superset.controllers.log import LogController
    from superset.controllers.permission_view import PermissionViewController
    from superset.controllers.query import QueryController
    from superset.controllers.report import ReportScheduleController
    from superset.controllers.report_log import ReportExecutionLogController
    from superset.controllers.rls import RLSController
    from superset.controllers.role import RoleController
    from superset.controllers.saved_query import SavedQueryController
    from superset.controllers.sqllab import SqlLabController
    from superset.controllers.tab_state import (
        TabStateController,
        TableSchemaController,
    )
    from superset.controllers.sqllab_permalink import SqlLabPermalinkController
    from superset.controllers.tag import TagController
    from superset.controllers.theme import ThemeController
    from superset.controllers.user import UserController, UserRegistrationsController
    from superset.controllers.user_me import CurrentUserController

    # Import WebSocket handler (Phase 6)
    from superset.websocket.events import AsyncQueryWebSocket

    # ------------------------------------------------------------------
    # Legacy explore_json endpoint — serves deck.gl and other legacy viz
    # types that POST/GET form_data instead of /api/v1/chart/data.
    # ------------------------------------------------------------------
    async def _handle_explore_json(  # noqa: C901
        request: Request[Any, Any, Any],
        session: AsyncSession,
        datasource_type: str | None = None,
        datasource_id: int | None = None,
    ) -> Response[Any]:
        """Core handler for legacy explore_json requests.

        Parses form_data from the request (GET query-string or POST form
        body), resolves the datasource, builds a viz object, executes
        the query asynchronously, and returns the JSON payload.
        """
        import json as _json

        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        from superset.models.connectors import SqlaTable
        from superset.viz import get_viz as make_viz

        # ---- 1. Parse form_data ----
        form_data: dict[str, Any] = {}

        # Try JSON body first
        if request.content_type and "json" in request.content_type:
            try:
                body = await request.json()
                if isinstance(body, dict):
                    # chart data API shape: {queries: [{...}]}
                    if "queries" in body and body["queries"]:
                        form_data.update(body["queries"][0])
                    else:
                        form_data.update(body)
            except Exception:  # noqa: S110
                pass

        # Form-encoded body
        try:
            form = await request.form()
            form_data_str = form.get("form_data", "")
            if form_data_str:
                try:
                    parsed = _json.loads(form_data_str)
                    if isinstance(parsed, dict):
                        queries = parsed.get("queries")
                        if isinstance(queries, list) and queries:
                            form_data.update(queries[0])
                        else:
                            form_data.update(parsed)
                except (ValueError, TypeError):
                    pass
        except Exception:  # noqa: S110
            pass

        # Query-string params can override the body
        args_form_data = request.query_params.get("form_data", "")
        if args_form_data:
            try:
                form_data.update(_json.loads(args_form_data))
            except (ValueError, TypeError):
                pass

        if not form_data:
            return Response(
                content={"error": "No form_data provided"},
                status_code=400,
                media_type="application/json",
            )

        # ---- 1b. Filter REJECTED_FORM_DATA_KEYS ----
        from superset.utils.feature_flags import feature_flag_manager

        if not feature_flag_manager.is_feature_enabled("ENABLE_JAVASCRIPT_CONTROLS"):
            _REJECTED_KEYS = {"js_tooltip", "js_onclick_href", "js_data_mutator"}  # noqa: N806
            form_data = {k: v for k, v in form_data.items() if k not in _REJECTED_KEYS}

        # ---- 1c. Merge saved slice params if slice_id present ----
        slice_id = form_data.get("slice_id")
        if slice_id:
            from superset.models.slice import Slice  # noqa: E402

            slice_stmt = select(Slice).where(Slice.id == int(slice_id))
            slice_result = await session.execute(slice_stmt)
            slc = slice_result.scalars().one_or_none()
            if slc:
                slice_form_data = _json.loads(str(slc.params or "{}"))
                slice_form_data.update(form_data)
                form_data = slice_form_data

        # ---- 2. Resolve datasource info ----
        # form_data.datasource = "<id>__<type>" takes precedence
        ds_str = form_data.get("datasource", "")
        if "__" in str(ds_str):
            parts = str(ds_str).split("__")
            if parts[0] and parts[0] != "None":
                datasource_id = int(parts[0])
                datasource_type = parts[1] if len(parts) > 1 else datasource_type

        if datasource_id is None:
            ds_id_raw = form_data.get("datasource_id")
            if ds_id_raw is not None:
                datasource_id = int(ds_id_raw)

        if datasource_id is None:
            return Response(
                content={
                    "error": "The dataset associated with this chart no longer exists"
                },
                status_code=400,
                media_type="application/json",
            )

        # ---- 3. Load datasource ----
        stmt = (
            select(SqlaTable)
            .where(SqlaTable.id == int(datasource_id))
            .options(
                selectinload(SqlaTable.database),
                selectinload(SqlaTable.columns),
                selectinload(SqlaTable.metrics),
            )
        )
        result = await session.execute(stmt)
        datasource = result.scalars().one_or_none()
        if not datasource:
            return Response(
                content={"error": "Datasource not found"},
                status_code=404,
                media_type="application/json",
            )

        # ---- 4. Determine response type ----
        response_type = "json"
        for opt in ("csv", "json", "query", "results", "samples"):
            if request.query_params.get(opt) == "true":
                response_type = opt
                break

        # ---- 4b. Check CSV export permission ----
        if response_type == "csv":
            from superset.dependencies import provide_security_manager

            sec_mgr = await provide_security_manager(session, request.app.state)
            user = getattr(request, "user", None)
            if not await sec_mgr.can_access("can_csv", "Superset", user=user):
                return Response(
                    content=_json.dumps(
                        {"error": "You don't have the rights to download as csv"}
                    ),
                    status_code=403,
                    media_type="application/json",
                )

        # ---- 5. Build viz object and execute query ----
        force = request.query_params.get("force") == "true"

        try:
            viz_obj = make_viz(
                datasource=datasource,
                form_data=form_data,
                force=force,
                settings=settings,
            )

            payload = await viz_obj.get_payload()

            # Serialize the payload
            payload_json = viz_obj.json_dumps(payload)
            has_error = viz_obj.has_error(payload)

            return Response(
                content=payload_json,
                status_code=400 if has_error else 200,
                media_type="application/json",
            )
        except Exception as ex:
            logger.exception("explore_json error")
            return Response(
                content=_json.dumps({"error": str(ex)}, default=str),
                status_code=400,
                media_type="application/json",
            )

    @post(
        "/superset/explore_json/{datasource_type:str}/{datasource_id:int}/",
        opt={"skip_csrf": True},
    )
    async def explore_json_post_with_ids(
        request: Request[Any, Any, Any],
        session: AsyncSession,
        datasource_type: str,
        datasource_id: int,
    ) -> Response[Any]:
        """POST /superset/explore_json/<type>/<id>/"""
        return await _handle_explore_json(
            request, session, datasource_type, datasource_id
        )

    @get(
        "/superset/explore_json/{datasource_type:str}/{datasource_id:int}/",
    )
    async def explore_json_get_with_ids(
        request: Request[Any, Any, Any],
        session: AsyncSession,
        datasource_type: str,
        datasource_id: int,
    ) -> Response[Any]:
        """GET /superset/explore_json/<type>/<id>/"""
        return await _handle_explore_json(
            request, session, datasource_type, datasource_id
        )

    @post("/superset/explore_json/", opt={"skip_csrf": True})
    async def explore_json_post(
        request: Request[Any, Any, Any],
        session: AsyncSession,
    ) -> Response[Any]:
        """POST /superset/explore_json/"""
        return await _handle_explore_json(request, session)

    @get(
        "/superset/explore_json/",
    )
    async def explore_json_get(
        request: Request[Any, Any, Any],
        session: AsyncSession,
    ) -> Response[Any]:
        """GET /superset/explore_json/"""
        return await _handle_explore_json(request, session)

    route_handlers: list[Any] = [
        explore_json_post_with_ids,
        explore_json_get_with_ids,
        explore_json_post,
        explore_json_get,
        health_check,
        health,
        healthcheck,
        ping,
        readiness_probe,
        AuthController,
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
        TabStateController,
        TableSchemaController,
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
        # Phase 7: cleanup — datasource, role, group, permission-view controllers
        DatasourceController,
        RoleController,
        GroupController,
        PermissionViewController,
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

    # Build CSRF middleware (session-based, Flask-WTF compatible)
    csrf_middleware = None  # Don't use Litestar built-in CSRF config
    if settings.csrf_enabled:
        from superset.middleware.csrf import (
            create_csrf_middleware,
        )

        _raw_secret = settings.secret_key
        secret_key: str = (
            _raw_secret.get_secret_value()
            if hasattr(_raw_secret, "get_secret_value")
            else str(_raw_secret)
        )
        csrf_middleware = create_csrf_middleware(
            secret=secret_key,
            header_name=getattr(
                settings,
                "csrf_header_name",
                "X-CSRFToken",
            ),
            max_age=604800,  # 1 week (WTF_CSRF_TIME_LIMIT)
            exclude_paths=[
                # Health probes
                "/health",
                "/healthcheck",
                "/ping",
                "/healthz",
                # Auth forms (no token before login)
                "/login",
                "/logout",
                # Original WTF_CSRF_EXEMPT_LIST:
                "/api/v1/chart/data",
                "/api/v1/dashboard/cache_dashboard_screenshot",
                "/superset/explore_json",
                "/superset/log",
                "/datasource/samples",
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
            *([csrf_middleware] if csrf_middleware else []),
        ],
        csrf_config=None,  # Using custom middleware
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
