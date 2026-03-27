from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from superset.cli.compat import superset_cli
from superset.cli.main import superset_cli


@pytest.fixture
def runner():
    return CliRunner()


def test_superset_version(runner):
    result = runner.invoke(superset_cli, ["version"])
    assert result.exit_code == 0
    assert "Superset" in result.output


def test_superset_version_verbose(runner):
    result = runner.invoke(superset_cli, ["version", "-v"])
    assert result.exit_code == 0
    assert "Litestar" in result.output
    assert "SQLAlchemy" in result.output


def test_superset_init(runner):
    with patch("superset.config.SupersetSettings") as mock_settings_cls, \
         patch("superset.db.session.create_db_engine") as mock_engine, \
         patch("superset.db.session.create_session_factory") as mock_sf:
        mock_settings = MagicMock()
        mock_settings.sqlalchemy_database_uri = "sqlite+aiosqlite:///:memory:"
        mock_settings_cls.return_value = mock_settings

        mock_eng = AsyncMock()
        mock_eng.dispose = AsyncMock()
        mock_engine.return_value = mock_eng

        # Build a mock session context manager
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.commit = AsyncMock()

        mock_session_cm = AsyncMock()
        mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cm.__aexit__ = AsyncMock(return_value=False)
        mock_sf.return_value.return_value = mock_session_cm

        result = runner.invoke(superset_cli, ["init"])
        assert result.exit_code == 0
        assert "Initializing" in result.output


def test_superset_compat_version(runner):
    result = runner.invoke(superset_cli, ["version"])
    assert result.exit_code == 0
    assert "Superset" in result.output


def test_superset_has_db_group(runner):
    result = runner.invoke(superset_cli, ["db", "--help"])
    assert result.exit_code == 0
    assert "upgrade" in result.output
