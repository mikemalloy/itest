"""Pin the PR gate: `itest plan` on the reference example, and nothing else.

`.github/workflows/itest-plan.yml` runs on every pull request and every push
to main, so its shape is a promise: no secret, no node, no promptfoo, no
network past the stdio subprocess plan launches — seconds, not minutes. This
parses the workflow and holds it to that, then runs its plan step for real
against a copy of the example, the way the runner would.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "itest-plan.yml"
EXAMPLE_DIR = REPO_ROOT / "examples" / "reference-mcp"

JOB_NAME = "itest plan (reference-mcp)"


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _triggers(workflow: dict) -> dict:
    # PyYAML resolves the bare key `on` to the boolean True (YAML 1.1).
    return workflow[True] if True in workflow else workflow["on"]


def _the_job() -> dict:
    (job,) = _workflow()["jobs"].values()
    return job


def _run_lines(job: dict) -> list[str]:
    lines: list[str] = []
    for step in job["steps"]:
        lines.extend(
            line.strip() for line in (step.get("run") or "").splitlines() if line
        )
    return lines


def _itest_commands(job: dict) -> list[list[str]]:
    commands = []
    for line in _run_lines(job):
        for part in re.split(r"&&|\|\||;", line):
            argv = shlex.split(part.strip())
            if argv and argv[0] == "itest":
                commands.append(argv[1:])
    return commands


def test_it_runs_on_pull_requests_and_pushes_to_main_only() -> None:
    triggers = _triggers(_workflow())
    assert set(triggers) == {"pull_request", "push"}
    assert triggers["push"] == {"branches": ["main"]}


def test_it_references_no_secret_no_node_and_no_promptfoo() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "secrets." not in text
    assert "ANTHROPIC" not in text
    assert "setup-node" not in text
    assert "npx" not in text
    assert "promptfoo" not in text.lower()


def test_it_needs_only_read_access_to_the_checkout() -> None:
    assert _workflow()["permissions"] == {"contents": "read"}


def test_one_job_named_for_the_checks_list_on_ubuntu_with_python_312() -> None:
    job = _the_job()
    assert job["name"] == JOB_NAME
    assert job["runs-on"] == "ubuntu-latest"
    assert 0 < job["timeout-minutes"] <= 10
    (setup,) = [s for s in job["steps"] if "setup-python" in str(s.get("uses", ""))]
    assert setup["with"]["python-version"] == "3.12"
    assert any('pip install -e ".[examples]"' in line for line in _run_lines(job)), (
        _run_lines(job)
    )


def test_it_invokes_exactly_itest_plan_from_the_example_directory() -> None:
    job = _the_job()
    assert _itest_commands(job) == [["plan"]]
    (plan_line,) = [line for line in _run_lines(job) if "itest plan" in line]
    assert plan_line == "cd examples/reference-mcp && itest plan"


def _plan_step() -> dict:
    (step,) = [s for s in _the_job()["steps"] if "itest plan" in (s.get("run") or "")]
    return step


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_the_plan_step_passes_on_the_shipped_example(tmp_path: Path) -> None:
    """The step's own script, run from a checkout that holds the example as
    shipped: the server is reachable over stdio, so plan exits 0. A declaration
    the live listing contradicts would exit 2 and fail the gate."""
    shutil.copytree(
        EXAMPLE_DIR,
        tmp_path / "examples" / "reference-mcp",
        ignore=shutil.ignore_patterns("__pycache__", "itest_tests", "manifest.yaml"),
    )
    venv_bin = Path(sys.executable).parent
    result = subprocess.run(
        ["bash", "-e", "-c", _plan_step()["run"]],
        cwd=tmp_path,
        env={**os.environ, "PATH": f"{venv_bin}{os.pathsep}{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "reference-mcp" in result.stdout
    assert (tmp_path / "examples" / "reference-mcp" / ".itest" / "plan.json").is_file()
