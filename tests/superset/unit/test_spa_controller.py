import pytest
from litestar import Litestar
from litestar.testing import AsyncTestClient

from superset.app import create_app
from superset.config import SupersetSettings
from superset.controllers.spa import SPA_ROUTE_PREFIXES


@pytest.fixture
def app():
    settings = SupersetSettings(
        secret_key="test-secret-long-enough",
        sqlalchemy_database_uri="sqlite+aiosqlite://",
        cors_allow_origins=["*"],
    )
    return create_app(settings=settings)


@pytest.mark.parametrize(
    "path",
    [
        "/superset/welcome/",
        "/explore/",
        "/dashboard/list/",
        "/superset/sqllab/",
        "/chart/list/",
    ],
)
async def test_spa_routes_return_html(app: Litestar, path: str):
    async with AsyncTestClient(app=app) as client:
        resp = await client.get(path)
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")


async def test_spa_route_contains_bootstrap_data(app: Litestar):
    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/superset/welcome/")
        assert resp.status_code == 200


async def test_spa_route_with_path_param(app: Litestar):
    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/dashboard/42/")
        assert resp.status_code == 200


async def test_known_spa_route_200(app: Litestar):
    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/superset/welcome/")
        assert resp.status_code == 200


async def test_unknown_prefix_404(app: Litestar):
    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/unknown/path/")
        assert resp.status_code == 404


def test_spa_route_prefixes_not_empty():
    assert len(SPA_ROUTE_PREFIXES) > 0
