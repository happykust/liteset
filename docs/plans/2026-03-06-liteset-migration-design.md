# Liteset: Migration Design Document

## Flask -> Litestar Migration for Apache Superset

**Author:** Nikolaevsky K.M.
**Date:** 2026-03-06
**Status:** Approved

---

## 1. Overview

Migration of Apache Superset backend from synchronous Flask/WSGI to asynchronous Litestar/ASGI using the Strangler Fig pattern. The goal is to achieve 2-3x RPS improvement and significant memory reduction while maintaining full API contract compatibility. The frontend must remain completely untouched.

> **Performance expectations:** The 2-3x RPS improvement is expected primarily for IO-bound
> workloads (concurrent DB queries, cache lookups). Memory savings come from replacing
> Gunicorn pre-fork workers (each forking the full application) with a single-process
> Uvicorn event loop. Actual gains depend on the deployment topology and workload profile —
> benchmarks in section 5.7 will quantify the real numbers.

### Key Decisions

| Decision | Choice | Rationale |
|---|---|---|
| ASGI Framework | Litestar | DI, OpenAPI, msgspec, strict typing, ~20-24k RPS |
| Migration Pattern | Strangler Fig | Incremental replacement, rollback capability |
| ASGI Server | Uvicorn + uvloop | De-facto standard, fair benchmark vs Gunicorn |
| Serialization | msgspec | 2-3x faster than Pydantic v1, replaces Marshmallow |
| ORM | SQLAlchemy 2.0 AsyncSession + asyncpg | Async Select API, same models |
| Config | Pydantic Settings | Type-safe validation, superset_config.py compat |
| Celery | Keep as-is | Orthogonal to HTTP layer migration |
| Coexistence | Flask mounted inside Litestar via WsgiToAsgi | Single process, single port |

---

## 2. Project Structure

### 2.1 Directory Layout

```
liteset/                              # Project root
├── superset/                         # Flask backend (AS-IS, untouched until cleanup)
├── liteset/                          # New Litestar backend
│   ├── app.py                        # App factory: create_app() -> Litestar
│   ├── config.py                     # Pydantic Settings + SupersetConfigSettingsSource
│   ├── dependencies.py               # Global Provide (AsyncSession, current_user, request_cache)
│   ├── exceptions.py                 # SIP-40 exception hierarchy + handlers (see section 3.9)
│   ├── i18n.py                       # contextvars-based i18n (see section 20)
│   ├── logging.py                    # structlog configuration (see section 16)
│   ├── middleware/
│   │   ├── auth.py                   # AuthMiddleware (cookie/JWT/API-key)
│   │   ├── csrf.py                   # CSRF protection
│   │   ├── cors.py                   # CORS config
│   │   ├── locale.py                 # Locale middleware (i18n, see section 20)
│   │   └── rate_limit.py            # Rate limiting (Redis-backed, see section 24)
│   ├── params/
│   │   └── rison.py                  # Rison query parameter decoder (see section 8.4)
│   ├── guards/
│   │   └── rbac.py                   # RBAC Guards (replaces @protect)
│   ├── db/
│   │   ├── session.py                # AsyncEngine, async_sessionmaker
│   │   ├── base_dao.py              # BaseAsyncDAO<T>
│   │   ├── daos/                    # Concrete async DAOs (23 classes across 15 modules + 1 mixin)
│   │   │   ├── annotation.py        # AsyncAnnotationDAO, AsyncAnnotationLayerDAO
│   │   │   ├── chart.py             # AsyncChartDAO
│   │   │   ├── css.py               # AsyncCssTemplateDAO
│   │   │   ├── dashboard.py         # AsyncDashboardDAO, AsyncEmbeddedDashboardDAO
│   │   │   ├── database.py          # AsyncDatabaseDAO, AsyncSSHTunnelDAO, AsyncDatabaseUserOAuth2TokensDAO
│   │   │   ├── dataset.py           # AsyncDatasetDAO, AsyncDatasetColumnDAO, AsyncDatasetMetricDAO
│   │   │   ├── datasource.py        # AsyncDatasourceDAO
│   │   │   ├── favorites_mixin.py   # FavoriteMixin for favorites support
│   │   │   ├── key_value.py         # AsyncKeyValueDAO
│   │   │   ├── log.py               # AsyncLogDAO
│   │   │   ├── query.py             # AsyncQueryDAO, AsyncSavedQueryDAO
│   │   │   ├── report.py            # AsyncReportScheduleDAO, AsyncReportExecutionLogDAO
│   │   │   ├── security.py          # AsyncSecurityDAO
│   │   │   ├── tag.py               # AsyncTagDAO
│   │   │   ├── theme.py             # AsyncThemeDAO
│   │   │   └── user.py              # AsyncUserDAO
│   │   └── engine_specs/            # Async DB adapters
│   │       ├── base.py              # BaseAsyncEngineSpec
│   │       ├── postgres.py          # asyncpg
│   │       ├── mysql.py             # asyncmy
│   │       ├── clickhouse.py        # asynch
│   │       ├── trino.py             # aiotrino
│   │       └── sync_fallback.py     # run_in_executor wrapper (default for 40+ unsupported drivers)
│   ├── commands/
│   │   └── base.py                   # AsyncBaseCommand
│   ├── security/                     # SecurityManager reimplementation
│   │   ├── manager.py               # AsyncSecurityManager (replaces FAB SecurityManager)
│   │   ├── permissions.py           # Permission constants and helpers
│   │   └── session_decoder.py       # Flask/itsdangerous cookie session decoder
│   ├── controllers/                  # 37 controllers (1:1 with Flask API + legacy compat)
│   │   ├── advanced_data_type.py
│   │   ├── annotation_layer.py
│   │   ├── annotation.py
│   │   ├── async_event.py
│   │   ├── available_domains.py
│   │   ├── cache.py
│   │   ├── chart.py
│   │   ├── chart_data.py
│   │   ├── css_template.py
│   │   ├── dashboard.py
│   │   ├── dashboard_filter_state.py
│   │   ├── dashboard_permalink.py
│   │   ├── database.py
│   │   ├── dataset.py
│   │   ├── dataset_columns.py
│   │   ├── dataset_metric.py
│   │   ├── datasource.py
│   │   ├── embedded_dashboard.py
│   │   ├── explore.py
│   │   ├── explore_form_data.py
│   │   ├── explore_permalink.py
│   │   ├── import_export.py
│   │   ├── legacy_api.py            # Deprecated /v1/query/, /v1/form_data/, /v1/time_range/
│   │   ├── log.py
│   │   ├── query.py
│   │   ├── report.py
│   │   ├── report_log.py
│   │   ├── rls.py
│   │   ├── role.py
│   │   ├── saved_query.py
│   │   ├── security.py
│   │   ├── sqllab.py
│   │   ├── sqllab_permalink.py
│   │   ├── tag.py
│   │   ├── temporary_cache.py       # Base controller for filter state / form data
│   │   ├── theme.py
│   │   ├── user.py
│   │   ├── user_me.py
│   │   └── spa.py                   # SPA HTML shell routes (/explore, /dashboard, /sqllab)
│   ├── schemas/                      # msgspec Structs (replaces Marshmallow)
│   │   ├── base.py                  # ApiResponse, ApiListResponse, ErrorResponse, SupersetErrorDetail
│   │   ├── chart.py
│   │   ├── dashboard.py
│   │   ├── database.py
│   │   ├── dataset.py
│   │   ├── query.py
│   │   ├── report.py
│   │   ├── tag.py
│   │   ├── security.py
│   │   └── ...
│   ├── common/                       # Async QueryContext (data processing core)
│   │   ├── query_context.py
│   │   ├── query_context_processor.py
│   │   └── query_object.py
│   ├── async_events/                 # Async event manager (Redis streams)
│   │   └── manager.py
│   ├── key_value/                    # Async KV store
│   │   └── manager.py
│   ├── distributed_lock/            # Async distributed locks
│   │   └── lock.py
│   ├── importexport/                # Async import/export
│   │   └── manager.py
│   ├── temporary_cache/             # Base for filter state / form data
│   │   └── base.py
│   ├── thumbnails/                  # Digest + async screenshot trigger
│   │   └── digest.py
│   ├── cache/                       # Async cache layer (replaces flask-caching)
│   │   └── manager.py
│   ├── websocket/                   # WebSocket (replaces superset-websocket)
│   │   └── events.py
│   ├── templates/                   # Jinja2 templates (SPA shell)
│   │   ├── spa.html                 # Adapted from superset/templates/superset/spa.html
│   │   ├── macros.html
│   │   └── partials/
│   │       └── asset_bundle.html
│   ├── cli/                         # CLI commands (click-based)
│   │   ├── main.py                  # liteset_cli group + runserver, init, version
│   │   └── compat.py                # superset_cli backward compat wrapper
│   └── fallback.py                  # WSGI->ASGI Flask mount
├── superset-frontend/               # Frontend (UNTOUCHED)
├── superset-websocket/              # Removed at cleanup stage
├── tests/
│   └── liteset/
│       ├── unit/                    # DAO, Commands, Guards, Middleware
│       ├── integration/             # Every endpoint, API contract checks
│       ├── contract/                # Flask vs Litestar response parity
│       └── load/                    # Locust scenarios
└── docs/
    └── plans/
```

### 2.2 Modules Reused from superset/ (no copy)

- `superset.models.*` — all SQLAlchemy models
- `superset.sql.*` — SQL parsing via sqlglot (no Flask deps)
- `superset.sql_validators.*` — SQL validators
- `superset.connectors.*` — connectors
- `superset.db_engine_specs.*` — engine specs (until async migration)
- `superset.tasks.*` — Celery tasks (kept as-is)
- `superset.translations.*` — i18n
- `superset.migrations.*` — Alembic migrations (shared DB)

### 2.3 Out of Scope

- `superset/examples/` — demo data (dev-only utility, not reused by liteset)

---

## 3. Component Architecture

### 3.1 App Factory (liteset/app.py)

```python
from litestar import Litestar, get
from litestar.config.compression import CompressionConfig
from litestar.config.cors import CORSConfig
from litestar.openapi import OpenAPIConfig

def create_app(
    settings: LitesetSettings | None = None,
    enable_flask_fallback: bool = True,
) -> Litestar:
    if settings is None:
        settings = LitesetSettings()

    route_handlers = [health_check, SPAController]
    startup_hooks = [on_startup]

    # Flask fallback — conditional, gracefully skipped if flask not installed
    if enable_flask_fallback:
        try:
            from liteset.fallback import create_flask_fallback, init_flask_fallback
            route_handlers.append(create_flask_fallback())
            startup_hooks.append(init_flask_fallback)
        except ImportError:
            pass

    # Static file routers for webpack assets and FAB statics
    route_handlers.extend([
        create_static_files_router(path="/static/assets", directories=[...]),
        create_static_files_router(path="/static/appbuilder", directories=[...]),
    ])

    return Litestar(
        route_handlers=route_handlers,
        dependencies={
            "session": Provide(provide_async_session),
            "request_cache": Provide(provide_request_cache, use_cache=True, sync_to_thread=False  # only for sync callables),
            # TODO(liteset/auth): "current_user": Provide(get_current_user),
        },
        # TODO(liteset/auth): middleware=[AuthMiddleware],
        on_startup=startup_hooks,
        on_shutdown=[on_shutdown],  # dispose_engine, close_redis, cancel_websockets (see section 23)
        exception_handlers={
            LitesetException: liteset_exception_handler,
            Exception: generic_exception_handler,
        },
        openapi_config=OpenAPIConfig(
            title="Superset API", version="v1", path="/swagger/v1",
        ),
        cors_config=CORSConfig(allow_origins=settings.cors_allow_origins)
        if settings.cors_allow_origins else None,
        # TODO(liteset/auth): csrf_config=CSRFConfig(secret=settings.secret_key),
        compression_config=CompressionConfig(backend="gzip"),
        template_config=TemplateConfig(
            directory=Path(__file__).parent / "templates",
            engine=JinjaTemplateEngine,
            engine_callback=_register_template_globals,  # manifest.json lookups
        ),
        state=State({"settings": settings}),
    )
```

> **Note:** `on_startup` creates the DB engine and stores it in `app.state`.
> Webpack `manifest.json` is loaded at app creation for Jinja2 template callable
> registration (`js_manifest`, `css_manifest`, `assets_prefix`).

### 3.2 Configuration (liteset/config.py)

```python
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Prefix-based sync->async driver mapping (covers all common variants).
# URIs already using async drivers (e.g., "postgresql+asyncpg://") do not
# match any prefix below and are returned unchanged via the fallback `return v`.
# Other dialects (e.g., "mssql://", "oracle://") are also left as-is and
# handled by SyncFallbackEngineSpec at runtime (see section 19).
_SYNC_TO_ASYNC_DRIVERS = {
    "postgresql://": "postgresql+asyncpg://",
    "postgresql+psycopg2://": "postgresql+asyncpg://",
    "postgresql+pg8000://": "postgresql+asyncpg://",
    "mysql://": "mysql+asyncmy://",
    "mysql+pymysql://": "mysql+asyncmy://",
    "mysql+mysqldb://": "mysql+asyncmy://",
    "sqlite://": "sqlite+aiosqlite://",
}

class LitesetSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LITESET_",
        env_file=".env",
        extra="ignore",
    )

    secret_key: Annotated[SecretStr, MinLen(16)]  # min 16 chars, validated
    sqlalchemy_database_uri: str = "sqlite+aiosqlite:///superset.db"
    host: str = "0.0.0.0"
    port: int = 8088
    debug: bool = False
    static_assets_prefix: str = ""
    global_async_queries: bool = False
    cors_allow_origins: list[str] = []
    log_level: str = "INFO"
    production: bool = False
    cache_redis_url: str = ""
    cache_default_ttl: int = 300

    @field_validator("sqlalchemy_database_uri")
    @classmethod
    def convert_to_async_driver(cls, v: str) -> str:
        """Convert sync DB URI to async driver variant via prefix matching.

        Handles all common driver variants:
        - postgresql://, postgresql+psycopg2://, postgresql+pg8000:// -> postgresql+asyncpg://
        - mysql://, mysql+pymysql://, mysql+mysqldb:// -> mysql+asyncmy://
        - sqlite:// -> sqlite+aiosqlite://
        URIs already using async drivers are returned unchanged.
        Other dialects are left as-is (handled by sync_fallback at runtime).
        """
        for sync_prefix, async_prefix in _SYNC_TO_ASYNC_DRIVERS.items():
            if v.startswith(sync_prefix):
                return v.replace(sync_prefix, async_prefix, 1)
        return v

```

