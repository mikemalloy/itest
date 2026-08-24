from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from itest.cli import app
from itest.core.manifest import load_manifest

runner = CliRunner()

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "simple-web-app-plan.json"
STUB_FILE = "itest_tests/test_sg_edges.py"


@pytest.fixture
def workdir(tmp_path, monkeypatch) -> Path:
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _sync(*extra: str):
    return runner.invoke(app, ["sync", "--auto-approve", "--tf-json", *extra])


def test_first_sync_creates_three_stubs(workdir: Path) -> None:
    result = runner.invoke(app, ["sync", "--auto-approve", "--tf-json", str(FIXTURE)])
    assert result.exit_code == 0, result.output
    assert "added 3 stub(s)" in result.output

    manifest = load_manifest(workdir / ".itest" / "manifest.yaml")
    assert len(manifest.points) == 3
    assert len(manifest.tests) == 3
    assert all(t.status == "stub" for t in manifest.tests)

    stub_text = (workdir / STUB_FILE).read_text()
    assert stub_text.count("def test_sg_") == 3
    assert "pytest.skip" in stub_text
    # Names are derived from the points, not indices.
    assert "def test_sg_internet_to_alb_443" in stub_text
    assert "def test_sg_web_to_db_5432" in stub_text


def test_second_sync_is_noop(workdir: Path) -> None:
    runner.invoke(app, ["sync", "--auto-approve", "--tf-json", str(FIXTURE)])
    before = (workdir / STUB_FILE).read_text()

    result = runner.invoke(app, ["sync", "--auto-approve", "--tf-json", str(FIXTURE)])
    assert result.exit_code == 0
    assert "No changes to apply" in result.output

    # Nothing appended.
    assert (workdir / STUB_FILE).read_text() == before
    manifest = load_manifest(workdir / ".itest" / "manifest.yaml")
    assert manifest.tests[0].test_name  # manifest still intact
    assert (before.count("def test_sg_")) == 3


def test_human_edit_preserved_and_orphan_flagged(workdir: Path) -> None:
    # First sync: generate the three stubs.
    runner.invoke(app, ["sync", "--auto-approve", "--tf-json", str(FIXTURE)])

    stub_path = workdir / STUB_FILE
    original = stub_path.read_text()
    assert "def test_sg_web_to_db_5432" in original

    # Human edits the web->db stub: truncate the file at that function and
    # rewrite its body with a real assertion. (web->db is the last stub, so
    # truncating there leaves the earlier two stubs intact.)
    marker = "def test_sg_web_to_db_5432():"
    idx = original.index(marker)
    edited = (
        original[:idx]
        + marker
        + '\n    """human edited"""\n'
        + "    assert 1 + 1 == 2  # human-written check\n"
    )
    stub_path.write_text(edited)
    human_body = stub_path.read_text()
    assert "human-written check" in human_body

    # Remove the db_from_web rule from a copy of the fixture: web->db vanishes.
    fixture_copy = workdir / "plan-modified.json"
    data = json.loads(FIXTURE.read_text())
    resources = data["planned_values"]["root_module"]["resources"]
    data["planned_values"]["root_module"]["resources"] = [
        r for r in resources if r["address"] != "aws_security_group_rule.db_from_web"
    ]
    fixture_copy.write_text(json.dumps(data))

    # Re-sync against the modified plan.
    result = runner.invoke(
        app, ["sync", "--auto-approve", "--tf-json", str(fixture_copy)]
    )
    assert result.exit_code == 0, result.output

    # The human-edited file was not rewritten.
    assert stub_path.read_text() == human_body
    assert "human-written check" in stub_path.read_text()

    # The web->db test entry is flagged orphaned in the manifest.
    manifest = load_manifest(workdir / ".itest" / "manifest.yaml")
    web_db = [t for t in manifest.tests if t.test_name == "test_sg_web_to_db_5432"]
    assert len(web_db) == 1
    assert web_db[0].status == "orphaned"

    # The point itself is gone from the registry.
    assert all(p.target != "aws_security_group.db" for p in manifest.points)
