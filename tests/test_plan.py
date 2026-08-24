from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from itest.cli import app

runner = CliRunner()

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "simple-web-app-plan.json"


@pytest.fixture
def workdir(tmp_path, monkeypatch) -> Path:
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_plan_first_run_all_new(workdir: Path) -> None:
    result = runner.invoke(app, ["plan", "--tf-json", str(FIXTURE)])
    assert result.exit_code == 0, result.output
    assert "3 new, 0 unchanged, 0 orphaned" in result.output
    # The three edges appear in the human summary.
    assert "0.0.0.0/0 -> aws_security_group.alb" in result.output
    assert "aws_security_group.alb -> aws_security_group.web" in result.output
    assert "aws_security_group.web -> aws_security_group.db" in result.output
    # Not-analyzed section is reported, not silently dropped.
    assert "Not analyzed" in result.output
    assert "aws_db_instance" in result.output


def test_plan_writes_artifacts(workdir: Path) -> None:
    runner.invoke(app, ["plan", "--tf-json", str(FIXTURE)])

    plan_file = workdir / ".itest" / "plan.json"
    diagram_file = workdir / ".itest" / "diagram.mmd"
    assert plan_file.exists()
    assert diagram_file.exists()

    data = json.loads(plan_file.read_text())
    assert len(data["new_points"]) == 3
    assert data["unchanged_points"] == []
    assert data["unanalyzed"]["aws_instance"] == 2

    diagram = diagram_file.read_text()
    assert diagram.startswith("flowchart")
    assert "tcp:443" in diagram
    assert "tcp:5432" in diagram


def test_plan_does_not_touch_manifest(workdir: Path) -> None:
    manifest_file = workdir / ".itest" / "manifest.yaml"
    manifest_file.parent.mkdir(parents=True)
    sentinel = (
        "schema_version: 1\ngenerated_at: '2026-01-01T00:00:00+00:00'\n"
        "points: []\ntests: []\n"
    )
    manifest_file.write_text(sentinel)

    runner.invoke(app, ["plan", "--tf-json", str(FIXTURE)])

    # Plan never writes the manifest.
    assert manifest_file.read_text() == sentinel


def test_plan_json_output(workdir: Path) -> None:
    result = runner.invoke(app, ["plan", "--tf-json", str(FIXTURE), "--output", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert len(payload["new_points"]) == 3
    assert payload["orphan_candidates"] == []


def test_plan_bad_tf_json_path(workdir: Path) -> None:
    result = runner.invoke(app, ["plan", "--tf-json", "does-not-exist.json"])
    assert result.exit_code == 1
    assert "not found" in result.output