> **Note:** Backward compatibility with `superset_config.py` is handled automatically
> via `SupersetConfigSettingsSource` in the Pydantic Settings source chain (see section 11.2).
> No explicit `from_superset_config()` classmethod is needed — `LitesetSettings()` resolves
> values from env vars, `.env`, and `superset_config.py` in the correct priority order.

### 3.3 Data Access Layer (liteset/db/base_dao.py)

```python
from functools import lru_cache

T = TypeVar("T", bound=DeclarativeBase)

class BaseAsyncDAO(Generic[T]):
    model_cls: type[T]

    def __init__(self, session: AsyncSession):
        self.session = session

    # Shared across all subclasses intentionally: keyed by model_cls,
    # so each subclass caches its own PK column without collision.
    _pk_column_cache: dict[type, Any] = {}

    @classmethod
    def _get_pk_column(cls) -> Any:
        """Return the primary key column attribute, cached per model class.

        Uses a shared class-level dict keyed by model_cls — thread-safe
        for single-writer (first access populates, subsequent reads are
        dict lookups). Raises ValueError for composite PKs.
        """
        if cls.model_cls not in cls._pk_column_cache:
            pk_cols = inspect(cls.model_cls).primary_key
            if len(pk_cols) != 1:
                raise ValueError(
                    f"{cls.model_cls.__name__} has composite PK; "
                    "use a custom query instead of find_by_ids"
                )
            cls._pk_column_cache[cls.model_cls] = getattr(
                cls.model_cls, pk_cols[0].name,
            )
        return cls._pk_column_cache[cls.model_cls]

    async def find_by_id(self, model_id: int | str) -> T | None:
        return await self.session.get(self.model_cls, model_id)

    async def find_by_ids(self, model_ids: list[int | str]) -> list[T]:
        pk_col = self._get_pk_column()
        stmt = select(self.model_cls).where(pk_col.in_(model_ids))
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def find_all(self, filters: list[Any] | None = None) -> list[T]:
        stmt = select(self.model_cls)
        if filters:
            stmt = stmt.where(*filters)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def find_one_or_none(self, **filter_by: Any) -> T | None:
        stmt = select(self.model_cls).filter_by(**filter_by)
        result = await self.session.execute(stmt)
        return result.scalars().one_or_none()

    async def create(self, attributes: dict[str, Any]) -> T:
        """Create a new model instance and add to session.

        Does not flush or commit — caller (command/controller) controls
        persistence timing. Transaction middleware commits on success.
        """
        item = self.model_cls(**attributes)
        self.session.add(item)
        return item

    async def update(self, item: T, attributes: dict[str, Any]) -> T:
        """Update model attributes in-place.

        Does not flush or commit — changes are tracked by the session
        and persisted on next flush/commit.
        """
        for key, value in attributes.items():
            setattr(item, key, value)
        return item

    async def delete(self, items: list[T]) -> None:
        """Delete one or a few already-loaded instances."""
        for item in items:
            await self.session.delete(item)

    async def bulk_delete(self, ids: list[int | str]) -> int:
        """Bulk delete by IDs in a single SQL DELETE. Returns deleted count.

        WARNING: Bypasses ORM-level cascades (cascade="all, delete-orphan").
        Use delete() for models with ORM cascades. Override in subclasses
        when cascade behavior is required.
        """
        if not ids:
            return 0
        pk_col = self._get_pk_column()
        stmt = sa_delete(self.model_cls).where(pk_col.in_(ids))
        result = await self.session.execute(stmt)
        return result.rowcount
```

Key differences from Flask Superset DAO:
- Session injected via constructor (Litestar DI), not from global `db.session`
- PK column introspected and cached via `@lru_cache` (thread-safe, supports UUID/int PKs)
- `create()` / `update()` do NOT flush — transaction middleware handles commit
- `find_by_ids()` for batch lookups by PK list

### 3.4 Command Layer (liteset/commands/base.py)

```python
class AsyncBaseCommand(ABC, Generic[T]):
    @abstractmethod
    async def validate(self) -> None: ...

    @abstractmethod
    async def run(self) -> T: ...

    async def execute(self) -> T:
        await self.validate()
        return await self.run()
```

Commands receive DAO via DI from the controller:

```python
class CreateChartCommand(AsyncBaseCommand[Slice]):
    def __init__(self, data: dict, dao: ChartDAO, current_user: User):
        self.data = data
        self.dao = dao
        self.current_user = current_user

    async def validate(self) -> None: ...

    async def run(self) -> Slice:
        self.data["last_saved_by"] = self.current_user
        return await self.dao.create(self.data)
```

### 3.5 Controller Layer (example: liteset/controllers/chart.py)

DAOs are registered as Litestar dependencies (not created manually in handlers).
This enables proper DI, testability via overrides, and keeps controllers thin.

```python
from litestar import Controller, get, post, put, delete
from litestar.params import Parameter

class ChartController(Controller):
    path = "/api/v1/chart"
    tags = ["Charts"]
    dependencies = {"dao": Provide(ChartDAO)}

    @get("/")
    async def get_list(
        self,
        dao: ChartDAO,
        current_user: User,
        page: int = Parameter(default=0),
        page_size: int = Parameter(default=25),
    ) -> ChartListResponse:
        items = await dao.find_all(...)
        return ChartListResponse(result=items, count=len(items))

    @post("/")
    async def create(
        self,
        data: ChartPostSchema,
        dao: ChartDAO,
        current_user: User,
    ) -> ChartResponse:
        cmd = CreateChartCommand(data, dao, current_user)
        chart = await cmd.execute()
        return ChartResponse(result=chart)

    @post("/data")
    async def chart_data(
        self,
        data: ChartDataRequestSchema,
        session: AsyncSession,
        current_user: User,
    ) -> ChartDataResponse:
        cmd = ChartDataCommand(data, session, current_user)
        result = await cmd.execute()
        return ChartDataResponse(result=result)
```

> **Note:** `session.commit()` is handled by the transaction middleware (see section 3.10),
> not called explicitly in controllers.

### 3.6 Auth & Security

Full reimplementation of Flask-AppBuilder SecurityManager as async-native module.
Works with the same FAB database tables (`ab_user`, `ab_role`, `ab_permission_view`, `ab_permission`, `ab_view_menu`, `ab_user_role`) — existing Superset databases connect without migration.

```python
# liteset/middleware/auth.py
class AuthMiddleware(AbstractAuthenticationMiddleware):
    """Authentication middleware — runs BEFORE Litestar DI resolution.

    Because middleware executes before dependency injection, the session
    cannot be injected via Provide(). Instead, AuthMiddleware creates its
    own short-lived AsyncSession from the session_factory stored in
    app.state (set during on_startup). This session is used only for
    user lookup and is closed immediately after authentication completes.

    The controller-level AsyncSession (from provide_async_session DI)
    is a separate instance scoped to the request lifecycle.

    Performance optimization: To reduce connection pool pressure (each request
    opens TWO sessions — one for auth, one for the handler), resolved users are
    cached in Redis with a short TTL (60s). Cache key: `auth:user:{identifier}`.
    On cache hit, the middleware skips the DB session entirely. On cache miss,
    it creates a short-lived session, resolves the user, and populates the cache.
    Cache is invalidated on password change, role change, or logout.
    """
    _USER_CACHE_TTL = 60  # seconds

    async def authenticate_request(self, connection) -> AuthenticationResult:
        redis: Redis = connection.app.state.redis
        identifier = self._extract_identifier(connection)  # cookie/JWT/API-key

        # Try Redis user cache first to avoid opening a DB session.
        # If Redis is unavailable, fall back to DB lookup gracefully.
        try:
            cached_user = await self._get_cached_user(redis, identifier)
            if cached_user is not None:
                return AuthenticationResult(user=cached_user, auth=None)
        except (ConnectionError, TimeoutError):
            pass  # Redis down — fall through to DB lookup

        # Cache miss (or Redis unavailable) — resolve via DB
        session_factory = connection.app.state.session_factory
        async with session_factory() as session:
            security_manager = AsyncSecurityManager(session)
            # 1. Cookie session (decode itsdangerous signed cookie for FAB compat)
            # 2. JWT Bearer token
            # 3. API key (X-API-Key header or ?api_key= query param)
            user = await security_manager.resolve_user(connection)
            if user is None:
                raise NotAuthorizedException()

            # Cache resolved user for subsequent requests (best-effort)
            try:
                await self._cache_user(redis, identifier, user)
            except (ConnectionError, TimeoutError):
                pass  # Redis down — auth works, just without cache

            return AuthenticationResult(user=user, auth=None)

# liteset/security/manager.py
class AsyncSecurityManager:
    """Full reimplementation of FAB SecurityManager via async SQLAlchemy queries.
    Reads from the same ab_* tables — zero DB migration needed.

    Note: This class is used in two contexts:
    1. AuthMiddleware — with a short-lived session created by middleware itself
    2. Controllers/Guards — with the request-scoped session from DI
    Both are valid; the session lifecycle is managed by the caller.
    """

    def __init__(self, session: AsyncSession):
        self.session = session

    async def has_access(self, permission_name: str, view_name: str) -> bool:
        """Check if current user has permission on a view/resource."""
        ...

    async def get_user_roles(self, user: User) -> list[Role]:
        """Get all roles for a user (including public role)."""
        ...

    async def raise_for_access(
        self, *, database: Database | None = None,
        datasource: BaseDatasource | None = None,
        query: Query | None = None,
    ) -> None:
        """Raise PermissionDeniedException if user lacks access."""
        ...

    async def can_access(self, permission_name: str, view_name: str) -> bool: ...
    async def can_access_all_databases(self) -> bool: ...
    async def can_access_all_datasources(self) -> bool: ...
    async def get_schemas_accessible_by_user(
        self, database: Database, schemas: list[str],
    ) -> list[str]: ...
    async def get_datasources_accessible_by_user(self) -> list[BaseDatasource]: ...

    async def invalidate_user_cache(self, redis: Redis, user: User) -> None:
        """Invalidate Redis auth cache for a user.

        Called by commands that change user state:
        - Password change (UpdatePasswordCommand)
        - Role assignment/removal (UpdateUserRoleCommand)
        - User deactivation (DeactivateUserCommand)
        - Explicit logout (LogoutCommand)

        Deletes all possible cache keys for the user:
        - auth:user:<user_id> (cookie/JWT identifier)
        - auth:user:<username> (API key identifier)
        - auth:user:<email> (alternative identifier)

        This is sufficient for single- and multi-instance deployments
        because Redis is the shared cache — deleting the key in Redis
        makes all instances miss on next request and re-resolve from DB.
        """
        keys = [
            f"auth:user:{user.id}",
            f"auth:user:{user.username}",
            f"auth:user:{user.email}",
        ]
        await redis.delete(*keys)

# liteset/security/session_decoder.py
class FlaskSessionDecoder:
    """Decodes itsdangerous-signed Flask session cookies.
    Provides backward compatibility during Strangler Fig coexistence."""
    ...

# liteset/guards/rbac.py
def require_permission(*permissions: str) -> Guard:
    async def guard(connection, handler) -> None:
        user = connection.user
        if not has_permissions(user, permissions):
            raise PermissionDeniedException()
    return guard

# Usage:
@get("/", guards=[require_permission("can_read", "Chart")])
async def get_list(self, ...): ...
```

### 3.7 WebSocket (replaces superset-websocket)

**Authentication:** Litestar's `AbstractAuthenticationMiddleware` handles HTTP requests only.
WebSocket connections authenticate via a JWT token passed as a query parameter during the
handshake (`ws://host/ws/events?token=<jwt>`). The handler validates the token before
accepting the connection.

**Origin validation:** Since CORS headers do not apply to WebSocket upgrade requests,
the handler explicitly validates the `Origin` header against `settings.cors_allow_origins`
before accepting the connection. This prevents cross-site WebSocket hijacking (CSWSH).

```python
# liteset/websocket/events.py
from litestar import Controller, websocket
from litestar.handlers import WebsocketListener

class AsyncQueryWebSocket(Controller):
    path = "/ws"

    @websocket("/events")
    async def on_event(self, socket: WebSocket, state: State) -> None:
        """Server-push WebSocket: subscribes to Redis channel and forwards events.

        Uses @websocket() (not @websocket_listener) because this is a
        server-initiated push pattern — the server continuously sends messages
        from Redis pub/sub without waiting for client data each iteration.

        Auth: JWT token is passed via query parameter during WS handshake.
        This is the standard approach for WebSocket auth since browsers
        do not support custom headers in the WebSocket constructor.
        Cookie-based auth is also supported as a fallback (same session
        cookie sent automatically by the browser during WS handshake).
        """
        # Origin validation (CORS doesn't apply to WebSocket upgrade)
        origin = socket.headers.get("origin", "")
        allowed_origins = state.settings.cors_allow_origins
        if allowed_origins and origin not in allowed_origins:
            await socket.close(code=4403)  # Custom close code: forbidden origin
            return

        # Authenticate before accepting
        user = await authenticate_websocket(socket)  # JWT query param or cookie
        if user is None:
            await socket.close(code=4401)  # Custom close code: unauthorized
            return

        # Enforce per-server WebSocket connection limit to prevent resource exhaustion.
        # A single user can open at most MAX_WS_PER_USER connections.
        MAX_WS_PER_USER = 10
        active_ws: dict[WebSocket, int] = state.active_websockets  # {socket: user_id}
        user_ws_count = sum(1 for uid in active_ws.values() if uid == user.id)
        if user_ws_count >= MAX_WS_PER_USER:
            await socket.close(code=4429)  # Custom close code: too many connections
            return

        await socket.accept()
        active_ws[socket] = user.id
        channel = f"events:{user.id}"
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._relay_events(socket, channel))
                tg.create_task(self._heartbeat(socket))
        except* (WebSocketDisconnect, ConnectionError):
            pass  # client disconnected — clean exit
        finally:
            active_ws.pop(socket, None)
            await socket.close()

    @staticmethod
    async def _relay_events(socket: WebSocket, channel: str) -> None:
        """Subscribe to Redis and forward events to WebSocket.

        Handles Redis reconnection: if the pub/sub connection drops,
        re-subscribes automatically. Uses an asyncio.Queue as a send
        buffer to apply backpressure — if the client is too slow,
        drops stale events rather than growing the buffer unboundedly.

        Note: Litestar's WebSocket does not expose a send_queue_size
        attribute, so we manage backpressure via an explicit queue.

        Cancellation behavior: _producer and _consumer run in a single
        TaskGroup. If either task raises (e.g., Redis connection lost
        permanently, or WebSocket send fails), the TaskGroup cancels the
        sibling task. The exception then propagates to the outer TaskGroup
        in on_event(), which cancels _heartbeat as well — ensuring full
        cleanup on any failure path.
        """
        send_queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=64)

        async def _producer() -> None:
            """Read from Redis pub/sub and put into send_queue."""
            while True:
                try:
                    pubsub = redis.pubsub()
                    await pubsub.subscribe(channel)
                    async for message in pubsub.listen():
                        if message["type"] != "message":
                            continue
                        if send_queue.full():
                            try:
                                send_queue.get_nowait()  # drop oldest stale event
                            except asyncio.QueueEmpty:
                                pass
                        await send_queue.put(message["data"])
                except redis.ConnectionError:
                    await asyncio.sleep(1)  # reconnect backoff
                    continue  # re-subscribe

        async def _consumer() -> None:
            """Drain send_queue and forward to WebSocket."""
            while True:
                message = await send_queue.get()
                await socket.send_json(message)

        async with asyncio.TaskGroup() as tg:
            tg.create_task(_producer())
            tg.create_task(_consumer())

    @staticmethod
    async def _heartbeat(socket: WebSocket) -> None:
        """Send periodic application-level heartbeat to detect dead connections.

        Note: Litestar/ASGI does not expose RFC 6455 ping/pong frames
        directly. Instead, we send an application-level heartbeat message
        that the client can use to detect connection liveness. If send_json
        raises (client disconnected), the exception propagates to TaskGroup
        which cancels all sibling tasks (relay + heartbeat).
        """
        while True:
            await asyncio.sleep(30)
            await socket.send_json({"type": "ping", "timestamp": time.time()})
```

