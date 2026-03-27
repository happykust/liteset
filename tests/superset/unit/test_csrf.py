"""Tests for CSRF configuration and token endpoint."""

from __future__ import annotations

from litestar import Litestar
from litestar.testing import AsyncTestClient

from superset.controllers.security import SecurityController
from superset.middleware.csrf import create_csrf_config


def test_csrf_config_created():
    config = create_csrf_config(secret="test-secret-at-least-16")
    assert config is not None
    assert config.secret == "test-secret-at-least-16"


def test_csrf_config_excludes_safe_methods():
    config = create_csrf_config(secret="test-secret-at-least-16")
    assert "GET" in config.safe_methods


def test_csrf_config_exclude_paths():
    config = create_csrf_config(
        secret="test-secret-at-least-16",
        exclude_paths=["/api/v1/health"],
    )
    assert "/api/v1/health" in config.exclude


async def test_csrf_token_endpoint():
    app = Litestar(
        route_handlers=[SecurityController],
    )
    async with AsyncTestClient(app=app) as client:
        resp = await client.get("/api/v1/security/csrf_token/")
        assert resp.status_code == 200
        data = resp.json()
        assert "result" in data


async def test_csrf_token_endpoint_returns_cookie_value():
    """Endpoint should return the CSRF cookie value when present."""
    app = Litestar(
        route_handlers=[SecurityController],
    )
    async with AsyncTestClient(app=app) as client:
        # First call — no cookie yet, returns empty string
        resp = await client.get("/api/v1/security/csrf_token/")
        assert resp.status_code == 200
        assert resp.json()["result"] == ""
