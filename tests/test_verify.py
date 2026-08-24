from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from itest.cli import app

runner = CliRunner()

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "simple-web-app-plan.json"
STUB_FILE = "itest_tests/test_sg_edges.py"

SKIP_LINE = 'pytest.skip("stub: implement this integration test")'


@pytest.fixture
def synced_project(tmp_path, monkeypatch) -> Path:
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["sync", "--auto-approve", "--tf-json", str(FIXTURE)])
    return tmp_path


def _set_body(base_dir: Path, func: str, statement: str) -> None:
    """Replace the pytest.skip in ``func`` with ``statement`` (no indent)."""
    path = base_dir / STUB_FILE
    text = path.read_text()
    marker = f"def {func}():"
    i = text.index(marker)
    j = text.index(SKIP_LINE, i)
    end = text.index("\n", j) + 1
    path.write_text(text[:j] + statement + "\n" + text[end:])


def test_verify_rollup_with_one_passing(synced_project: Path) -> None:
    # Turn the web->db stub into a real (pure-python) passing check.
    _set_body(synced_project, "test_sg_web_to_db_5432", "assert 1 + 1 == 2")

    result = runner.invoke(app, ["verify"])
    assert result.exit_code == 0, result.output
    assert "3 integration points: 1 passing, 0 failing, 2 stubs" in result.output
    # The passing point renders as PASS, the stubs as STUB.
    assert "[PASS] aws_security_group.web -> aws_security_group.db" in result.output
    assert result.output.count("[STUB]") == 2


def test_verify_failing_test_exit_1(synced_project: Path) -> None:
    _set_body(synced_project, "test_sg_alb_to_web_80", "assert False, 'boom'")

    result = runner.invoke(app, ["verify"])
    assert result.exit_code == 1, result.output
    assert "1 failing" in result.output
    assert "[FAIL] aws_security_group.alb -> aws_security_group.web" in result.output
    assert "Failing tests:" in result.output


def test_verify_json_output(synced_project: Path) -> None:
    _set_body(synced_project, "test_sg_web_to_db_5432", "assert True")
    result = runner.invoke(app, ["verify", "--output", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["total_points"] == 3
    assert payload["passing"] == 1
    assert payload["stubs"] == 2


def test_verify_junit_output(synced_project: Path) -> None:
    result = runner.invoke(app, ["verify", "--output", "junit"])
    assert result.exit_code == 0
    junit = synced_project / "itest-results.xml"
    assert junit.exists()
    content = junit.read_text()
    assert "<testsuite" in content
    assert "test_sg_internet_to_alb_443" in content


def test_verify_without_manifest_is_config_error(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["verify"])
    assert result.exit_code == 2
    assert "itest plan" in result.output
