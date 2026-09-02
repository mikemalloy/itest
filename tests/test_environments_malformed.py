"""F6 regression: a malformed environment spec is a config error, not a crash.

`dev: readonly` (a scalar where a mapping is expected) reached `spec.get(...)`
and raised AttributeError. It must raise EnvironmentConfigError at load time —
the same actionable, exit-2 refusal every other policy problem gets.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from itest.cli import app
from itest.core.environments import EnvironmentConfigError, load_policy

runner = CliRunner()


def _write_policy(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "environments.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_scalar_env_spec_raises_config_error(tmp_path: Path) -> None:
    policy = _write_policy(tmp_path, "version: 1\nenvironments:\n  dev: readonly\n")
    with pytest.raises(EnvironmentConfigError):
        load_policy(policy)


def test_list_env_spec_raises_config_error(tmp_path: Path) -> None:
    policy = _write_policy(
        tmp_path, "version: 1\nenvironments:\n  dev: [static, readonly]\n"
    )
    with pytest.raises(EnvironmentConfigError):
        load_policy(policy)


def test_verify_reports_malformed_env_as_exit_2(tmp_path, monkeypatch) -> None:
    """End to end: a scalar spec makes verify refuse to start with exit 2."""
    monkeypatch.chdir(tmp_path)
    itest_dir = tmp_path / ".itest"
    itest_dir.mkdir()
    (itest_dir / "environments.yaml").write_text(
        "version: 1\nenvironments:\n  dev: readonly\n", encoding="utf-8"
    )
    (itest_dir / "manifest.yaml").write_text(
        "schema_version: 2\ngenerated_at: '2026-01-01T00:00:00Z'\n"
        "points: []\ntests: []\n",
        encoding="utf-8",
    )
    (itest_dir / "environment").write_text("dev", encoding="utf-8")

    result = runner.invoke(app, ["verify"])
    assert result.exit_code == 2, result.output
