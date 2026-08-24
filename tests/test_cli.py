from __future__ import annotations

from typer.testing import CliRunner

from itest.cli import app

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == "0.1.0"


def test_sync_not_implemented() -> None:
    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 1
    assert "not implemented" in result.output


def test_verify_not_implemented() -> None:
    result = runner.invoke(app, ["verify"])
    assert result.exit_code == 1
    assert "not implemented" in result.output
