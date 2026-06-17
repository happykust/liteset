from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from superset.config import _superset_config_cache, SupersetSettings


def test_default_settings(monkeypatch) -> None:
    # Neutralise ambient LITESET_* env (env has higher precedence than field defaults).
    monkeypatch.delenv("LITESET_SECRET_KEY", raising=False)
    monkeypatch.delenv("LITESET_SQLALCHEMY_DATABASE_URI", raising=False)
    # sqlalchemy_database_uri has no field default in production; supply the upstream
    # sync sqlite URI to exercise the async-driver conversion.
    settings = SupersetSettings(
        secret_key="test-key-long-enough",
        sqlalchemy_database_uri="sqlite:///superset.db",
    )
    assert settings.secret_key.get_secret_value() == "test-key-long-enough"
    assert settings.sqlalchemy_database_uri == "sqlite+aiosqlite:///superset.db"
    assert settings.host == "0.0.0.0"  # noqa: S104  # asserts default host value
    assert settings.port == 8088
    assert settings.debug is False


def test_ws_url_default_targets_main_app_ws_events() -> None:
    """The default GAQ WebSocket URL points at the main app's ``/ws/events``.

    Liteset folds the WebSocket relay into the ASGI app (no Node sidecar), so
    the default must target the in-app ``/ws/events`` path on the canonical
    Superset port — NOT upstream's now-removed ``ws://127.0.0.1:8080/``
    sidecar, which can never work here.
    """
    from urllib.parse import urlparse

    from superset.websocket.events import AsyncQueryWebSocket

    settings = SupersetSettings(secret_key="test-key-long-enough")
    parsed = urlparse(settings.global_async_queries_websocket_url)
    assert parsed.scheme == "ws"
    assert parsed.path == "/ws/events"
    assert AsyncQueryWebSocket.path == "/"
    assert str(settings.port) in settings.global_async_queries_websocket_url


def test_async_uri_conversion_postgresql() -> None:
    settings = SupersetSettings(
        secret_key="test-key-long-enough",
        sqlalchemy_database_uri="postgresql://user:pass@localhost/db",
    )
    assert (
        settings.sqlalchemy_database_uri
        == "postgresql+asyncpg://user:pass@localhost/db"
    )


def test_async_uri_conversion_psycopg2() -> None:
    settings = SupersetSettings(
        secret_key="test-key-long-enough",
        sqlalchemy_database_uri="postgresql+psycopg2://user:pass@localhost/db",
    )
    assert (
        settings.sqlalchemy_database_uri
        == "postgresql+asyncpg://user:pass@localhost/db"
    )


def test_async_uri_no_conversion_already_async() -> None:
    settings = SupersetSettings(
        secret_key="test-key-long-enough",
        sqlalchemy_database_uri="sqlite+aiosqlite:///test.db",
    )
    assert settings.sqlalchemy_database_uri == "sqlite+aiosqlite:///test.db"


def test_from_superset_config(tmp_path: Path) -> None:
    config_file = tmp_path / "superset_config.py"
    config_file.write_text(
        textwrap.dedent("""\
            SECRET_KEY = "superset-secret-long-enough"
            SQLALCHEMY_DATABASE_URI = "postgresql://user:pass@localhost/superset"
        """)
    )
    settings = SupersetSettings.from_superset_config(str(config_file))
    assert settings.secret_key.get_secret_value() == "superset-secret-long-enough"
    assert (
        settings.sqlalchemy_database_uri
        == "postgresql+asyncpg://user:pass@localhost/superset"
    )


def test_secret_key_rejects_short_value() -> None:
    with pytest.raises(Exception, match="at least 16 characters"):
        SupersetSettings(secret_key="short")


def test_default_cors_is_empty() -> None:
    settings = SupersetSettings(secret_key="test-key-long-enough")
    assert settings.cors_allow_origins == []


def test_from_superset_config_maps_extra_fields(tmp_path: Path) -> None:
    config_file = tmp_path / "superset_config.py"
    config_file.write_text(
        textwrap.dedent("""\
            SECRET_KEY = "superset-secret-long-enough"
            SQLALCHEMY_DATABASE_URI = "postgresql://user:pass@localhost/superset"
            CORS_ALLOW_ORIGINS = ["http://localhost:3000"]
            GLOBAL_ASYNC_QUERIES = True
            STATIC_ASSETS_PREFIX = "/cdn"
        """)
    )
    settings = SupersetSettings.from_superset_config(str(config_file))
    assert settings.cors_allow_origins == ["http://localhost:3000"]
    assert settings.global_async_queries is True
    assert settings.static_assets_prefix == "/cdn"


def test_from_superset_config_missing_secret_key(tmp_path: Path) -> None:
    config_file = tmp_path / "superset_config.py"
    config_file.write_text('SQLALCHEMY_DATABASE_URI = "postgresql://u:p@host/db"\n')
    with pytest.raises(ValueError, match="SECRET_KEY not found"):
        SupersetSettings.from_superset_config(str(config_file))


@pytest.fixture(autouse=True)
def _clear_config_cache():
    _superset_config_cache.clear()
    yield
    _superset_config_cache.clear()


def test_superset_config_source_auto_loads(monkeypatch, tmp_path):
    config_file = tmp_path / "superset_config.py"
    config_file.write_text(
        'SECRET_KEY = "auto-loaded-secret-key"\n'
        'SQLALCHEMY_DATABASE_URI = "postgresql://u:p@host/db"\n'
    )
    monkeypatch.setenv("SUPERSET_CONFIG_PATH", str(config_file))
    # Env has higher precedence than superset_config.py; neutralise to let
    # config-file values win.
    monkeypatch.delenv("LITESET_SECRET_KEY", raising=False)
    monkeypatch.delenv("LITESET_SQLALCHEMY_DATABASE_URI", raising=False)
    settings = SupersetSettings()
    assert settings.secret_key.get_secret_value() == "auto-loaded-secret-key"
    assert "asyncpg" in settings.sqlalchemy_database_uri


def test_env_overrides_superset_config(monkeypatch, tmp_path):
    config_file = tmp_path / "superset_config.py"
    config_file.write_text('SECRET_KEY = "from-superset-config"\n')
    monkeypatch.setenv("SUPERSET_CONFIG_PATH", str(config_file))
    monkeypatch.setenv("LITESET_SECRET_KEY", "from-env-override-key")
    settings = SupersetSettings()
    assert settings.secret_key.get_secret_value() == "from-env-override-key"


def test_no_superset_config_uses_defaults(monkeypatch):
    monkeypatch.delenv("SUPERSET_CONFIG_PATH", raising=False)
    monkeypatch.setenv("LITESET_SECRET_KEY", "fallback-secret-key")
    settings = SupersetSettings()
    assert settings.secret_key.get_secret_value() == "fallback-secret-key"


def test_version_sha_respects_configured_length():
    """``_resolve_version_info`` must honor VERSION_SHA_LENGTH, not [:8].

    The truncation length is configurable via VERSION_SHA_LENGTH.
    """
    from unittest.mock import mock_open, patch

    settings = SupersetSettings()
    settings.version_sha = ""
    settings.version_sha_length = 4
    with patch(
        "builtins.open",
        mock_open(read_data='{"GIT_SHA": "abcdef1234567890", "version": "6.0.0"}'),
    ):
        settings._resolve_version_info()
    assert settings.version_sha == "abcd"
