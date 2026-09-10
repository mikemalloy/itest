"""Pin the release workflow's ordering: nothing reaches PyPI without notes.

A version on PyPI can never be uploaded again, so the check that the written
release notes exist has to run *before* the publish job, not after it. This
parses `.github/workflows/release.yml`, asserts the job graph puts the check
ahead of `publish-pypi`, and executes the check's own script against a temp
checkout with and without the notes file.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"

NOTES_STEP = "Require written release notes"
TAG = "v9.9.9"


def _jobs() -> dict[str, dict]:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]


def _needs(job: dict) -> list[str]:
    needs = job.get("needs", [])
    return [needs] if isinstance(needs, str) else list(needs)


def _ancestors(jobs: dict[str, dict], name: str) -> set[str]:
    """Every job that must finish before ``name`` starts."""
    seen: set[str] = set()
    pending = _needs(jobs[name])
    while pending:
        job = pending.pop()
        if job not in seen:
            seen.add(job)
            pending.extend(_needs(jobs[job]))
    return seen


def _jobs_with_notes_step(jobs: dict[str, dict]) -> list[str]:
    return [
        name
        for name, job in jobs.items()
        if any(step.get("name") == NOTES_STEP for step in job.get("steps", []))
    ]


def _notes_step() -> dict:
    jobs = _jobs()
    (name,) = _jobs_with_notes_step(jobs)
    return next(s for s in jobs[name]["steps"] if s.get("name") == NOTES_STEP)


def test_the_notes_check_runs_before_anything_is_published() -> None:
    jobs = _jobs()
    holders = _jobs_with_notes_step(jobs)
    assert len(holders) == 1, holders
    (holder,) = holders
    assert holder in _ancestors(jobs, "publish-pypi")
    # And it does not wait on the publish itself.
    assert "publish-pypi" not in _ancestors(jobs, holder)


def test_the_notes_check_job_checks_out_the_tag() -> None:
    """The script reads docs/releases/, so its job must have the checkout."""
    jobs = _jobs()
    (holder,) = _jobs_with_notes_step(jobs)
    assert any(
        str(step.get("uses", "")).startswith("actions/checkout@")
        for step in jobs[holder]["steps"]
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_a_missing_notes_file_fails_and_says_to_add_it_and_rerun(
    tmp_path: Path,
) -> None:
    step = _notes_step()
    result = subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        cwd=tmp_path,
        env={**os.environ, "TAG": TAG},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 1
    message = result.stdout + result.stderr
    assert f"docs/releases/{TAG}.md is missing" in message
    assert "re-run" in message
    assert "re-tag" not in message


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_a_present_notes_file_passes(tmp_path: Path) -> None:
    (tmp_path / "docs" / "releases").mkdir(parents=True)
    (tmp_path / "docs" / "releases" / f"{TAG}.md").write_text("# notes\n")
    result = subprocess.run(
        ["bash", "-e", "-c", _notes_step()["run"]],
        cwd=tmp_path,
        env={**os.environ, "TAG": TAG},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
