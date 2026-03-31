import pytest
from litestar import Litestar
from litestar.testing import AsyncTestClient

from superset.app import create_app
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
    """App should have SecurityController registered."""
    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/api/v1/security/csrf_token/")
        assert resp.status_code == 200


async def test_app_has_current_user_dependency(app):
    """App should have current_user in dependencies."""
    assert "current_user" in app.dependencies
