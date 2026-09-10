"""Execute the example post-apply workflow's steps, and pin it to them.

`.github/workflows/pipeline-example.yml` is a template customers copy, so it is
`workflow_dispatch` only and never runs in ITest's own CI. That would leave it
free to rot, so this test does two things:

1. Runs the four `itest` commands the workflow runs, in order, against the same
   committed fixture in a temp directory — no cloud account, no network — and
   asserts the exit codes and the artifacts the workflow's comments claim.
2. Parses the workflow and asserts every `itest` command in it is one of the
   commands step 1 executed. A command that changes in the workflow and not
   here (or the reverse) fails the suite: the example and the CLI cannot drift.
"""

from __future__ import annotations

import json
import re
import shlex
import string
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from itest.cli import app

runner = CliRunner()

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "pipeline-example.yml"
POLICY = REPO_ROOT / "docs" / "examples" / "environments.yaml"
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "alex" / "alex-s6.json"

#: The environment the example binds to, and the one its policy defines.
ENVIRONMENT = "example"


def _run(argv: list[str]) -> tuple[int, str]:
    result = runner.invoke(app, argv)
    if result.exception is not None and not isinstance(result.exception, SystemExit):
        raise result.exception
    return result.exit_code, result.output


@pytest.fixture
def example_run(tmp_path, monkeypatch) -> dict:
    """Run the workflow's steps in a temp dir; return what they produced."""
    monkeypatch.chdir(tmp_path)

    # Step "terraform show -json": the example copies a committed fixture.
    (tmp_path / "state.json").write_text(FIXTURE.read_text(encoding="utf-8"))
    # Step "Environment policy": the committed half of the gate, copied into
    # place because the fixture arrives without a .itest/ directory.
    (tmp_path / ".itest").mkdir()
    (tmp_path / ".itest" / "environments.yaml").write_text(
        POLICY.read_text(encoding="utf-8")
    )

    executed: list[list[str]] = []
    outputs: dict[str, str] = {}
    for name, argv in (
        ("plan", ["plan", "--tf-json", "state.json"]),
        ("sync", ["sync", "--auto-approve"]),
        ("verify", ["verify", "--environment", ENVIRONMENT, "--output", "json"]),
        (
            "report",
            ["report", "--html", "--from", "verify.json", "--out", "readiness.html"],
        ),
    ):
        if name == "report":
            # The workflow redirects verify's stdout; do the same before the
            # step that reads the file.
            (tmp_path / "verify.json").write_text(outputs["verify"])
        code, output = _run(argv)
        outputs[name] = output
        outputs[f"{name}_exit"] = code
        executed.append(argv)

    return {"dir": tmp_path, "executed": executed, **outputs}


def test_example_steps_exit_as_documented(example_run: dict) -> None:
    # plan reads the state; sync consumes the plan it wrote, with no terraform.
    assert example_run["plan_exit"] == 0, example_run["plan"]
    assert example_run["sync_exit"] == 0, example_run["sync"]
    assert "added 14 stub(s)" in example_run["sync"]
    # A stub-only run is not a failure: verify exits 0, which is why the
    # workflow's `|| true` is belt-and-braces rather than load-bearing here.
    assert example_run["verify_exit"] == 0, example_run["verify"]
    # report always exits 0 when the page renders — the verdict is not an exit
    # code, and this is the assertion that keeps it that way.
    assert example_run["report_exit"] == 0, example_run["report"]


def test_verify_json_parses_and_is_stub_only(example_run: dict) -> None:
    document = json.loads(example_run["verify"])
    assert document["environment"] == ENVIRONMENT
    assert document["total_points"] == 14
    assert document["passing"] == 0
    assert document["stubs"] == 14
    assert document["gated"] == 0


def test_rendered_page_is_at_risk_with_named_empty_states(example_run: dict) -> None:
    page = (example_run["dir"] / "readiness.html").read_text(encoding="utf-8")
    # docs/report.md: a stub-only run is AT RISK, never VERIFIED — a stub is
    # not coverage, so a green stamp over "0 of 14 verified" must never print.
    assert "AT RISK" in page
    assert "VERIFIED" not in page
    assert "0 of 14 integration points verified" in example_run["report"]
    # An absent source renders empty-but-named, never as sample data.
    assert "No agent tools declared" in page


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _normalize(line: str, env: dict[str, str]) -> list[str]:
    """One shell line from a `run:` block as the argv `itest` would receive."""
    line = line.split("||")[0]  # `|| true`
    line = re.sub(r">\s*\S+", "", line)  # a stdout redirection
    line = string.Template(line).safe_substitute(env)  # $VAR and ${VAR}
    argv = shlex.split(line)
    assert argv[0] == "itest"
    return argv[1:]


def _itest_commands(workflow: dict) -> list[list[str]]:
    commands: list[list[str]] = []
    for job in workflow["jobs"].values():
        env = {k: str(v) for k, v in (job.get("env") or {}).items()}
        for step in job["steps"]:
            for line in (step.get("run") or "").splitlines():
                line = line.strip()
                if line.startswith("itest "):
                    commands.append(_normalize(line, env))
    return commands


def test_workflow_itest_commands_are_the_executed_ones(example_run: dict) -> None:
    commands = _itest_commands(_workflow())
    assert commands, "the example workflow runs no itest commands"
    executed = example_run["executed"]
    assert commands == executed, (
        "the example workflow and this test have drifted:\n"
        f"  workflow: {commands}\n"
        f"  executed: {executed}"
    )


def test_workflow_is_a_template_not_a_gate() -> None:
    workflow = _workflow()
    # PyYAML resolves the bare key `on` to the boolean True (YAML 1.1).
    triggers = workflow[True] if True in workflow else workflow["on"]
    assert set(triggers) == {"workflow_dispatch"}, (
        "the example is a template customers copy; a push or pull_request "
        "trigger would make it one of ITest's own gates"
    )
    assert workflow["permissions"] == {"contents": "read"}


def test_workflow_reads_the_files_this_test_reads() -> None:
    """The two non-itest steps that feed the run must name the same files."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert str(FIXTURE.relative_to(REPO_ROOT)) in text
    assert str(POLICY.relative_to(REPO_ROOT)) in text
