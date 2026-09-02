"""F0 regression: verify must run every registered test path, not just itest_tests/.

`verifier` hardcoded ``pytest itest_tests``, so a test registered via `itest add`
under any other path — the whole Ring-3 adoption use case `itest add` exists for
— was never collected. Its outcome fell through to ``missing``, the point rolled
up as a passing-looking ``[STUB]``, and verify exited 0. A test the user believes
is guarding an integration silently did not run.
"""

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
def synced_project(tmp_path, monkeypatch) -> Path:
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["sync", "--auto-approve", "--tf-json", str(FIXTURE)])
    return tmp_path


def _register(base_dir: Path, point_id: str, rel: str, func: str) -> None:
    from itest.core.manifest import TestEntry, load_manifest, save_manifest

    manifest_path = base_dir / ".itest" / "manifest.yaml"
    manifest = load_manifest(manifest_path)
    manifest.tests.append(
        TestEntry(
            id=f"t-{func}",
            point_id=point_id,
            path=rel,
            test_name=func,
            ownership_hash="0" * 64,
            status="implemented",
        )
    )
    save_manifest(manifest, manifest_path)


def test_registered_test_outside_itest_tests_actually_runs(
    synced_project: Path,
) -> None:
    """A failing test registered under tests_scratch/ must actually run: its
    failure has to surface (exit 1, listed as failing), not be masked as a STUB
    at exit 0 because verify only ran itest_tests/."""
    outside = synced_project / "tests_scratch"
    outside.mkdir()
    (outside / "test_alb.py").write_text(
        "def test_alb_health():\n    assert False, 'boom: the guard did not hold'\n"
    )

    from itest.core.manifest import load_manifest

    manifest = load_manifest(synced_project / ".itest" / "manifest.yaml")
    point_id = next(
        t.point_id
        for t in manifest.tests
        if t.test_name == "test_sg_internet_to_alb_443"
    )
    _register(synced_project, point_id, "tests_scratch/test_alb.py", "test_alb_health")

    result = runner.invoke(app, ["verify"])

    # The registered outside test ran and failed, so the suite fails.
    assert result.exit_code == 1, result.output
    assert "tests_scratch/test_alb.py::test_alb_health" in result.output
    assert "1 failing" in result.output

    # And it is reflected as a real outcome, not "missing".
    payload = json.loads(runner.invoke(app, ["verify", "--output", "json"]).output)
    entry = next(
        t
        for t in payload["tests"]
        if t["canonical"] == "tests_scratch/test_alb.py::test_alb_health"
    )
    assert entry["outcome"] == "failed", payload


def test_unrelated_tests_outside_registered_paths_are_not_collected(
    synced_project: Path,
) -> None:
    """The flip side: verify must NOT collect the customer's unrelated tests.

    A stray test file that no manifest entry registers is never run — targeting
    the registered file paths (not a parent directory) is what keeps verify from
    sweeping up the whole project.
    """
    stray = synced_project / "tests_scratch"
    stray.mkdir()
    (stray / "test_unrelated.py").write_text(
        "def test_unrelated():\n    assert False, 'must never run'\n"
    )

    result = runner.invoke(app, ["verify"])
    # The stray failure is invisible: it was never a registered target.
    assert result.exit_code == 0, result.output
    assert "test_unrelated" not in result.output
