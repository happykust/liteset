from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from liteset.config import LitesetSettings


def test_default_settings() -> None:
    settings = LitesetSettings(secret_key="test-key-long-enough")
    assert settings.secret_key == "test-key-long-enough"
    # Default URI should be async sqlite
    assert settings.sqlalchemy_database_uri == "sqlite+aiosqlite:///superset.db"
    assert settings.host == "0.0.0.0"
    assert settings.port == 8088
    assert settings.debug is False


def test_async_uri_conversion_postgresql() -> None:
    settings = LitesetSettings(
        secret_key="test-key-long-enough",
        sqlalchemy_database_uri="postgresql://user:pass@localhost/db",
    )
    assert settings.sqlalchemy_database_uri == "postgresql+asyncpg://user:pass@localhost/db"


def test_async_uri_conversion_psycopg2() -> None:
    settings = LitesetSettings(
        secret_key="test-key-long-enough",
        sqlalchemy_database_uri="postgresql+psycopg2://user:pass@localhost/db",
    )
    assert settings.sqlalchemy_database_uri == "postgresql+asyncpg://user:pass@localhost/db"


def test_async_uri_no_conversion_already_async() -> None:
    settings = LitesetSettings(
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
    settings = LitesetSettings.from_superset_config(str(config_file))
    assert settings.secret_key == "superset-secret-long-enough"
    assert settings.sqlalchemy_database_uri == "postgresql+asyncpg://user:pass@localhost/superset"


def test_secret_key_rejects_short_value() -> None:
    with pytest.raises(Exception, match="at least 16 characters"):
        LitesetSettings(secret_key="short")


def test_default_cors_is_empty() -> None:
    settings = LitesetSettings(secret_key="test-key-long-enough")
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
    settings = LitesetSettings.from_superset_config(str(config_file))
    assert settings.cors_allow_origins == ["http://localhost:3000"]
    assert settings.global_async_queries is True
    assert settings.static_assets_prefix == "/cdn"


def test_from_superset_config_missing_secret_key(tmp_path: Path) -> None:
    config_file = tmp_path / "superset_config.py"
    config_file.write_text('SQLALCHEMY_DATABASE_URI = "postgresql://u:p@host/db"\n')
    with pytest.raises(ValueError, match="SECRET_KEY not found"):
        LitesetSettings.from_superset_config(str(config_file))
