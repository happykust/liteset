import pytest
from litestar import Litestar
from litestar.testing import AsyncTestClient

from liteset.app import create_app
from liteset.config import LitesetSettings


@pytest.fixture
def app():
    settings = LitesetSettings(
        secret_key="test-secret-long-enough",
        sqlalchemy_database_uri="sqlite+aiosqlite://",
        cors_allow_origins=["*"],
    )
    return create_app(settings=settings, enable_flask_fallback=False)


async def test_app_is_litestar_instance(app):
    assert isinstance(app, Litestar)


async def test_health_endpoint(app):
    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/api/v1/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "OK"}


async def test_openapi_available(app):
    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/schema/openapi.json")
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
