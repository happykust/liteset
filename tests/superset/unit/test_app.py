import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from litestar import Litestar
from litestar.config.cors import CORSConfig
from litestar.testing import AsyncTestClient
from sqlalchemy.exc import SQLAlchemyError

from superset.app import _build_cors_config, _seed_one_theme, create_app
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
    base = {
        "secret_key": "test-secret-long-enough",
        "sqlalchemy_database_uri": "sqlite+aiosqlite://",
    }
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


# ---------------------------------------------------------------------------
# _seed_one_theme — per-theme independent transaction semantics
# ---------------------------------------------------------------------------
# Original: superset_old/commands/theme/seed.py::_upsert_system_theme decorated
# with @transaction(), one call per theme, each commits independently.
# Regression guard: a failure in one theme must NOT prevent another theme from
# being committed (the old single-session loop lost ALL themes on any error).


def _make_session(existing_theme: MagicMock | None = None) -> AsyncMock:
    """Return a mock AsyncSession. ``existing_theme`` is what SELECT returns."""
    session = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock()
    result = MagicMock()
    scalars = MagicMock()
    scalars.first = MagicMock(return_value=existing_theme)
    result.scalars = MagicMock(return_value=scalars)
    session.execute = AsyncMock(return_value=result)
    return session


def _factory_for(session: AsyncMock):
    """Return a session_factory whose context-manager yields ``session``."""

    @asynccontextmanager
    async def factory():
        yield session

    return factory


async def test_seed_one_theme_inserts_new_theme() -> None:
    """When no existing theme row is found a new Theme is add()ed and committed."""
    session = _make_session(existing_theme=None)

    await _seed_one_theme(_factory_for(session), "THEME_DEFAULT", {"color": "blue"})

    session.add.assert_called_once()
    session.commit.assert_awaited_once()
    added = session.add.call_args[0][0]
    assert added.theme_name == "THEME_DEFAULT"
    assert json.loads(added.json_data) == {"color": "blue"}
    assert added.is_system is True


async def test_seed_one_theme_updates_existing_theme() -> None:
    """When an existing system theme row is found its json_data is updated."""
    existing = MagicMock()
    existing.json_data = json.dumps({"color": "old"})
    session = _make_session(existing_theme=existing)

    await _seed_one_theme(_factory_for(session), "THEME_DEFAULT", {"color": "new"})

    session.add.assert_not_called()
    session.commit.assert_awaited_once()
    assert json.loads(existing.json_data) == {"color": "new"}


async def test_seed_one_theme_independent_sessions_per_call() -> None:
    """Every _seed_one_theme call opens and commits its own session.

    1:1 with superset_old/commands/theme/seed.py::run() calling
    _upsert_system_theme once per theme where each call has @transaction()
    wrapping it.  Two consecutive calls must produce two independent sessions
    and two independent commits.
    """
    session1 = _make_session()
    session2 = _make_session()
    idx = 0

    @asynccontextmanager
    async def factory():
        nonlocal idx
        sess = [session1, session2][idx]
        idx += 1
        yield sess

    await _seed_one_theme(factory, "THEME_DEFAULT", {"a": 1})
    await _seed_one_theme(factory, "THEME_DARK", {"b": 2})

    session1.commit.assert_awaited_once()
    session2.commit.assert_awaited_once()
    assert session1.add.call_count == 1
    assert session2.add.call_count == 1


async def test_seed_one_theme_second_failure_does_not_revert_first() -> None:
    """A DB error in theme2's commit must not revoke theme1's already-committed data.

    This is the regression the per-transaction fix addresses: with the old
    single-session loop a commit error for the second theme rolled back both
    because there was one shared session.  Now each theme has its own session,
    so theme1's commit is already done and unaffected.
    """
    session1 = _make_session()
    session2 = _make_session()
    session2.commit = AsyncMock(side_effect=SQLAlchemyError("simulated DB error"))

    idx = 0

    @asynccontextmanager
    async def factory():
        nonlocal idx
        sess = [session1, session2][idx]
        idx += 1
        yield sess

    # theme1 succeeds
    await _seed_one_theme(factory, "THEME_DEFAULT", {"a": 1})
    session1.commit.assert_awaited_once()

    # theme2 fails — exception propagates (on_startup catches it per-theme)
    with pytest.raises(SQLAlchemyError):
        await _seed_one_theme(factory, "THEME_DARK", {"b": 2})

    # theme1's commit was already done and remains unaffected
    session1.commit.assert_awaited_once()


async def test_seed_one_theme_uuid_ref_not_found_skips_upsert() -> None:
    """UUID-only config whose referenced theme does not exist returns early."""
    session = _make_session(existing_theme=None)  # execute returns None

    await _seed_one_theme(_factory_for(session), "THEME_DEFAULT", {"uuid": "missing"})

    session.add.assert_not_called()
    session.commit.assert_not_awaited()


async def test_seed_one_theme_uuid_ref_bad_json_skips_upsert() -> None:
    """UUID-only config whose referenced theme has unparseable JSON returns early."""
    referenced = MagicMock()
    referenced.json_data = "{{invalid-json"

    session = _make_session(existing_theme=None)
    ref_result = MagicMock()
    ref_scalars = MagicMock()
    ref_scalars.first = MagicMock(return_value=referenced)
    ref_result.scalars = MagicMock(return_value=ref_scalars)
    session.execute = AsyncMock(return_value=ref_result)

    await _seed_one_theme(_factory_for(session), "THEME_DEFAULT", {"uuid": "u1"})

    session.add.assert_not_called()
    session.commit.assert_not_awaited()
