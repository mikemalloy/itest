"""F9 regression: the junit summary line must include the errored count.

`cli.render_verify_line` (the human line printed after `verify --output junit`)
omitted errored, so a broken import showed no error while the command exited 2.
Human mode already shows it. The fix is append-only — like the gated clause — so
the no-error line is byte-identical.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from itest.cli import app, render_verify_line
from itest.core.verifier import VerifyReport

runner = CliRunner()

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "simple-web-app-plan.json"


def test_no_error_line_is_byte_identical() -> None:
    report = VerifyReport(
        total_points=3, passing=1, failing=0, errored=0, stubs=2, orphaned_tests=0
    )
    assert render_verify_line(report) == (
        "3 integration points: 1 passing, 0 failing, 2 stubs, 0 orphaned tests."
    )


def test_errored_count_is_appended_when_present() -> None:
    report = VerifyReport(
        total_points=3, passing=0, failing=0, errored=3, stubs=0, orphaned_tests=0
    )
    assert render_verify_line(report) == (
        "3 integration points: 0 passing, 0 failing, 0 stubs, 0 orphaned tests, "
        "3 errored."
    )


def test_errored_and_gated_both_appended() -> None:
    report = VerifyReport(
        total_points=2,
        passing=0,
        failing=0,
        errored=1,
        stubs=0,
        orphaned_tests=0,
        gated=1,
    )
    line = render_verify_line(report)
    assert "1 errored" in line
    assert "1 gated" in line


@pytest.fixture
def synced_project(tmp_path, monkeypatch) -> Path:
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["sync", "--auto-approve", "--tf-json", str(FIXTURE)])
    return tmp_path


def test_junit_output_line_shows_errors(synced_project: Path) -> None:
    stub = synced_project / "itest_tests" / "test_sg_edges.py"
    stub.write_text("import itest_missing_dep_xyz\n" + stub.read_text())

    result = runner.invoke(app, ["verify", "--output", "junit"])
    assert result.exit_code == 2, result.output
    assert "3 errored" in result.output
