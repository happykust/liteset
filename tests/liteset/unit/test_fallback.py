import pytest
from litestar import Litestar, get
from litestar.testing import AsyncTestClient


@get("/litestar-native")
async def native_route() -> dict:
    return {"source": "litestar"}


async def test_native_route_works():
    """Test that a plain Litestar app works (sanity check)."""
    app = Litestar(route_handlers=[native_route])
    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/litestar-native")
        assert resp.status_code == 200
        assert resp.json() == {"source": "litestar"}


def test_create_flask_fallback_importable():
    """Test that the fallback function is importable."""
    from liteset.fallback import create_flask_fallback

    assert callable(create_flask_fallback)