### 3.8 Flask Fallback (Strangler Fig)

```python
# liteset/fallback.py
from asgiref.wsgi import WsgiToAsgi
from litestar import asgi, Litestar
from superset.app import create_app as create_flask_app

async def init_flask_fallback(app: Litestar) -> None:
    """Initialize Flask ASGI wrapper during Litestar on_startup.

    Eager initialization avoids race conditions that would occur with
    lazy init via a global variable (multiple coroutines hitting the
    fallback simultaneously on the first request).
    """
    app.state.flask_asgi = WsgiToAsgi(create_flask_app())

def create_flask_fallback():
    @asgi("/", is_mount=True, copy_scope=True)
    async def flask_fallback(scope, receive, send):
        from litestar import Litestar
        app: Litestar = scope["app"]
        await app.state.flask_asgi(scope, receive, send)
    return flask_fallback
```

> **Note:** `init_flask_fallback` is registered in `on_startup` (see section 3.1).

### 3.9 Exception Handling

Superset has a custom exception hierarchy (`SupersetException`, `SupersetSecurityException`,
`SupersetErrorException`, `SupersetTimeoutException`, etc.) with a specific JSON error format
that the frontend depends on. Liteset maps these to Litestar exception handlers:

```python
# liteset/exceptions.py — SIP-40 compatible exception hierarchy
from litestar import MediaType, Request, Response

class LitesetException(Exception):
    """Base exception for all Liteset errors."""
    status_code: int = 500
    message: str = "An unexpected error occurred"

    def __init__(self, message="", exception=None, error_type=None, extra=None):
        if message:
            self.message = message
        self.extra: dict = extra or {}
        self._exception = exception
        self._error_type = error_type
        super().__init__(self.message)

    @property
    def error_type(self) -> str:
        return self._error_type or type(self).__name__

    def to_sip40(self) -> dict:
        return {
            "message": self.message,
            "error_type": self.error_type,
            "level": "error",
            "extra": self.extra,
        }

# --- Core exceptions ---

class LitesetSecurityException(LitesetException):
    status_code = 403
    message = "Access denied"

class LitesetValidationException(LitesetException):
    status_code = 422
    message = "Validation error"

class LitesetNotFoundError(LitesetException):
    status_code = 404
    message = "Resource not found"

class LitesetTimeoutException(LitesetException):
    status_code = 504
    message = "Request timed out"

# --- Command-layer exceptions ---

class CommandException(LitesetException):
    """Base for command-layer errors."""
    status_code = 500

class CommandInvalidError(CommandException):
    status_code = 422

class ObjectNotFoundError(CommandException):
    status_code = 404

class ForbiddenError(CommandException):
    status_code = 403

class CreateFailedError(CommandException):
    status_code = 500

class UpdateFailedError(CommandException):
    status_code = 500

class DeleteFailedError(CommandException):
    status_code = 500

class ImportFailedError(CommandException):
    status_code = 500
```

Exception handlers are defined in the same file (`liteset/exceptions.py`):

```python
# liteset/exceptions.py (continued)

def liteset_exception_handler(request: Request, exc: LitesetException) -> Response:
    """SIP-40 compatible error response handler.

    Response format must match Flask Superset for frontend compatibility:
    {
        "errors": [{"message": str, "error_type": str, "level": str, "extra": dict}],
        "message": str  # legacy field, kept for backward compat
    }
    """
    body = ErrorResponse(
        errors=[SupersetErrorDetail(**exc.to_sip40())],
        message=exc.message,
    )
    return Response(content=body, status_code=exc.status_code, media_type=MediaType.JSON)

def generic_exception_handler(request: Request, exc: Exception) -> Response:
    """Catch-all for unhandled exceptions.

    Preserves status_code from Litestar HTTP exceptions (404, 405, etc.)
    while wrapping them in SIP-40 format. Masks 5xx internals in response
    but logs full traceback for diagnostics.
    """
    # Note: logger is defined at module level:
    # logger = logging.getLogger(__name__)

    # Preserve status code from Litestar/HTTP exceptions (404, 405, 422, etc.)
    status_code = getattr(exc, "status_code", 500)

    # Log full traceback for server errors (diagnostics)
    if status_code >= 500:
        logger.exception("Unhandled server error: %s", exc)

    # Mask internal details in production responses to prevent info leakage
    message = str(exc) if status_code < 500 else "An unexpected error occurred"

    body = ErrorResponse(
        errors=[SupersetErrorDetail(
            message=message,
            error_type=type(exc).__name__,
            level="error",
            extra={},
        )],
        message=message,
    )
    return Response(content=body, status_code=status_code, media_type=MediaType.JSON)

# Registered in app factory:
# exception_handlers={LitesetException: liteset_exception_handler, Exception: generic_exception_handler}
```

Key mapping from Superset exceptions:

| Superset Exception | Liteset Exception | HTTP Code |
|---|---|---|
| `SupersetException` | `LitesetException` | 500 |
| `SupersetSecurityException` | `LitesetSecurityException` | 403 |
| `SupersetErrorException` | `LitesetValidationException` | 422 |
| `SupersetTimeoutException` | `LitesetTimeoutException` | 504 |
| `CommandException` | `CommandException` | 500 |
| `CommandInvalidError` | `CommandInvalidError` | 422 |
| `ObjectNotFoundError` | `ObjectNotFoundError` | 404 |
| `ForbiddenError` | `ForbiddenError` | 403 |
| `CreateFailedError` | `CreateFailedError` | 500 |
| `UpdateFailedError` | `UpdateFailedError` | 500 |
| `DeleteFailedError` | `DeleteFailedError` | 500 |
| `ImportFailedError` | `ImportFailedError` | 500 |

### 3.10 Transaction Management

Controllers do NOT call `session.commit()` or `session.rollback()` directly.
Instead, the `provide_async_session` yield-based dependency manages the full transaction lifecycle:

```python
async def provide_async_session(state: State) -> AsyncGenerator[AsyncSession, None]:
    """Provide an AsyncSession with auto-commit/rollback.

    Session is managed manually (not via async with) to avoid
    double-rollback from the context manager's __aexit__.
    """
    session: AsyncSession = state.session_factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()
```

This ensures:
- No orphaned transactions on unhandled exceptions
- Controllers remain free of transaction boilerplate
- Commands can safely `flush()` without worrying about commit timing

### 3.11 Per-Request State (replaces flask.g)

Flask uses `flask.g` for per-request caching (e.g., caching datasource lists, database access checks within a single request). Liteset replaces this with a `RequestCache` dependency resolved per-request by Litestar's DI container:

