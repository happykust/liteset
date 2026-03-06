import pytest
from click.testing import CliRunner

from liteset.cli.compat import superset_cli
from liteset.cli.main import liteset_cli


@pytest.fixture
def runner():
    return CliRunner()


def test_liteset_version(runner):
    result = runner.invoke(liteset_cli, ["version"])
    assert result.exit_code == 0
    assert "Liteset" in result.output


def test_liteset_version_verbose(runner):
    result = runner.invoke(liteset_cli, ["version", "-v"])
    assert result.exit_code == 0
    assert "Litestar" in result.output
    assert "SQLAlchemy" in result.output


def test_liteset_init(runner):
    result = runner.invoke(liteset_cli, ["init"])
    assert result.exit_code == 0
    assert "Done" in result.output


def test_superset_compat_version(runner):
    result = runner.invoke(superset_cli, ["version"])
    assert result.exit_code == 0
    assert "Liteset" in result.output


def test_liteset_has_db_group(runner):
    result = runner.invoke(liteset_cli, ["db", "--help"])
    assert result.exit_code == 0
    assert "upgrade" in result.output
