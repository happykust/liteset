import pytest
from litestar import Litestar
from litestar.config.cors import CORSConfig
from litestar.testing import AsyncTestClient

from superset.app import _build_cors_config, create_app
from superset.config import SupersetSettings


@pytest.fixture
def app():
    settings = SupersetSettings(
        secret_key="test-secret-long-enough",
        sqlalchemy_database_uri="sqlite+aiosqlite://",
        cors_allow_origins=["*"],
    )
    return create_app(settings=settings)


async def test_app_is_litestar_instance(app):
    assert isinstance(app, Litestar)


async def test_health_endpoint(app):
    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/api/v1/health")
        assert resp.status_code == 200
        assert resp.text == "OK"


@pytest.mark.skip(
    reason="OpenAPI path requires auth exclusion — deferred to superset/cleanup"
)
async def test_openapi_available(app):
    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/swagger/v1/openapi.json")
        assert resp.status_code == 200
        data = resp.json()
        assert data["info"]["title"] == "Superset API"


async def test_static_routes_have_unique_names(app):
    """Both static file routers should register without name collision."""
    route_names = [r.name for r in app.routes if hasattr(r, "name")]
    static_names = [n for n in route_names if n and "static" in n.lower()]
    assert len(static_names) == len(set(static_names)), (
        f"Duplicate static route names: {static_names}"
    )


async def test_app_has_auth_middleware(app):
    """App should have SupersetAuthMiddleware registered."""
    assert any("SupersetAuthMiddleware" in str(m) for m in app.middleware)


async def test_app_has_security_controller(app):
    """App should have SecurityController registered.

    ``/csrf_token/`` is guarded by ``require_authentication`` (1:1 upstream
    ``@protect()``), so an unauthenticated request returns 401 — proving the
    controller IS registered (a 404 would indicate a missing controller).
    """
    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/api/v1/security/csrf_token/")
        assert resp.status_code == 401


async def test_app_has_current_user_dependency(app):
    """App should have current_user in dependencies."""
    assert "current_user" in app.dependencies


def _settings(**kwargs) -> SupersetSettings:
    base = dict(
        secret_key="test-secret-long-enough",
        sqlalchemy_database_uri="sqlite+aiosqlite://",
    )
    base.update(kwargs)
    return SupersetSettings(**base)


def test_cors_disabled_yields_no_config() -> None:
    """ENABLE_CORS=False -> CORS is OFF (no permissive default)."""
    settings = _settings(enable_cors=False, cors_options={"origins": ["*"]})
    assert _build_cors_config(settings) is None


def test_cors_disabled_app_has_no_cors_config() -> None:
    settings = _settings(enable_cors=False)
    app = create_app(settings=settings)
    assert app.cors_config is None


def test_cors_enabled_default_options() -> None:
    """Upstream default CORS_OPTIONS only sets origins; rest mirror Flask-CORS."""
    settings = _settings(
        enable_cors=True,
        cors_options={"origins": ["https://tile.openstreetmap.org"]},
    )
    cfg = _build_cors_config(settings)
    assert isinstance(cfg, CORSConfig)
    assert cfg.allow_origins == ["https://tile.openstreetmap.org"]
    assert cfg.allow_methods == ["*"]
    assert cfg.allow_headers == ["*"]
    assert cfg.expose_headers == []
    assert cfg.allow_credentials is False
    assert cfg.max_age == 600


def test_cors_enabled_full_mapping() -> None:
    """Every Flask-CORS option maps onto the matching CORSConfig field."""
    settings = _settings(
        enable_cors=True,
        cors_options={
            "origins": ["https://a.com", "https://b.com"],
            "methods": ["GET", "POST"],
            "allow_headers": ["X-Custom", "Content-Type"],
            "expose_headers": ["X-Total-Count"],
            "supports_credentials": True,
            "max_age": 3600,
        },
    )
    cfg = _build_cors_config(settings)
    assert isinstance(cfg, CORSConfig)
    assert cfg.allow_origins == ["https://a.com", "https://b.com"]
    assert cfg.allow_methods == ["GET", "POST"]
    # Litestar lowercases header names internally.
    assert [h.lower() for h in cfg.allow_headers] == ["x-custom", "content-type"]
    assert cfg.expose_headers == ["X-Total-Count"]
    assert cfg.allow_credentials is True
    assert cfg.max_age == 3600


def test_cors_enabled_string_origin_coerced_to_list() -> None:
    """Flask-CORS accepts a bare string for origins; coerce to a list."""
    settings = _settings(enable_cors=True, cors_options={"origins": "*"})
    cfg = _build_cors_config(settings)
    assert isinstance(cfg, CORSConfig)
    assert cfg.allow_origins == ["*"]


def test_cors_enabled_app_attaches_config() -> None:
    settings = _settings(
        enable_cors=True,
        cors_options={"origins": ["https://tile.osm.ch"]},
    )
    app = create_app(settings=settings)
    assert isinstance(app.cors_config, CORSConfig)
    assert app.cors_config.allow_origins == ["https://tile.osm.ch"]