```python
# liteset/dependencies.py

def provide_request_cache() -> RequestCache:
    """Per-request cache object, replaces flask.g for memoization.

    Returns a new RequestCache instance each time. Registered with
    `use_cache=True` and `sync_to_thread=False  # only for sync callables` to ensure:
    1. A single RequestCache instance per request (Litestar memoizes the
       dependency result within the request scope when use_cache=True).
    2. No unnecessary thread dispatch for a simple constructor call.
    """
    return RequestCache()

class RequestCache:
    """Lazily populated per-request cache for expensive lookups.

    Thread-safety note: this cache is accessed only from async handlers
    running on the event loop thread. SyncFallbackEngineSpec operations
    run in a dedicated ThreadPoolExecutor and must NOT access RequestCache.
    Any data needed by sync fallback code should be passed as arguments,
    not fetched from the request cache inside the executor.
    """
    _EVENT_LOOP_THREAD_ID: int | None = None  # set on first access in on_startup

    def __init__(self):
        self._store: dict[str, Any] = {}

    async def get_or_set(self, key: str, factory: Callable[[], Awaitable[T]]) -> T:
        # Runtime assertion: detect accidental access from executor threads
        if self._EVENT_LOOP_THREAD_ID is not None:
            import threading
            assert threading.current_thread().ident == self._EVENT_LOOP_THREAD_ID, (
                "RequestCache must not be accessed from executor threads. "
                "Pass data as arguments to sync fallback code instead."
            )
        if key not in self._store:
            self._store[key] = await factory()
        return self._store[key]
```

Registered as a global dependency: `"request_cache": Provide(provide_request_cache, use_cache=True, sync_to_thread=False  # only for sync callables)`.

> **Note:** `RequestCache._EVENT_LOOP_THREAD_ID` is set during `on_startup` via
> `threading.current_thread().ident`. This enables the runtime assertion in
> `get_or_set()` that detects accidental access from `SyncFallbackEngineSpec`
> executor threads — a common mistake that would cause silent race conditions.

Additional per-request helpers in `dependencies.py`:

```python
def get_current_user(request: Request) -> Any:
    """Extract current user from request (set by AuthMiddleware)."""
    return getattr(request, "user", None)

def get_user_id(request: Request) -> int | None: ...
def get_username(request: Request) -> str | None: ...
```

### 3.12 Request Flow (TO-BE)

```
Client HTTP Request
    |
    v
Uvicorn + uvloop (event loop)
    |
    v
Litestar Router
    +-- Migrated route? -> Litestar Controller
    |       |
    |       +-- AuthMiddleware -> User in scope
    |       +-- RBAC Guard -> permission check
    |       +-- msgspec -> body deserialization
    |       +-- DI -> AsyncSession, User
    |       +-- Command.execute()
    |       |       +-- validate()
    |       |       +-- DAO (await session.execute)
    |       +-- session auto-commit (via DI teardown, see 3.10)
    |       +-- Exception? -> liteset_exception_handler (SIP-40 JSON)
    |       +-- msgspec -> response serialization
    |
    +-- Not migrated? -> Flask WSGI fallback (via WsgiToAsgi)
```

---

## 4. Git Strategy

### 4.1 Branch Structure (7 sequential phases)

```
main
 |
 +-- liteset/infrastructure ---------- merge -> main  [DONE]
 |   +- feat: scaffold liteset/ package with app factory
 |   +- feat: add Pydantic Settings config with superset_config.py compat
 |   +- feat: async SQLAlchemy engine and session management
 |   +- feat: WSGI-to-ASGI Flask fallback mount
 |   +- feat: auth middleware scaffold (cookie/JWT/API-key stubs)
 |   +- feat: RBAC guards system
 |   +- feat: CSRF, CORS, security headers middleware
 |   +- feat: BaseAsyncDAO with generic CRUD
 |   +- feat: AsyncBaseCommand pattern
 |   +- feat: msgspec schema base utilities
 |   +- feat: static files config (webpack assets + appbuilder statics)
 |   +- feat: Jinja2 SPA template rendering (spa.html, macros, partials)
 |   +- feat: SPA controller for frontend routes (/explore, /dashboard, /sqllab)
 |   +- feat: liteset CLI with runserver, init, version, db commands
 |   +- feat: superset CLI backward compatibility wrapper
 |   +- test: infrastructure unit tests
 |
 +-- liteset/data-layer --------------- merge -> main  [DONE]
 |   +- feat: async engine specs base class
 |   +- feat: async engine spec for PostgreSQL (asyncpg)
 |   +- feat: async engine spec for MySQL (asyncmy)
 |   +- feat: async engine spec for ClickHouse (asynch)
 |   +- feat: async engine spec for Trino (aiotrino)
 |   +- feat: fallback sync-in-async wrapper for unsupported drivers (40+ DBs)
 |   +- feat: async DAO for charts, dashboards (incl. embedded), datasets (incl. columns, metrics)
 |   +- feat: async DAO for databases (incl. SSH tunnel, OAuth2 tokens), queries, saved_queries
 |   +- feat: async DAO for reports (incl. execution logs), annotations (incl. layers), tags
 |   +- feat: async DAO for remaining models (security, users, key_value, theme, log, css, datasource)
 |   +- feat: FavoriteMixin for cross-DAO favorites support
 |   +- test: DAO unit tests with async fixtures
 |
 +-- liteset/auth --------------------- merge -> main
 |   +- feat: AsyncSecurityManager — full reimplementation of FAB SecurityManager
 |   +- feat: has_access(), get_user_roles(), raise_for_access() via async queries
 |   +- feat: can_access_all_databases(), get_schemas_accessible_by_user()
 |   +- feat: get_datasources_accessible_by_user(), dataset/query access checks
 |   +- feat: FlaskSessionDecoder — itsdangerous cookie session decoding
 |   +- feat: AuthMiddleware — cookie session, JWT Bearer, API key authentication
 |   +- feat: current_user DI dependency (Provide)
 |   +- feat: CSRF config activation
 |   +- feat: permission constants and helpers (liteset/security/permissions.py)
 |   +- feat: full backward compat with FAB tables (ab_user, ab_role, ab_permission_view)
 |   +- test: unit tests for SecurityManager, AuthMiddleware, session decoder
 |   +- test: integration tests for auth flows (login, permission checks)
 |
 +-- liteset/core-api ----------------- merge -> main
 |   |
 |   |  Sub-phase A: Common data processing layer
 |   +- feat: async QueryContext facade
 |   +- feat: async QueryContextProcessor (~1500 LOC migration)
 |   +- feat: async QueryObject
 |   +- feat: async import/export base framework (used by chart, dashboard, database, dataset)
 |   +- test: unit tests for QueryContext and import/export
 |   |
 |   |  Sub-phase B: Core controllers + schemas + commands
 |   +- feat: chart controller with full API parity
 |   +- feat: chart_data controller with full API parity
 |   +- feat: dashboard controller with full API parity
 |   +- feat: dashboard_filter_state and permalink controllers
 |   +- feat: dataset controller with full API parity
 |   +- feat: dataset_columns and dataset_metric controllers
 |   +- feat: database controller with full API parity
 |   +- feat: query and saved_query controllers
 |   +- feat: sqllab and sqllab_permalink controllers
 |   +- feat: msgspec schemas for all core controllers
 |   +- feat: concrete commands (Create/Update/Delete/BulkDelete) for core entities
 |   +- test: integration + contract tests for core API endpoints
 |
 +-- liteset/remaining-api ------------ merge -> main
 |   +- feat: annotation_layer and annotation controllers
 |   +- feat: css_template and theme controllers
 |   +- feat: tag controller
 |   +- feat: report and report_log controllers
 |   +- feat: rls controller
 |   +- feat: role, security, user, user_me controllers
 |   +- feat: explore, explore_form_data, explore_permalink controllers
 |   +- feat: embedded_dashboard, datasource controllers
 |   +- feat: available_domains, advanced_data_type controllers
 |   +- feat: async_event, cache controllers
 |   +- feat: import_export controller (full, uses base from core-api)
 |   +- feat: log controller
 |   +- feat: temporary_cache base controller + integration
 |   +- feat: legacy_api controller (deprecated /v1/query/, /v1/form_data/, /v1/time_range/)
 |   +- feat: async key_value manager
 |   +- feat: async distributed_lock manager
 |   +- feat: async temporary_cache manager
 |   +- feat: async importexport manager (full)
 |   +- feat: thumbnails trigger (Celery task dispatch only)
 |   +- feat: msgspec schemas for all remaining controllers
 |   +- feat: concrete commands for remaining entities
 |   +- test: integration + contract tests for all remaining endpoints
 |
 +-- liteset/websocket ---------------- merge -> main
 |   +- feat: WebSocket handler for async query events (replaces superset-websocket Node.js)
 |   +- feat: Redis pub/sub integration for event streaming
 |   +- feat: async_events manager (Redis streams, supports both polling REST + WebSocket)
 |   +- feat: backward compat — polling REST API preserved for existing frontend
 |   +- test: WebSocket connection and event tests
 |   +- test: polling REST fallback tests
 |
 +-- liteset/cleanup ------------------ merge -> main
     +- refactor: move SQLAlchemy models from superset/ to liteset/
     +- refactor: remove Flask fallback mount
     +- refactor: remove superset/ directory
     +- refactor: remove superset-websocket/ directory
     +- chore: Alembic config — psycopg2 (sync) for migrations
     +- chore: update Docker, CI/CD configs
     +- docs: update deployment documentation
     +- test: full load test suite, final benchmark table
```

### 4.2 Commit Convention

Conventional Commits format: `type(scope): description`

- `feat:` — new functionality
- `test:` — tests only
- `refactor:` — code restructuring
- `chore:` — tooling, deps, configs
- `docs:` — documentation

Each merge to main is a PR with a description of the migration stage.

---

## 5. Testing Strategy

### 5.1 Test Pyramid

```
           /\
          /  \         Load tests (Locust)
         / 5% \        Flask vs Litestar comparison
        /------\
       /        \      Integration tests (pytest + httpx)
      /   20%    \     Every endpoint, API contract checks
     /------------\
    /              \   Unit tests (pytest + anyio)
   /     75%        \  DAO, Commands, Guards, Middleware
  /------------------\
```

### 5.2 pytest-asyncio Configuration

All async tests use `pytest-asyncio` with `auto` mode to avoid decorating every
test function with `@pytest.mark.asyncio`:

```ini
# pyproject.toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
```

This is critical for a project where ~75% of tests are async. Without `auto` mode,
every async test and fixture would require an explicit `@pytest.mark.asyncio` decorator.

### 5.3 Unit Tests

```python
# tests/liteset/unit/test_chart_dao.py
@pytest.fixture
async def async_session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSession(engine) as session:
        yield session

async def test_chart_dao_create(async_session):
    dao = ChartDAO(async_session)
    chart = await dao.create({"slice_name": "Test", "viz_type": "table"})
    assert chart.slice_name == "Test"
```

### 5.4 Integration Tests

```python
# tests/liteset/integration/test_chart_api.py
from litestar.testing import AsyncTestClient

async def test_get_chart_list(client, auth_headers):
    resp = await client.get("/api/v1/chart/", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "result" in data
    assert "count" in data
```

### 5.5 Contract Tests (Flask vs Litestar parity)

```python
# tests/liteset/contract/test_api_parity.py
ENDPOINTS = [
    ("GET", "/api/v1/chart/", None),
    ("GET", "/api/v1/dashboard/", None),
    ("POST", "/api/v1/chart/data", CHART_DATA_PAYLOAD),
    # ... all controllers (see section 6 for full registry)
]

@pytest.mark.parametrize("method,path,body", ENDPOINTS)
async def test_response_schema_parity(flask_client, litestar_client, method, path, body):
    flask_resp = flask_client.open(path, method=method, json=body)
    litestar_resp = await litestar_client.request(method, path, json=body)
    assert flask_resp.status_code == litestar_resp.status_code
    assert set(flask_resp.json.keys()) == set(litestar_resp.json().keys())

    # Deep structural comparison: verify value types and nested object shapes
    # (not exact values — data may differ between instances)
    _assert_schema_match(flask_resp.json, litestar_resp.json())

def _assert_schema_match(flask_data: Any, litestar_data: Any, path: str = "$") -> None:
    """Recursively compare JSON structure: keys, value types, nested shapes.

    Does not compare exact values (data may differ), but ensures the response
    schema is structurally identical — same keys, same types, same nesting.
    """
    if isinstance(flask_data, dict) and isinstance(litestar_data, dict):
        assert set(flask_data.keys()) == set(litestar_data.keys()), (
            f"Key mismatch at {path}: {set(flask_data.keys())} != {set(litestar_data.keys())}"
        )
        for key in flask_data:
            _assert_schema_match(flask_data[key], litestar_data[key], f"{path}.{key}")
    elif isinstance(flask_data, list) and isinstance(litestar_data, list):
        if flask_data and litestar_data:
            # Compare first element structure (representative sample)
            _assert_schema_match(flask_data[0], litestar_data[0], f"{path}[0]")
    else:
        assert type(flask_data) is type(litestar_data), (
            f"Type mismatch at {path}: {type(flask_data).__name__} != {type(litestar_data).__name__}"
        )
```

### 5.6 Load Testing (Locust)

```python
# tests/liteset/load/locustfile.py
class SupersetUser(HttpUser):
    wait_time = between(0.1, 0.5)

    def on_start(self):
        self.client.post("/api/v1/security/login", json={
            "username": "admin", "password": "admin", "provider": "db",
        })

    @task(10)
    def chart_data(self):
        self.client.post("/api/v1/chart/data", json=CHART_DATA_PAYLOAD)

    @task(5)
    def dashboard_list(self):
        self.client.get("/api/v1/dashboard/")

    @task(5)
    def chart_list(self):
        self.client.get("/api/v1/chart/")

    @task(3)
    def dataset_list(self):
        self.client.get("/api/v1/dataset/")

    @task(1)
    def database_list(self):
        self.client.get("/api/v1/database/")
```

### 5.7 Benchmark Scenarios

| Scenario | Description | Measures |
|---|---|---|
| A. Single user | 1 user, 100 sequential requests | Baseline latency (P50, P95, P99) |
| B. Parallel load | 50 users, 60 seconds | RPS, latency under load |
| C. Scaling | 10->50->100->200 users | Degradation point |
| D. Dashboard simulation | 1 user -> 50 parallel chart/data | Real-world scenario |
| E. Long queries | SQL 5-10 sec, 20 users | Async IO-bound advantage |

### 5.8 Metrics Table

```
+-------------------------+-------------------+--------------------+
| Metric                  | Flask + Gunicorn  | Litestar + Uvicorn |
+-------------------------+-------------------+--------------------+
| RPS (scenario B)        | ~50               | hypothesis: ~150+  |
| P95 latency (ms)        |                   |                    |
| P99 latency (ms)        |                   |                    |
| Memory per worker (MB)  | 50-100            | hypothesis: 20-40  |
| CPU utilization (%)     |                   |                    |
| Max concurrent users    |                   |                    |
| Error rate (%)          |                   |                    |
+-------------------------+-------------------+--------------------+
```

Same conditions: one server, one PostgreSQL DB, same worker count, same data.

> **Important caveat:** The 2-3x RPS improvement is expected primarily for **IO-bound** workloads
> (concurrent DB queries, cache lookups) where async excels. For **CPU-bound** scenarios
> (heavy Python data processing in QueryContext), improvement will be smaller because
> async does not parallelize CPU work. Scenario E (long SQL queries) is designed
> specifically to demonstrate the async IO advantage. Real-world gains depend on the
> ratio of IO-wait to CPU-processing in each endpoint.

### 5.9 Tests per Git Branch

| Branch | Test Types |
|---|---|
| liteset/infrastructure | Unit: middleware scaffold, guards, DI, config, CLI |
| liteset/data-layer | Unit: DAO, engine_specs (async fixtures) |
| liteset/auth | Unit + integration: SecurityManager, AuthMiddleware, session decoder, auth flows |
| liteset/core-api | Unit: QueryContext, import/export; Integration + contract: core endpoints |
| liteset/remaining-api | Integration + contract: all remaining endpoints |
| liteset/websocket | WebSocket connection, event streaming, polling fallback |
| liteset/cleanup | Full load test suite, final benchmark table |

---

## 6. Complete API Controller Registry (37 controllers)

### 6.1 Core API Controllers (Phase: liteset/core-api)

| # | Flask Class | resource_name | Litestar Controller File |
|---|---|---|---|
| 1 | ChartRestApi | chart | chart.py |
| 2 | ChartDataRestApi | chart | chart_data.py |
| 3 | DashboardRestApi | dashboard | dashboard.py |
| 4 | DashboardFilterStateRestApi | dashboard | dashboard_filter_state.py |
| 5 | DashboardPermalinkRestApi | dashboard | dashboard_permalink.py |
| 6 | DatabaseRestApi | database | database.py |
| 7 | DatasetRestApi | dataset | dataset.py |
| 8 | DatasetColumnsRestApi | dataset | dataset_columns.py |
| 9 | DatasetMetricRestApi | dataset | dataset_metric.py |
| 10 | QueryRestApi | query | query.py |
| 11 | SavedQueryRestApi | saved_query | saved_query.py |
| 12 | SqlLabRestApi | sqllab | sqllab.py |
| 13 | SqlLabPermalinkRestApi | sqllab | sqllab_permalink.py |

### 6.2 Remaining API Controllers (Phase: liteset/remaining-api)

| # | Flask Class | resource_name | Litestar Controller File |
|---|---|---|---|
| 14 | AdvancedDataTypeRestApi | advanced_data_type | advanced_data_type.py |
| 15 | AnnotationLayerRestApi | annotation_layer | annotation_layer.py |
| 16 | AnnotationRestApi | annotation_layer | annotation.py |
| 17 | AsyncEventsRestApi | async_event | async_event.py |
| 18 | AvailableDomainsRestApi | available_domains | available_domains.py |
| 19 | CacheRestApi | cachekey | cache.py |
| 20 | CssTemplateRestApi | css_template | css_template.py |
| 21 | DatasourceRestApi | datasource | datasource.py |
| 22 | EmbeddedDashboardRestApi | embedded_dashboard | embedded_dashboard.py |
| 23 | ExploreRestApi | explore | explore.py |
| 24 | ExploreFormDataRestApi | explore | explore_form_data.py |
| 25 | ExplorePermalinkRestApi | explore | explore_permalink.py |
| 26 | ImportExportRestApi | assets | import_export.py |
| 27 | ReportScheduleRestApi | report | report.py |
| 28 | ReportExecutionLogRestApi | report | report_log.py |
| 29 | RLSRestApi | rls | rls.py |
| 30 | RoleRestAPI | role | role.py |
| 31 | SecurityRestApi | security | security.py |
| 32 | UserRegistrationsRestAPI | user_registrations | user.py |
| 33 | CurrentUserRestApi | me | user_me.py |
| 34 | UserRestApi | user | user.py |
| 35 | TagRestApi | tag | tag.py |
| 36 | ThemeRestApi | theme | theme.py |
| 37 | LogRestApi | log | log.py |

### 6.3 Base & Legacy Controllers

| # | Flask Class | resource_name | Litestar Controller File | Notes |
|---|---|---|---|---|
| 38 | TemporaryCacheRestApi | temporary_cache | temporary_cache.py | Base class for #4, #24 |
| 39 | Api (legacy) | — | legacy_api.py | Deprecated: /v1/query/, /v1/form_data/, /v1/time_range/ |

### 6.4 Non-controller Endpoints

| Endpoint | Implementation | Notes |
|---|---|---|
| GET /health, /healthcheck, /ping | Inline in app.py | Simple health check (matches Flask Superset routes) |
| SPA routes (/explore, /dashboard, etc.) | controllers/spa.py | Jinja2 template rendering |

---

## 7. Risk Mitigation

| Risk | Mitigation |
|---|---|
| Not all DB drivers have async support | sync_fallback.py with run_in_executor; native async specs for top-4 DBs (postgres, mysql, clickhouse, trino); remaining 40+ via sync_fallback; gradual migration to async |
| Flask-AppBuilder security is deeply coupled | Full AsyncSecurityManager reimplementation reading from same FAB tables (ab_user, ab_role, ab_permission_view); dedicated liteset/auth phase |
| Marshmallow schemas have custom post_load/post_dump | Replicate behavior in msgspec enc/dec hooks |
| QueryContext is complex (~1500 LOC) | Dedicated sub-phase in core-api; migrate incrementally, async-wrap sync parts initially |
| superset/config.py has 2100+ lines of config | Full backward compat via `SupersetConfigSettingsSource` in Pydantic Settings source chain — loads entire config module and maps all fields used by liteset (see section 11.2) |
| Celery tasks import from superset/ | Keep superset/ importable during transition |
| Flask session cookies during coexistence | FlaskSessionDecoder reads itsdangerous-signed cookies for Strangler Fig compat |
| Alembic migrations require sync engine | Use psycopg2 (sync driver) for Alembic; async engine only for app runtime |
| Thumbnails depend on Selenium + Celery | Migrate only the trigger (task dispatch); Celery task code stays in superset/tasks/ |
| async_events uses Redis streams + polling | Support both polling REST API (frontend compat) and native WebSocket; superset-websocket Node.js module replaced |
| `flask.g` used for per-request caching | `Request.state` + `RequestCache` dependency (see section 3.11); automatic per-request scoping and GC |
| Rison query params are non-standard format | Custom `rison_query()` dependency using `prison` library (see section 8.4); transparent to controllers |
| Flask-bound logging config not usable in async | Module-level `logging.getLogger(__name__)` (already used by Superset) + `structlog` for structured output (see section 16) |
| Transaction management across async handlers | `provide_async_session` yield-based DI with auto commit/rollback (see section 3.10); no explicit `session.commit()` in controllers |
| Cache thundering herd on popular endpoints | `AsyncCacheManager.get_or_set()` uses Redis `SET NX` lock to ensure only one caller computes per key (see section 18.2) |
| Connection pool exhaustion under load | Configurable pool_size + max_overflow with pre-ping health checks and pool_recycle (see section 21) |
| Celery task imports break when superset/ removed | Phased import migration with transitional compat layer in cleanup phase (see section 25) |
| WebSocket dead connections consuming resources | Heartbeat ping/pong + backpressure for slow clients (see section 3.7) |
| WebSocket resource exhaustion from too many connections | Per-user connection limit (MAX_WS_PER_USER=10) enforced before socket.accept() (see section 3.7) |
| Auth middleware doubles DB pool pressure | Redis user cache (TTL 60s) in AuthMiddleware — cache hit skips DB session entirely; invalidated on password/role change (see section 3.6) |
| Graceful shutdown leaving orphaned connections | on_shutdown hooks drain DB pool, close Redis, close WebSockets within configurable timeout (see section 23) |

---

## 8. Flask Dependency Migration Strategy

### 8.1 Complete Flask Library Inventory (14 packages)

| Flask Library | Imports | What It Provides | Litestar Replacement |
|---|---|---|---|
| **flask-appbuilder** | ~250 occurrences across ~130 files | Model, SQLAInterface, RBAC, @expose, @protect, SecurityManager, User model | Models reused via SQLAlchemy; RBAC via Guards; Controllers replace ModelRestApi |
| **flask** (core) | ~170 | `g`, `request`, `Response`, `current_app` | `Request.state` + `RequestCache` replaces `g` (section 3.11); Litestar Request/Response; `app.state` replaces `current_app` |
| **flask-babel** | ~140 | `lazy_gettext`, `gettext`, `ngettext` | `babel` directly + locale middleware + thin `gettext()`/`lazy_gettext()` wrapper (see section 20) |
| **flask-login** | ~15 | `current_user`, `login_user` | Litestar AuthMiddleware |
| **flask-caching** | moderate | Cache backend | `redis.asyncio` directly |
| **flask-sqlalchemy** | moderate | `db.session`, `db.engine` | SQLAlchemy 2.0 directly, AsyncSession |
| **flask-migrate** | low | Alembic wrapper | Alembic CLI directly |
| **flask-wtf** | low | CSRF | Litestar `CSRFConfig` |
| **flask-cors** | low | CORS | Litestar `CORSConfig` |
| **flask-talisman** | low | Security headers (CSP, HSTS) | Litestar middleware |
| **flask-jwt-extended** | low | JWT tokens | Litestar JWT auth |
| **flask-compress** | low | gzip | Litestar `CompressionConfig` |
| **flask-limiter** | low | Rate limiting | Litestar middleware |
| **flask-session** | low | Server-side sessions | Litestar session backends |

### 8.2 Migration Phases (mapped to git branches)

**Phase 1 — liteset/infrastructure** (framework scaffolding):
- `flask` core -> Litestar Request/Response/State + DI
- `flask-sqlalchemy` -> SQLAlchemy 2.0 AsyncSession directly
- `flask-cors` -> `CORSConfig`
- `flask-wtf` (CSRF) -> `CSRFConfig` (scaffold, activated in auth phase)
- `flask-talisman` -> Security headers middleware
- `flask-compress` -> `CompressionConfig`

**Phase 2 — liteset/data-layer** (async data access):
- No Flask library replacements — focuses on async DAO and engine specs

**Phase 3 — liteset/auth** (security reimplementation):
- `flask-login` -> AuthMiddleware (full implementation)
- `flask-jwt-extended` -> Litestar JWT auth
- `flask-appbuilder` SecurityManager -> AsyncSecurityManager (full reimplementation)
- `flask-appbuilder` User/Role/Permission models -> import as plain SQLAlchemy, query via AsyncSession
- CSRF config activation

**Phase 4 — liteset/core-api** (controller migration):
- `flask-appbuilder` ModelRestApi -> Litestar Controller
- `flask-appbuilder` @expose/@protect -> Litestar @get/@post + Guards
- `flask-appbuilder` SQLAInterface -> direct SQLAlchemy queries in DAO
- `flask-limiter` -> rate limiting middleware

**Phase 5 — liteset/remaining-api** (complete API coverage):
- `flask-babel` -> `babel` directly + locale middleware + `gettext()`/`lazy_gettext()` wrappers (see section 20)
- `flask-session` -> Litestar session backend
- `flask-caching` -> `redis.asyncio` directly

**Phase 6 — liteset/websocket** (real-time events):
- No Flask library replacements — replaces superset-websocket Node.js module

**Phase 7 — liteset/cleanup** (full removal):
- `flask-migrate` -> Alembic CLI directly (psycopg2 sync driver for migrations)
- Remove all Flask dependencies from requirements

### 8.3 Flask-AppBuilder: Detailed Replacement Strategy

```
FAB Component            Litestar Replacement
-----------------------  -----------------------------------------------
Model (declarative)      Reuse as-is (plain SQLAlchemy)
User, Role, Permission   Import from FAB models, query via AsyncSession
SecurityManager          Reimplement key methods:
                         - has_access() -> RBAC Guard
                         - get_user_roles() -> async query
                         - raise_for_access() -> PermissionDeniedException
SQLAInterface            Not needed — DAO works directly with SQLAlchemy
ModelRestApi             Litestar Controller + BaseAsyncDAO
@expose                  @get / @post / @put / @delete
@protect                 Guards
@safe                    Litestar exception handlers
rison                    Custom RisonParameter + query param parser (see 8.4)
```

### 8.4 Rison Query Parameter Migration

Superset uses [Rison](https://github.com/Nanonid/rison) encoding for complex query parameters (`filters`, `columns`, `order_column`, etc.) passed as `?q=rison_encoded_string`. This is a non-standard format that requires explicit handling.

**Strategy:**

```python
# liteset/params/rison.py
import prison  # rison decoder library (already a Superset dependency)
from litestar.params import Parameter
from litestar.connection import Request

def rison_query(name: str = "q") -> Any:
    """Decode Rison-encoded query parameter into a Python dict.

    Superset frontend sends filter/sort/pagination as:
      GET /api/v1/chart/?q=(filters:!(...),page:0,page_size:25)

    This decoder is applied as a Litestar dependency or before_request hook.
    """
    async def parse_rison(request: Request) -> dict | None:
        raw = request.query_params.get(name)
        if raw is None:
            return None
        return prison.loads(raw)
    return parse_rison
```

Registered per-controller or globally:
```python
class ChartController(Controller):
    dependencies = {"rison_params": Provide(rison_query())}

    @get("/")
    async def get_list(self, rison_params: dict | None, ...) -> ...:
        filters = rison_params.get("filters", []) if rison_params else []
        ...
```

This preserves full backward compatibility with the existing frontend which sends Rison-encoded query strings.

---

## 9. Static Files and SPA Templates

### 9.1 What Gets Served

Superset frontend is a React SPA. Flask currently:
1. Serves `superset/templates/superset/spa.html` — HTML shell with bootstrap_data JSON
2. Serves `superset/static/assets/` — Webpack-built JS/CSS bundles
3. Serves `superset/static/appbuilder/` — FAB statics (flag icons, etc.)

### 9.2 Litestar Static Files Config

```python
from litestar.static_files import create_static_files_router
from litestar.template.config import TemplateConfig
from litestar.contrib.jinja import JinjaTemplateEngine

# Registered as route handlers (not via static_files_config kwarg)
route_handlers.extend([
    create_static_files_router(
        path="/static/assets",
        directories=[_PROJECT_ROOT / "superset" / "static" / "assets"],
        name="static_assets",
    ),
    create_static_files_router(
        path="/static/appbuilder",
        directories=[_PROJECT_ROOT / "superset" / "static" / "appbuilder"],
        name="static_appbuilder",
    ),
])

template_config=TemplateConfig(
    directory=Path(__file__).parent / "templates",
    engine=JinjaTemplateEngine,
    engine_callback=_register_template_globals,  # manifest.json lookups
)
```

### 9.3 SPA Controller

```python
# liteset/controllers/spa.py
from litestar import Controller, get
from litestar.datastructures import State
from litestar.exceptions import NotFoundException
from litestar.response import Template

# First path segments of all known SPA routes from Superset frontend router.
# Used as O(1) frozenset lookup to prevent SPA handler from catching
# API and static file routes.
#
# UPDATE STRATEGY: This set is derived from the React Router config in
# superset-frontend/src/views/routes.tsx. When adding new frontend routes:
# 1. Add the first path segment to this frozenset
# 2. No backend code changes needed beyond this list
# 3. If the frontend router changes are frequent, consider loading these
#    from a shared JSON file (e.g., superset-frontend/spa-routes.json)
#    that both React Router and this controller can reference.
# First path segments of all known SPA routes from Superset frontend router.
# These are registered as EXPLICIT route paths (not a catch-all) to ensure
# that un-matched paths (e.g., un-migrated /api/v1/* endpoints) fall through
# to the Flask ASGI fallback mount during Strangler Fig coexistence.
#
# IMPORTANT: A catch-all /{path:path} MUST NOT be used here because:
# 1. Litestar matches it for ALL paths, preventing Flask fallback from
#    receiving un-migrated API requests.
# 2. NotFoundException from the catch-all handler does NOT cause Litestar
#    to pass the request to the ASGI mount — it returns 404 via the
#    exception handler instead.
#
# UPDATE STRATEGY: This list is derived from the React Router config in
# superset-frontend/src/views/routes.tsx. When adding new frontend routes:
# 1. Add the prefix to SPA_ROUTE_PREFIXES and _SPA_PATHS
# 2. No backend code changes needed beyond this list
SPA_ROUTE_PREFIXES: frozenset[str] = frozenset({
    "explore", "dashboard", "superset", "chart",
    "alert", "report", "database", "dataset",
    "savedquery", "csstemplate", "annotationlayer",
    "rowlevelsecurity", "users", "roles", "logmodelview",
})

# Explicit route paths for Litestar registration.
# Each prefix gets a /{prefix}/{path:path} route to handle sub-paths.
_SPA_PATHS: list[str] = ["/"] + [
    f"/{prefix}/{{path:path}}" for prefix in SPA_ROUTE_PREFIXES
] + [
    f"/{prefix}" for prefix in SPA_ROUTE_PREFIXES
]

class SPAController(Controller):
    """Renders SPA HTML shell for all frontend routes.

    Uses explicit prefix-based routes instead of a catch-all to ensure
    un-matched paths (API, static, un-migrated endpoints) are NOT
    intercepted and can reach the Flask ASGI fallback mount.
    """
    path = "/"

    @get(
        _SPA_PATHS,
        exclude_from_auth=True,  # SPA shell does not require auth
    )
    async def spa_page(self, state: State, path: str = "") -> Template:

        settings = state.settings
        return Template(
            template_name="spa.html",
            context={
                "bootstrap_data": "{}",
                "entry": "spa",
                "title": "Superset",
                "assets_prefix": settings.static_assets_prefix,
                "standalone_mode": False,
                "favicons": [{"href": "/static/assets/images/favicon.png"}],
                "csrf_token": "",
            },
        )
```

> **Route resolution and Flask fallback:** SPA routes are registered as explicit prefix paths
> (`/explore/{path:path}`, `/dashboard/{path:path}`, etc.) — NOT a catch-all `/{path:path}`.
> This is critical for Strangler Fig coexistence: any request that does not match a Litestar
> route handler (migrated API controller, SPA prefix, or static file) falls through to the
> Flask ASGI mount at `/` (registered with `is_mount=True`). During migration, un-migrated
> API endpoints are served by Flask because no Litestar route matches them. As controllers
> are migrated to Litestar, they take priority over the Flask mount automatically.
>
> **Auth exclusion:** SPA handler uses `exclude_from_auth=True` because the HTML shell
> itself does not require authentication — the React SPA handles auth flows client-side.

### 9.4 Template Migration

Templates are adapted from `superset/templates/superset/` with minimal changes:
- Replace `{{ appbuilder.app.config['KEY'] }}` with `{{ settings.KEY }}`
- Replace `{{ csrf_token() }}` with passed context variable
- Keep `asset_bundle.html` macros for JS/CSS bundle loading

---

## 10. CLI Migration

### 10.1 Current State

CLI is built on `click` + Flask `FlaskGroup`. Entry point: `superset=superset.cli.main:superset`.

Commands: `init`, `version`, `db upgrade`, `load_examples`, `import-dashboards`,
`export-dashboards`, `import-datasources`, `export-datasources`, `thumbnails`, etc.

### 10.2 New CLI Architecture

```python
# liteset/cli/main.py
import click
import anyio

@click.group(context_settings={"token_normalize_func": normalize_token})
@click.pass_context
def liteset_cli(ctx):
    """The Liteset CLI (async Superset backend)"""
    ctx.ensure_object(dict)

@liteset_cli.command()
def runserver():
    """Run Litestar dev server via Uvicorn"""
    import uvicorn
    uvicorn.run(
        "liteset.app:create_app",
        factory=True,
        reload=True,
        host="0.0.0.0",
        port=8088,
    )

@liteset_cli.command()
def init():
    """Initialize Liteset application (roles, permissions)"""
    anyio.run(async_init)

@liteset_cli.command()
@click.option("--verbose", "-v", is_flag=True)
def version(verbose):
    """Print version"""
    ...

@liteset_cli.group()
def db():
    """Database migration commands"""

@db.command()
def upgrade():
    """Run Alembic migrations"""
    from alembic.config import Config
    from alembic import command
    alembic_cfg = Config("alembic.ini")
    command.upgrade(alembic_cfg, "head")
```

### 10.3 Backward Compatibility

```python
# liteset/cli/compat.py
import click
import warnings
from liteset.cli.main import liteset_cli

@click.group(context_settings={"token_normalize_func": normalize_token})
@click.pass_context
def superset_cli(ctx):
    """Legacy Superset CLI (deprecated, use 'liteset' instead)"""
    warnings.warn(
        "'superset' command is deprecated, use 'liteset' instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    ctx.ensure_object(dict)

# Register all the same commands from liteset_cli
for name, cmd in liteset_cli.commands.items():
    superset_cli.add_command(cmd, name)
```

### 10.4 Entry Points

```toml
# pyproject.toml or setup.py
[project.scripts]
liteset = "liteset.cli.main:liteset_cli"
superset = "liteset.cli.compat:superset_cli"
```

### 10.5 Command Mapping

| Command | superset (legacy) | liteset (new) | Notes |
|---|---|---|---|
| Init | `superset init` | `liteset init` | Create roles/permissions |
| Version | `superset version` | `liteset version` | Print version |
| DB upgrade | `superset db upgrade` | `liteset db upgrade` | Alembic directly |
| Run server | `superset run` (Flask) | `liteset runserver` (Uvicorn) | New async server |
| Load examples | `superset load-examples` | `liteset load-examples` | Same data |
| Import/export | `superset import-*` | `liteset import-*` | Backward compat |
| Thumbnails | `superset compute-thumbnails` | `liteset compute-thumbnails` | Via Celery |

---

## 11. Configuration Backward Compatibility

### 11.1 Strategy

Full backward compatibility with existing `superset_config.py` files. `SupersetConfigSettingsSource` (a custom Pydantic settings source) loads the entire config module and maps all fields that liteset uses. Unmapped fields are preserved in `app.state.legacy_config` for Flask fallback access during Strangler Fig coexistence. No explicit `from_superset_config()` classmethod is needed — `LitesetSettings()` resolves values automatically via the Pydantic Settings source chain (see section 11.2).

### 11.2 Config Loading Order

Implemented via Pydantic Settings' `settings_customise_sources()` to ensure correct priority:

1. Constructor kwargs / `init_settings` (highest priority — for programmatic overrides)
2. Environment variables with `LITESET_` prefix
3. `.env` file (dotenv)
4. `superset_config.py` via custom `SupersetConfigSettingsSource` (reads Python module)
5. File secrets / Pydantic Settings defaults (lowest priority)

```python
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

class SupersetConfigSettingsSource(PydanticBaseSettingsSource):
    """Reads superset_config.py as a Pydantic settings source.

    Loads the config module via importlib (path from SUPERSET_CONFIG_PATH env var),
    maps SUPERSET_* keys to liteset field names via _SUPERSET_TO_LITESET dict.
    Caches loaded values per config path to avoid re-executing the module.
    """
    def get_field_value(self, field, field_name):
        if field_name in self._values:
            return self._values[field_name], field_name, True
        return None, field_name, False

class LitesetSettings(BaseSettings):
    ...

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings,
                                    env_settings, dotenv_settings,
                                    file_secret_settings):
        return (
            init_settings,                              # highest (programmatic)
            env_settings,                               # env vars (LITESET_*)
            dotenv_settings,                            # .env file
            SupersetConfigSettingsSource(settings_cls),  # superset_config.py
            file_secret_settings,                       # lowest
        )
```

### 11.3 Key Mappings

Fields are added incrementally as controllers are migrated. Each phase adds the config fields its controllers require. Full list maintained in `liteset/config.py` as the source of truth.

---

## 12. Database Migration (Alembic) Strategy

### 12.1 Dual-Driver Approach

- **Runtime**: `postgresql+asyncpg://` (async) — used by Litestar app
- **Migrations**: `postgresql+psycopg2://` (sync) — used by Alembic

Alembic does not support async engines natively. Instead of async workarounds, we use psycopg2 as a dedicated sync driver for migrations only. This is configured in `alembic.ini` with the sync version of the database URI.

### 12.2 Migration Files

All migration files remain in `superset/migrations/versions/` during coexistence. Moved to `liteset/migrations/` in the cleanup phase.

---

## 13. Async Events & WebSocket Strategy

### 13.1 Current State (Superset)

- **REST polling**: `AsyncEventsRestApi` — frontend polls `/api/v1/async_event/` for query results
- **WebSocket**: `superset-websocket/` — standalone Node.js module, Redis pub/sub

### 13.2 Target State (Liteset)

Both mechanisms supported simultaneously:

1. **Polling REST API** (preserved) — `AsyncEventsController` migrated as regular Litestar controller. Frontend continues to use this without changes.
2. **Native WebSocket** (new) — Litestar `@websocket()` handler (server-push pattern, see section 3.7) replaces the Node.js `superset-websocket` module. Redis pub/sub integration for event streaming.

### 13.3 Frontend Compatibility

The frontend is NOT touched in this migration. It continues using the REST polling API. The WebSocket endpoint is available for future frontend migration or external consumers.

---

## 14. Thumbnails Strategy

### 14.1 Scope

Only the **trigger** (Celery task dispatch) is migrated to liteset. The actual thumbnail generation logic (Selenium/headless Chrome) remains in `superset/tasks/` as a Celery task.

### 14.2 Implementation

`liteset/thumbnails/digest.py` — computes cache digest and dispatches the Celery task. Does not import Selenium or any browser automation code.

---

## 15. Engine Spec Migration Roadmap

### 15.1 Native Async Specs (Phase 2: data-layer)

| Database | Async Driver | Spec File |
|---|---|---|
| PostgreSQL | asyncpg | postgres.py |
| MySQL | asyncmy | mysql.py |
| ClickHouse | asynch | clickhouse.py |
| Trino | aiotrino | trino.py |

### 15.2 Sync Fallback (default for all others)

All 40+ remaining database engines supported by Superset use `SyncFallbackEngineSpec` which wraps the existing sync `BaseEngineSpec` via `conn.run_sync()` / `run_in_executor`. This ensures zero functionality regression.

### 15.3 Future Async Migration

Additional databases should be migrated to native async specs as stable async drivers become available. Priority order based on Superset usage statistics.

---

## 16. Logging Strategy

### 16.1 Problem

Superset already uses standard `logging.getLogger(__name__)` throughout the codebase. However, it also relies on Flask's `current_app.config["STATS_LOGGER"]` and Flask-level logging configuration (via `app.logger`). In async code these Flask-bound patterns are unavailable.

### 16.2 Approach

- Use Python standard `logging` module with `structlog` for structured JSON output
- Each module uses `logger = logging.getLogger(__name__)` (same as Superset's existing pattern)
- Configure log format in `liteset/app.py` on_startup via `logging.config.dictConfig()`

```python
# liteset/logging.py
import logging
import structlog

def configure_logging(settings: LitesetSettings) -> None:
    """Configure structured logging.

    Uses structlog processors for JSON output in production,
    colorized console output in development.
    """
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer() if settings.production
            else structlog.dev.ConsoleRenderer(),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
    )
```

### 16.3 Migration Map

| Flask Pattern | Liteset Replacement |
|---|---|
| `logging.getLogger(__name__)` | Same — no migration needed |
| `current_app.config["STATS_LOGGER"]` | `state.settings.stats_logger` via DI |
| Flask's request logging | Litestar access log middleware |

---

## 17. Test Database Strategy

### 17.1 Problem

Unit test examples in this document use `sqlite+aiosqlite://` (section 5.2), but production uses PostgreSQL. SQLite does not support PostgreSQL-specific features (JSON operators, array types, `FOR UPDATE`, window functions over partitions). This can lead to false-positive tests.

### 17.2 Approach

| Test Level | Database | Rationale |
|---|---|---|
| Unit tests (DAO, simple CRUD) | `sqlite+aiosqlite://` in-memory | Fast, no external deps, sufficient for basic ORM logic |
| Integration tests (endpoints) | PostgreSQL via `testcontainers-python` | Real DB behavior, PG-specific features, auto-cleanup |
| Contract tests (parity) | PostgreSQL via `testcontainers-python` | Must match production exactly |

```python
# tests/liteset/conftest.py
import pytest
from testcontainers.postgres import PostgresContainer

@pytest.fixture(scope="session")
def pg_container():
    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg

@pytest.fixture
async def async_pg_session(pg_container):
    url = pg_container.get_connection_url().replace(
        "postgresql+psycopg2://", "postgresql+asyncpg://"
    )
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSession(engine) as session:
        yield session
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
```

This ensures integration and contract tests run against real PostgreSQL while keeping unit tests fast.

---

## 18. Async Cache Layer

### 18.1 Problem

Superset uses `flask-caching` extensively for: query results, chart data, database metadata,
CSV exports, explore form data, filter state, and thumbnails. `flask-caching` is synchronous
and tightly coupled to Flask's app context.

### 18.2 Approach

A thin async cache manager wrapping `redis.asyncio` directly, without a framework-specific
caching library. This avoids adding another dependency and gives full control over async behavior.

```python
# liteset/cache/manager.py
from redis.asyncio import Redis

class AsyncCacheManager:
    """Async cache layer replacing flask-caching.

    Supports multiple cache regions (data, metadata, thumbnails)
    with independent TTL and serialization settings.
    """

    def __init__(self, redis: Redis, default_ttl: int = 300):
        self.redis = redis
        self.default_ttl = default_ttl

    async def get(self, key: str) -> bytes | None:
        return await self.redis.get(key)

    async def set(self, key: str, value: bytes, ttl: int | None = None) -> None:
        await self.redis.set(key, value, ex=ttl or self.default_ttl)

    async def delete(self, key: str) -> None:
        await self.redis.delete(key)

    async def get_or_set(
        self, key: str, factory: Callable[[], Awaitable[bytes]], ttl: int | None = None,
    ) -> bytes:
        """Cache-aside pattern with thundering herd protection.

        Uses Redis SET NX to acquire a short-lived lock before computing.
        Only one caller executes factory(); others wait and retry.
        This prevents N concurrent cache misses from triggering N
        identical expensive computations (e.g., chart_data queries).
        """
        cached = await self.get(key)
        if cached is not None:
            return cached

        lock_key = f"_lock:{key}"
        lock_ttl = 30  # seconds — safety net for factory() hangs

        # Try to acquire lock (SET NX = set if not exists)
        acquired = await self.redis.set(lock_key, b"1", nx=True, ex=lock_ttl)
        if acquired:
            try:
                # Double-check after acquiring lock
                cached = await self.get(key)
                if cached is not None:
                    return cached
                value = await factory()
                await self.set(key, value, ttl)
                # Notify waiting subscribers that value is ready
                await self.redis.publish(f"_notify:{key}", b"1")
                return value
            finally:
                await self.redis.delete(lock_key)
        else:
            # Another caller is computing — wait for notification via Redis pub/sub.
            # The lock holder publishes to a notification channel when done.
            # This avoids busy-wait polling (up to 300 Redis GETs per waiter).
            import asyncio
            notify_channel = f"_notify:{key}"
            try:
                # Subscribe and wait for notification (with timeout = lock_ttl)
                pubsub = self.redis.pubsub()
                await pubsub.subscribe(notify_channel)
                async with asyncio.timeout(lock_ttl):
                    async for message in pubsub.listen():
                        if message["type"] == "message":
                            break  # got notification — value should be in cache now
                await pubsub.unsubscribe(notify_channel)
                await pubsub.close()
            except TimeoutError:
                pass  # lock expired without notification — fall through

            # Check cache after notification (or timeout)
            cached = await self.get(key)
            if cached is not None:
                return cached

            # Lock expired without value — fallback to direct compute
            value = await factory()
            await self.set(key, value, ttl)
            return value
```

### 18.3 Cache Regions

| Region | Purpose | Default TTL | Serialization |
|---|---|---|---|
| `data` | Query results, chart data | 5 min | msgspec JSON |
| `metadata` | Database schemas, table lists | 10 min | msgspec JSON |
| `thumbnails` | Dashboard/chart thumbnails | 24 hours | raw bytes |
| `filter_state` | Dashboard filter state | 7 days | msgspec JSON |
| `explore_form_data` | Explore form state | 7 days | msgspec JSON |

Regions are implemented as key prefixes (e.g., `data:chart:123`, `meta:db:5:schemas`).

### 18.4 Invalidation

Cache invalidation is triggered by Commands (Create/Update/Delete) via explicit `cache.delete()`
calls. No automatic invalidation framework — explicit is better than implicit.

---

## 19. Sync Fallback via SQLAlchemy Greenlets

### 19.1 Problem

`SyncFallbackEngineSpec` wraps 40+ synchronous database drivers for use in async context.
These drivers have no native async support and need a bridging mechanism.

### 19.2 Approach

Uses SQLAlchemy's `AsyncConnection.run_sync()` — a greenlet-based approach that executes
synchronous callables within the greenlet context of the async connection. This avoids the
overhead of a separate thread pool and integrates naturally with SQLAlchemy's async engine.

```python
# liteset/db/engine_specs/sync_fallback.py
class SyncFallbackEngineSpec:
    async def get_schema_names(self, conn: AsyncConnection) -> list[str]:
        def _run(sync_conn: Connection) -> list[str]:
            inspector = sa.inspect(sync_conn)
            return inspector.get_schema_names()
        return await conn.run_sync(_run)
```

Each method (`get_catalog_names`, `get_schema_names`, `get_table_names`, `get_columns`,
`execute`, `fetch_data`, `extract_errors`) defines an inner `_run()` function executed
via `run_sync()`.

### 19.3 Trade-offs

| Aspect | greenlet (`run_sync`) | ThreadPoolExecutor |
|---|---|---|
| Overhead | Minimal — no thread creation | Thread per call |
| SQLAlchemy integration | Native — shares connection context | Requires connection passing |
| Concurrency model | Cooperative (greenlet switch) | Preemptive (OS threads) |
| Configuration | None needed | `LITESET_SYNC_DB_POOL_SIZE` |

The greenlet approach was chosen for simplicity and tighter SQLAlchemy integration.
No additional configuration or monitoring is required.

---

## 20. Internationalization (i18n) Strategy

### 20.1 Problem

Superset uses `flask-babel` with `lazy_gettext()` extensively — in model field labels,
validation messages, and error strings. `lazy_gettext()` returns a lazy proxy that resolves
the translation at string-render time using Flask's request-scoped locale context.
In async code without Flask context, these lazy proxies would fail to resolve.

### 20.2 Approach

Replace `flask-babel` with `babel` directly + a context-variable-based locale resolver:

```python
# liteset/i18n.py
import contextvars
import gettext as gettext_module
from pathlib import Path

_current_locale: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_locale", default="en"
)

_translations: dict[str, gettext_module.GNUTranslations] = {}

def init_translations(locale_dir: Path, languages: list[str]) -> None:
    """Load .mo files at startup for all supported languages."""
    for lang in languages:
        _translations[lang] = gettext_module.translation(
            "messages", localedir=str(locale_dir), languages=[lang],
        )

def gettext(message: str) -> str:
    """Translate message using current request locale."""
    locale = _current_locale.get()
    if locale in _translations:
        return _translations[locale].gettext(message)
    return message

def lazy_gettext(message: str) -> "LazyString":
    """Lazy proxy that resolves at str() time using current locale context.

    Uses contextvars instead of Flask request context, so it works
    correctly in async handlers and background tasks.
    """
    return LazyString(gettext, message)

class LazyString:
    """Lazy string proxy — resolves translation on __str__/__repr__.

    Implements the full string protocol needed by Superset codebase:
    comparison, hashing, concatenation, formatting (f-strings, % and .format()),
    len(), contains, and iteration. This ensures lazy_gettext() is a drop-in
    replacement for flask-babel's LazyString everywhere in Superset.
    """
    __slots__ = ("_func", "_args")

    def __init__(self, func, *args):
        self._func = func
        self._args = args

    def __str__(self) -> str:
        return self._func(*self._args)

    def __repr__(self) -> str:
        return f"l'{self.__str__()}'"

    # Comparison uses the original (untranslated) message string to maintain
    # consistency with __hash__ (Python contract: a == b → hash(a) == hash(b)).
    # This means two LazyStrings are equal iff they wrap the same source string,
    # regardless of the current locale. This matches flask-babel's LazyString
    # behavior where equality is identity-based on the source message.
    def __eq__(self, other):
        if isinstance(other, LazyString):
            return self._args == other._args
        return str(self) == str(other)
    def __ne__(self, other): return not self.__eq__(other)
    def __lt__(self, other): return str(self) < str(other)
    def __add__(self, other): return str(self) + other
    def __radd__(self, other): return other + str(self)

    # Hashing: uses the original (untranslated) message string to ensure
    # consistent hash across different locales. This is critical when
    # LazyString is used as a dict key or set member — hash must not depend
    # on the current request locale, otherwise the same LazyString would
    # hash differently in different contexts, causing dict lookup failures.
    def __hash__(self): return hash(self._args[0] if self._args else "")

    # Formatting (f-strings, .format(), %-formatting)
    def __format__(self, format_spec: str) -> str: return format(str(self), format_spec)
    def __mod__(self, other): return str(self) % other

    # Sequence protocol (len, contains, iteration, indexing)
    def __len__(self) -> int: return len(str(self))
    def __contains__(self, item) -> bool: return item in str(self)
    def __iter__(self): return iter(str(self))
    def __getitem__(self, key): return str(self)[key]

    # bool: empty string is falsy
    def __bool__(self) -> bool: return bool(str(self))
```

### 20.3 Locale Middleware

```python
# liteset/middleware/locale.py
from litestar.middleware import AbstractMiddleware

class LocaleMiddleware(AbstractMiddleware):
    """Sets _current_locale contextvar from request context.

    Priority: user preference (DB) > cookie > Accept-Language header > default.
    contextvars are async-safe and automatically scoped per-task.
    """
    async def __call__(self, scope, receive, send):
        locale = resolve_locale(scope)  # parse Accept-Language, check cookie/user pref
        token = _current_locale.set(locale)
        try:
            await self.app(scope, receive, send)
        finally:
            _current_locale.reset(token)
```

### 20.4 Migration Path

Superset's existing `.po`/`.mo` translation files in `superset/translations/` are reused
as-is. The `init_translations()` call in `on_startup` points to the same locale directory.

---

## 21. Connection Pool Configuration

### 21.1 AsyncEngine Pool Settings

```python
# liteset/db/session.py
from sqlalchemy.ext.asyncio import create_async_engine

def create_engine(settings: LitesetSettings) -> AsyncEngine:
    """Create async engine with production-grade pool settings.

    create_async_engine uses AsyncAdaptedQueuePool by default.
    asyncpg uses its own internal connection pool, but SQLAlchemy's
    pool layer on top provides: overflow handling, connection recycling,
    pre-ping health checks, and checkout timeout control.
    """
    return create_async_engine(
        settings.sqlalchemy_database_uri,
        pool_size=settings.db_pool_size,             # steady-state connections
        max_overflow=settings.db_max_overflow,        # burst capacity above pool_size
        pool_timeout=settings.db_pool_timeout,        # seconds to wait for connection
        pool_recycle=settings.db_pool_recycle,         # seconds before connection is recycled
        pool_pre_ping=True,                           # verify connection is alive before checkout
        echo=settings.sqlalchemy_echo,                # SQL logging (dev only)
    )
```

### 21.2 Pool Settings in LitesetSettings

```python
# In liteset/config.py — added to LitesetSettings
db_pool_size: int = 10               # default: 10 steady-state connections
db_max_overflow: int = 20            # default: 20 burst connections (total max = 30)
db_pool_timeout: int = 30            # default: 30s wait for available connection
db_pool_recycle: int = 1800          # default: 30min — recycle stale connections
sqlalchemy_echo: bool = False        # SQL statement logging
```

### 21.3 Monitoring

Pool health is logged on startup and available via `/healthz` endpoint:

| Metric | Source | Description |
|---|---|---|
| `pool.status` | `engine.pool.status()` | Human-readable pool status string |

> **Note:** `QueuePool` also exposes `size()`, `checkedin()`, `checkedout()`, `overflow()`
> methods which provide more granular metrics. These are not part of SQLAlchemy's public API
> but are stable and widely used for monitoring. Wrap access in try/except for forward
> compatibility.

---

## 22. Health Check & Probes

### 22.1 Endpoints

```python
# Inline in liteset/app.py (not a controller — lightweight)
from litestar import get
from litestar.status_codes import HTTP_200_OK, HTTP_503_SERVICE_UNAVAILABLE

# Matches original Flask Superset routes: /health, /healthcheck, /ping
@get(["/health", "/healthcheck", "/ping"])
async def health_check() -> dict[str, str]:
    """Liveness probe — returns 200 if the process is running.

    Used by Kubernetes livenessProbe and load balancers.
    Does NOT check external dependencies (DB, Redis) to avoid
    cascading restarts when a dependency is temporarily unavailable.
    """
    return {"status": "OK"}

@get("/healthz")
async def readiness_check(state: State) -> Response:
    """Readiness probe — returns 200 only if all dependencies are reachable.

    Used by Kubernetes readinessProbe to remove the pod from
    service discovery when it cannot serve traffic.
    Checks: database connection pool, Redis connectivity.
    """
    checks: dict[str, str] = {}

    # Database: verify pool can checkout a connection
    try:
        async with state.session_factory() as session:
            await session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as e:
        checks["database"] = f"error: {e}"

    # Redis: verify connectivity (Redis is required for all environments)
    try:
        await state.redis.ping()
        checks["redis"] = "ok"
    except Exception as e:
        checks["redis"] = f"error: {e}"

    all_ok = all(v == "ok" for v in checks.values())
    return Response(
        content={"status": "ok" if all_ok else "degraded", "checks": checks},
        status_code=HTTP_200_OK if all_ok else HTTP_503_SERVICE_UNAVAILABLE,
    )
```

### 22.2 Kubernetes Configuration

```yaml
# Recommended probe configuration for deployment manifests
livenessProbe:
  httpGet:
    path: /health
    port: 8088
  initialDelaySeconds: 10
  periodSeconds: 15
  failureThreshold: 3

readinessProbe:
  httpGet:
    path: /healthz
    port: 8088
  initialDelaySeconds: 5
  periodSeconds: 10
  failureThreshold: 2
```

---

## 23. Graceful Shutdown

### 23.1 Shutdown Sequence

```
SIGTERM received
    |
    v
Uvicorn signals Litestar to shut down
    |
    v
1. Stop accepting new connections
2. Litestar on_shutdown hooks fire:
    +-- dispose_engine(): close DB pool, drain active connections
    +-- close_redis(): close Redis connections
    +-- cancel_websockets(): close all active WebSocket connections
3. In-flight HTTP requests finish (within timeout)
4. Process exits
```

### 23.2 Configuration

```python
# Uvicorn graceful shutdown timeout
uvicorn.run(
    "liteset.app:create_app",
    factory=True,
    timeout_graceful_shutdown=30,  # seconds to finish in-flight requests
    ...
)
```

### 23.3 on_shutdown Hooks

```python
# liteset/app.py — registered in create_app()
async def dispose_engine(app: Litestar) -> None:
    """Drain and close the database connection pool.

    Waits for checked-out connections to be returned (up to pool_timeout),
    then disposes the engine. Any connections not returned in time are
    forcibly closed to avoid hanging shutdown.
    """
    await app.state.engine.dispose()

async def close_redis(app: Litestar) -> None:
    """Close Redis connection pool."""
    await app.state.redis.close()

async def cancel_websockets(app: Litestar) -> None:
    """Send close frames to all active WebSocket connections.

    Each WebSocket handler checks for cancellation in its event loop
    and performs clean shutdown (unsubscribe from Redis, close socket).
    """
    for ws in list(app.state.active_websockets.keys()):
        await ws.close(code=1001, reason="Server shutting down")
```

---

## 24. Rate Limiting

### 24.1 Approach

Rate limiting is implemented as a Litestar middleware using Redis as the backend counter store. This replaces `flask-limiter` with an async-native implementation.

```python
# liteset/middleware/rate_limit.py
from litestar.middleware import AbstractMiddleware
from redis.asyncio import Redis

class RateLimitMiddleware(AbstractMiddleware):
    """Fixed window rate limiter using Redis.

    Runs AFTER AuthMiddleware (see section 26) so that `scope["user"]`
    is available for per-user rate limits. Unauthenticated requests
    (including login) fall back to per-IP limiting. The login endpoint
    has a separate, stricter per-IP limit (RATELIMIT_LOGIN) to protect
    against brute-force attacks.

    Uses a Redis Lua script for atomic increment + expire to prevent
    race conditions (key left without TTL if crash occurs between
    separate commands).

    Note: This is a fixed window counter, not a sliding window. At
    window boundaries, up to 2x the configured limit may be allowed
    within a short burst. This trade-off is acceptable for the current
    use case — a true sliding window (via Redis sorted sets) adds
    complexity and per-request overhead with marginal benefit.
    """
    # Lua script: atomic INCR + conditional EXPIRE.
    # Returns the current counter value after increment.
    # KEYS[1] = rate limit key, ARGV[1] = window TTL in seconds.
    _LUA_INCR_WITH_EXPIRE = """
    local current = redis.call('INCR', KEYS[1])
    if current == 1 then
        redis.call('EXPIRE', KEYS[1], ARGV[1])
    end
    return current
    """

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Identify caller: user ID (authenticated) or IP (anonymous)
        user = scope.get("user")
        key = f"rl:user:{user.id}" if user else f"rl:ip:{scope['client'][0]}"

        redis: Redis = scope["app"].state.redis

        # redis.evalsha() / redis.eval() executes the Lua script atomically
        # on the Redis server. This is NOT Python eval() — it is the standard
        # Redis API for server-side scripting (EVAL command, RFC-safe).
        current = await redis.execute_command(
            "EVAL", self._LUA_INCR_WITH_EXPIRE, 1, key, self.window_seconds,
        )

        if current > self.max_requests:
            response = Response(
                content={"message": "Rate limit exceeded"},
                status_code=429,
                headers={"Retry-After": str(self.window_seconds)},
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)
```

### 24.2 Default Limits

| Scope | Limit | Window | Configurable via |
|---|---|---|---|
| Authenticated user | 1000 requests | 1 minute | `RATELIMIT_AUTHENTICATED` |
| Anonymous (per IP) | 100 requests | 1 minute | `RATELIMIT_ANONYMOUS` |
| Login endpoint | 10 requests | 1 minute | `RATELIMIT_LOGIN` |
| Chart data endpoint | 200 requests | 1 minute | `RATELIMIT_CHART_DATA` |

Limits match Superset's default `flask-limiter` configuration for full backward compatibility. All limits are configurable via `superset_config.py` or environment variables.

### 24.3 Bypass

- Internal health check endpoints (`/health`, `/healthz`) are excluded
- Configurable allowlist for trusted IPs/service accounts

---

## 25. Celery Task Import Migration (Cleanup Phase)

### 25.1 Problem

During coexistence (phases 1-6), Celery tasks in `superset/tasks/` import from `superset/` modules. In the cleanup phase (phase 7), `superset/` is removed. Celery tasks must be updated to import from `liteset/` before removal.

### 25.2 Migration Plan

```
Phase 7 (liteset/cleanup) — Celery import migration:

1. Move SQLAlchemy models from superset/models/ to liteset/models/
   (this is already planned in the cleanup branch)

2. Update Celery task imports:
   superset/tasks/*.py → liteset/tasks/*.py
   - Replace: from superset.models.X import Y
     With:    from liteset.models.X import Y
   - Replace: from superset.utils.X import Y
     With:    from liteset.utils.X import Y (for migrated utils)
             or keep original import (for utils still in superset/)

3. Transitional compatibility layer (temporary):
   # superset/__init__.py (during transition only)
   import warnings
   warnings.warn("superset package is deprecated, use liteset", DeprecationWarning)
   # Re-export commonly imported symbols for gradual migration

4. Final removal: delete superset/ once all imports are updated
```

### 25.3 Task Inventory

| Task Module | Key Imports from superset/ | Migration Notes |
|---|---|---|
| `tasks/thumbnails.py` | models.Slice, models.Dashboard, utils.screenshots | Models move to liteset/; screenshot utils stay (Selenium) |
| `tasks/alerts.py` | models.ReportSchedule, commands.report | Commands already in liteset/ at phase 5 |
| `tasks/cache.py` | models.*, utils.cache | Models move; cache utils replaced by AsyncCacheManager |
| `tasks/scheduler.py` | models.*, celery_app | celery_app config stays compatible |

### 25.4 Celery Beat Configuration

Celery Beat schedule entries reference task paths by string (e.g., `"superset.tasks.alerts.execute"`). These must be updated in `superset_config.py` / `liteset_config.py`:

```python
# Before (superset_config.py):
CELERYBEAT_SCHEDULE = {
    "reports.scheduler": {
        "task": "superset.tasks.scheduler.execute",
        ...
    },
}

# After (liteset config):
CELERYBEAT_SCHEDULE = {
    "reports.scheduler": {
        "task": "liteset.tasks.scheduler.execute",
        ...
    },
}
```

Backward compatibility: accept both `superset.tasks.*` and `liteset.tasks.*` task names during transition via Celery's `task_routes` or name aliasing.

### 25.5 Deployment Topology

**Important:** Celery workers run as separate processes and may be deployed on
separate hosts/containers from the Litestar web server. During the cleanup phase,
both `liteset/` and `superset/` must be available in the Celery worker's `PYTHONPATH`.

```
# docker-compose.yml (example)
services:
  web:
    command: liteset runserver
    # Only needs liteset/ (and superset/ during coexistence phases 1-6)

  worker:
    command: celery -A liteset.tasks worker
    # Must have BOTH liteset/ and superset/ in PYTHONPATH during transition.
    # After cleanup phase: only liteset/ is needed.
    environment:
      - PYTHONPATH=/app

  beat:
    command: celery -A liteset.tasks beat
    # Same PYTHONPATH requirements as worker
```

After the cleanup phase, the `superset/` package can be removed from all deployments.

---

## 26. Middleware Ordering

### 26.1 Execution Order

Middleware order matters — Litestar executes middleware in registration order (outermost first).
The order differs from Flask, where middleware-like functionality is spread across
`before_request` hooks, decorators, and extensions. In Litestar, all middleware is explicit:

```python
# liteset/app.py — middleware registration order
Litestar(
    middleware=[
        # 1. CORS — must run first to handle preflight OPTIONS requests
        #    before any auth/rate-limit logic rejects them.
        CORSConfig(...).middleware,

        # 2. Security headers (CSP, HSTS, X-Frame-Options) — applied to
        #    all responses regardless of auth status.
        SecurityHeadersMiddleware,

        # 3. Authentication — resolves current user from cookie/JWT/API-key.
        #    Sets connection.user for downstream middleware and handlers.
        AuthMiddleware,

        # 4. Rate limiting — runs AFTER auth so it can use per-user limits
        #    for authenticated requests and per-IP limits for anonymous.
        #    Login endpoint has a separate, stricter per-IP limit configured
        #    via RATELIMIT_LOGIN to protect against brute-force attacks
        #    (this works because the login endpoint is unauthenticated and
        #    falls back to IP-based limiting automatically).
        RateLimitMiddleware,

        # 5. Locale — resolves locale from user preference / Accept-Language.
        #    Runs after auth so it can check user.locale preference.
        LocaleMiddleware,

        # 6. CSRF — validates CSRF token for state-changing requests.
        #    Runs after auth because CSRF is only relevant for
        #    cookie-authenticated (browser) requests.
        CSRFConfig(...).middleware,
    ],
)
```

### 26.2 Key Ordering Constraints

| Constraint | Reason |
|---|---|
| CORS before Auth | Preflight OPTIONS must not be rejected by auth |
| Auth before RateLimit | RateLimit uses `scope["user"]` for per-user limits; login endpoint protected via per-IP `RATELIMIT_LOGIN` |
| Auth before Locale | Locale middleware needs `connection.user` for user preference |
| Auth before CSRF | CSRF only applies to cookie-authenticated requests |
| SecurityHeaders early | Headers must be set even on error responses |

---

## 27. OpenAPI Schema Compatibility

### 27.1 Problem

Superset frontend and third-party integrations may depend on the auto-generated
OpenAPI/Swagger schema at `/swagger/v1`. Switching from Flask-AppBuilder + Marshmallow
to Litestar + msgspec changes the schema generation engine, which can produce
different field names, types, or structures.

### 27.2 Approach

1. **Schema snapshot**: Before migration, export the current Flask Swagger JSON as a
   baseline snapshot (`tests/liteset/contract/flask_openapi_snapshot.json`).

2. **Contract test**: Compare Litestar-generated OpenAPI schema against the snapshot
   for each migrated controller. Key checks:
   - Same endpoint paths and HTTP methods
   - Same request body field names and types
   - Same response field names and types
   - Same query parameter names (including Rison `q` param)
   - Same error response format (SIP-40)

3. **Known divergences**: Document acceptable differences:
   - `nullable` vs `anyOf` (OpenAPI 3.0 vs 3.1 difference)
   - Additional `description` fields from msgspec docstrings
   - Schema `$ref` names may differ (internal naming, not user-facing)

```python
# tests/liteset/contract/test_openapi_parity.py
import json

def test_openapi_endpoint_parity(litestar_client):
    resp = litestar_client.get("/swagger/v1/openapi.json")
    generated = resp.json()

    with open("tests/liteset/contract/flask_openapi_snapshot.json") as f:
        snapshot = json.load(f)

    # Compare paths (endpoint coverage)
    assert set(generated["paths"].keys()) >= set(snapshot["paths"].keys()), \
        f"Missing endpoints: {set(snapshot['paths']) - set(generated['paths'])}"

    # Compare methods per path
    for path in snapshot["paths"]:
        if path in generated["paths"]:
            assert set(generated["paths"][path].keys()) >= \
                   set(snapshot["paths"][path].keys()), \
                f"Missing methods for {path}"
```

### 27.3 msgspec Struct Naming Convention

To minimize schema divergence, msgspec Struct names follow the same pattern
as the original Marshmallow schemas:

| Marshmallow Schema | msgspec Struct |
|---|---|
| `ChartPostSchema` | `ChartPostSchema` |
| `ChartGetResponseSchema` | `ChartGetResponseSchema` |
| `DashboardDatasetSchema` | `DashboardDatasetSchema` |

---

## 28. Error Monitoring (Sentry)

### 28.1 Current State

Superset integrates with Sentry via `sentry-sdk[flask]`. The Flask integration
auto-captures exceptions, request context, and user information.

### 28.2 Migration

Replace `sentry-sdk[flask]` with `sentry-sdk[litestar]`:

```python
# liteset/app.py — Sentry initialization in on_startup
import sentry_sdk
from sentry_sdk.integrations.litestar import LitestarIntegration

async def on_startup(app: Litestar) -> None:
    settings = app.state.settings

    if settings.sentry_dsn:
        sentry_sdk.init(
            dsn=settings.sentry_dsn,
            # Native LitestarIntegration (available since sentry-sdk 2.13.0)
            # Auto-enabled if litestar is installed, but explicit is better.
            integrations=[
                LitestarIntegration(),
                SqlalchemyIntegration(),
                RedisIntegration(),
                CeleryIntegration(),
            ],
            traces_sample_rate=settings.sentry_traces_sample_rate,
            environment=settings.sentry_environment,
            release=settings.version,
            # Send user context (id, email) — set by AuthMiddleware
            send_default_pii=True,
        )

    # ... rest of startup (engine, redis, etc.)
```

### 28.3 Config Fields

```python
# In liteset/config.py — added to LitesetSettings
sentry_dsn: str = ""                          # empty = disabled
sentry_traces_sample_rate: float = 0.1        # 10% of transactions
sentry_environment: str = "development"
```

### 28.4 User Context

Sentry user context is set automatically by AuthMiddleware after resolving the user:

```python
# In AuthMiddleware.authenticate_request(), after successful auth:
sentry_sdk.set_user({"id": user.id, "email": user.email, "username": user.username})
```

---

## 29. Feature Flags Migration

### 29.1 Current State

Superset uses feature flags extensively via `superset_config.py`:

```python
# superset_config.py
FEATURE_FLAGS = {
    "DASHBOARD_NATIVE_FILTERS": True,
    "ENABLE_TEMPLATE_PROCESSING": False,
    "ALERT_REPORTS": True,
    # ... 74 flags in DEFAULT_FEATURE_FLAGS
}
```

These are accessed via `is_feature_enabled("FLAG_NAME")` and `get_feature_flags()` which
read from `current_app.config["FEATURE_FLAGS"]`.

### 29.2 Migration Strategy

Feature flags are loaded into `LitesetSettings` from `superset_config.py` via
`SupersetConfigSettingsSource` (section 11.2) and stored as a dict:

```python
# liteset/config.py — added to LitesetSettings
feature_flags: dict[str, bool] = {}

# liteset/feature_flags.py
from litestar.datastructures import State

def is_feature_enabled(flag_name: str, state: State) -> bool:
    """Check if a feature flag is enabled.

    Replaces superset.utils.core.is_feature_enabled() which depends
    on Flask's current_app. Uses Litestar's State (app-level) instead.
    """
    return state.settings.feature_flags.get(flag_name, False)

def get_feature_flags(state: State) -> dict[str, bool]:
    """Return all feature flags. Used by SPA bootstrap_data."""
    return state.settings.feature_flags
```

### 29.3 Usage in Controllers

```python
# Instead of: from superset.utils.core import is_feature_enabled
# Use Litestar State dependency:

@get("/")
async def get_list(self, state: State, ...) -> ...:
    if is_feature_enabled("DASHBOARD_NATIVE_FILTERS", state):
        ...
```

### 29.4 Frontend Compatibility

Feature flags are passed to the frontend via `bootstrap_data` in the SPA template.
The SPA controller (section 9.3) includes them in the template context:

```python
# liteset/controllers/spa.py — updated spa_page handler
context = {
    "bootstrap_data": json.dumps({
        "common": {
            "feature_flags": get_feature_flags(state),
            ...
        },
    }),
    ...
}
```

This maintains full compatibility with the frontend's `isFeatureEnabled()` checks.

---

## 30. CI/CD Strategy (GitHub Actions)

### 30.1 Approach

Reuse and extend the existing Superset GitHub Actions workflows (40 workflows in `.github/workflows/`). Liteset adds new workflows for async-specific testing while preserving all existing workflows during coexistence.

### 30.2 New Workflows

| Workflow | Trigger | Description |
|---|---|---|
| `liteset-python-unittest.yml` | push/PR to `liteset/*` branches | Async unit tests (DAO, commands, guards, middleware) via `pytest tests/liteset/unit/` |
| `liteset-python-integrationtest.yml` | push/PR to `liteset/*` branches | Integration tests with PostgreSQL + Redis services via `pytest tests/liteset/integration/` |
| `liteset-contract-test.yml` | push/PR to `liteset/core-api`, `liteset/remaining-api` | Flask vs Litestar response parity checks via `pytest tests/liteset/contract/` |
| `liteset-load-test.yml` | manual dispatch / push to `liteset/cleanup` | Locust load tests with benchmark result artifact upload |

### 30.3 Workflow Structure (unit test example)

```yaml
# .github/workflows/liteset-python-unittest.yml
name: Liteset Python Unit Tests

on:
  push:
    branches: ["liteset/*"]
    paths:
      - "liteset/**"
      - "tests/liteset/**"
  pull_request:
    paths:
      - "liteset/**"
      - "tests/liteset/**"

jobs:
  unit-tests:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.12"]
    services:
      redis:
        image: redis:7-alpine
        ports: ["6379:6379"]
        options: --health-cmd "redis-cli ping" --health-interval 10s
    steps:
      - uses: actions/checkout@v4
      - name: Set up Python ${{ matrix.python-version }}
        uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
      - name: Install dependencies
        run: |
          pip install -e ".[dev]"
      - name: Run unit tests
        run: pytest tests/liteset/unit/ -v --tb=short
```

### 30.4 Integration Test Services

Integration and contract tests require PostgreSQL and Redis as GitHub Actions services:

```yaml
services:
  postgres:
    image: postgres:16-alpine
    env:
      POSTGRES_USER: superset
      POSTGRES_PASSWORD: superset
      POSTGRES_DB: superset_test
    ports: ["5432:5432"]
    options: --health-cmd pg_isready --health-interval 10s
  redis:
    image: redis:7-alpine
    ports: ["6379:6379"]
```

### 30.5 Reused Existing Workflows

These existing workflows continue to run unchanged during coexistence:

| Workflow | Relevance |
|---|---|
| `pre-commit.yml` | Linting, formatting, mypy — applies to `liteset/` code |
| `license-check.yml` | ASF headers on new `liteset/` files |
| `codeql-analysis.yml` | Security scanning covers `liteset/` |
| `pr-lint.yml` | Conventional commits enforced on all PRs |
| `superset-frontend.yml` | Frontend tests — must keep passing (no frontend changes) |
| `superset-python-unittest.yml` | Flask tests — must keep passing during coexistence |
| `docker.yml` | Docker builds — updated in cleanup phase for Litestar entrypoint |

### 30.6 Contract Test Workflow

Contract tests run both Flask and Litestar against the same database to verify response parity:

```yaml
# .github/workflows/liteset-contract-test.yml
jobs:
  contract-tests:
    runs-on: ubuntu-latest
    services:
      postgres: { ... }  # same as integration
      redis: { ... }
    steps:
      - uses: actions/checkout@v4
      - name: Install dependencies
        run: pip install -e ".[dev]"
      - name: Run contract tests
        env:
          SUPERSET__SQLALCHEMY_DATABASE_URI: postgresql+psycopg2://superset:superset@localhost:5432/superset_test
          LITESET_SQLALCHEMY_DATABASE_URI: postgresql+asyncpg://superset:superset@localhost:5432/superset_test
        run: pytest tests/liteset/contract/ -v --tb=long
```

### 30.7 Phase-Specific CI Gates

| Branch | Required Checks |
|---|---|
| `liteset/infrastructure` | unit tests, pre-commit, license-check |
| `liteset/data-layer` | unit tests (DAO), pre-commit |
| `liteset/auth` | unit + integration tests, pre-commit |
| `liteset/core-api` | unit + integration + contract tests, pre-commit |
| `liteset/remaining-api` | unit + integration + contract tests, pre-commit |
| `liteset/websocket` | unit + integration tests (WebSocket), pre-commit |
| `liteset/cleanup` | all tests + load tests, Docker build, pre-commit |
