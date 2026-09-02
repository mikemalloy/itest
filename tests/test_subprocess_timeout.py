"""F13 regression: the pytest and terraform subprocesses must time out cleanly.

Neither subprocess.run had a timeout, so a hung pytest or a wedged `terraform
show -json` would block the CLI forever. Each now runs under a generous timeout
and raises a clear, typed error on expiry.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from typer.testing import CliRunner

from itest.cli import app

runner = CliRunner()

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "simple-web-app-plan.json"


def _timeout(*args, **kwargs):
    raise subprocess.TimeoutExpired(cmd="x", timeout=kwargs.get("timeout", 1))


def test_pytest_timeout_is_a_clean_config_error(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["sync", "--auto-approve", "--tf-json", str(FIXTURE)])

    from itest.core import verifier

    monkeypatch.setattr(verifier.subprocess, "run", _timeout)
    result = runner.invoke(app, ["verify"])
    assert result.exit_code == 2, result.output
    assert "timed out" in result.output.lower() or "did not finish" in (
        result.output.lower()
    )


def test_terraform_timeout_is_a_clean_plan_error(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    from itest.core import planner

    monkeypatch.setattr(planner.subprocess, "run", _timeout)
    # No --tf-json, so plan shells out to terraform, which "hangs".
    result = runner.invoke(app, ["plan"])
    assert result.exit_code == 1, result.output
    assert "did not finish" in result.output.lower() or "timed out" in (
        result.output.lower()
    )


def test_pytest_runs_under_a_timeout(tmp_path, monkeypatch) -> None:
    """The pytest subprocess is invoked with a positive timeout kwarg."""
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["sync", "--auto-approve", "--tf-json", str(FIXTURE)])

    from itest.core import verifier

    seen: dict = {}

    def capture(*args, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(verifier.subprocess, "run", capture)
    runner.invoke(app, ["verify"])
    assert isinstance(seen.get("timeout"), int | float) and seen["timeout"] > 0
