"""Pin the scheduled red-team job: promptfoo → itest sync → verify → report.

`.github/workflows/redteam-nightly.yml` is the one workflow in this repository
that spends money and holds a secret, so its shape is held to a few rules the
job's comments state and this file enforces: the key reaches the job as an
environment variable from `secrets.` and appears nowhere else — never in a
`run:` line; promptfoo, node and the model ids are pinned and promptfoo writes
no cache; the artifact is exactly the results file and the page; verify's
exit code can never fail the job, because the job gates nothing; and the
`itest` commands it runs are the ones this test executes for real against a
copy of the example with the committed 2026-09-23 results, so the workflow and
the CLI cannot drift.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import string
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from test_reference_mcp_example import copy_example

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "redteam-nightly.yml"
EXAMPLE_DIR = REPO_ROOT / "examples" / "reference-mcp"
FIXTURE = (
    REPO_ROOT
    / "tests"
    / "fixtures"
    / "evidence"
    / "promptfoo-reference-mcp-2026-09-23.json"
)

SECRET = "ANTHROPIC_API_KEY"
RESULTS_ENV = "ITEST_PROMPTFOO_RESULTS"
PROMPTFOO = "promptfoo@0.123.1"
ARTIFACTS = ["redteam-results.json", "readiness.html"]


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _triggers(workflow: dict) -> dict:
    # PyYAML resolves the bare key `on` to the boolean True (YAML 1.1).
    return workflow[True] if True in workflow else workflow["on"]


def _the_job() -> dict:
    (job,) = _workflow()["jobs"].values()
    return job


def _steps_named(job: dict, word: str) -> list[dict]:
    return [s for s in job["steps"] if word in str(s.get("name", "")).lower()]


def _run_text(job: dict) -> str:
    return "\n".join(step.get("run") or "" for step in job["steps"])


# --- the secret ---------------------------------------------------------------


def test_the_key_reaches_the_job_as_an_env_var_from_secrets_and_nowhere_else() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert text.count("secrets.") == 1
    job = _the_job()
    assert job["env"][SECRET] == "${{ secrets.ANTHROPIC_API_KEY }}"
    # Never in a run: line, never as an argument, never echoed.
    assert SECRET not in _run_text(job)
    for step in job["steps"]:
        assert SECRET not in yaml.safe_dump(step.get("with") or {})
        assert SECRET not in yaml.safe_dump(step.get("env") or {})


def test_it_is_scheduled_daily_and_runnable_on_demand_one_at_a_time() -> None:
    workflow = _workflow()
    triggers = _triggers(workflow)
    assert set(triggers) == {"schedule", "workflow_dispatch"}
    assert triggers["schedule"] == [{"cron": "0 9 * * *"}]
    assert workflow["concurrency"]["cancel-in-progress"] is False
    assert workflow["permissions"] == {"contents": "read"}


def test_run_steps_use_bash_so_a_tee_pipeline_keeps_itest_exit_code() -> None:
    """GitHub's default `run` shell is `bash -e` without pipefail, under
    which `itest sync | tee` succeeds whatever sync did. Naming `bash` as the
    shell is what gets `-eo pipefail` (documented), and the join step's
    `|| echo` depends on it."""
    assert _workflow()["defaults"]["run"]["shell"] == "bash"


def _shell_options() -> list[str]:
    """The options GitHub passes for the workflow's configured shell: for
    `bash`, `--noprofile --norc -eo pipefail`. Anything else is a failure
    here rather than a guess."""
    shell = _workflow().get("defaults", {}).get("run", {}).get("shell")
    assert shell == "bash", f"unhandled shell {shell!r}: this test knows bash"
    return ["bash", "--noprofile", "--norc", "-eo", "pipefail"]


def test_it_never_runs_on_a_pull_request_or_a_push() -> None:
    triggers = _triggers(_workflow())
    assert "pull_request" not in triggers
    assert "push" not in triggers


def test_the_job_is_bounded_pinned_and_named() -> None:
    job = _the_job()
    assert job["name"] == "redteam (reference-mcp)"
    assert job["runs-on"] == "ubuntu-latest"
    assert job["timeout-minutes"] == 20
    (python,) = [s for s in job["steps"] if "setup-python" in str(s.get("uses"))]
    assert python["with"]["python-version"] == "3.12"
    (node,) = [s for s in job["steps"] if "setup-node" in str(s.get("uses"))]
    assert node["with"]["node-version"] == "22"
    assert 'pip install -e ".[examples]"' in _run_text(job)
    for step in job["steps"]:
        uses = str(step.get("uses", ""))
        if uses:
            assert re.fullmatch(r"[\w./-]+@v\d+", uses), uses


def test_the_environment_pins_the_models_and_names_the_results_file() -> None:
    env = _the_job()["env"]
    assert env["AGENT_MODEL"] == "claude-sonnet-5"
    assert env["AGENT_MODEL_2"] == "claude-haiku-4-5-20251001"
    assert env["PROMPTFOO_DISABLE_TELEMETRY"] == 1
    assert env[RESULTS_ENV] == "${{ github.workspace }}/redteam-results.json"


# --- the red-team step ----------------------------------------------------------


def test_the_red_team_step_runs_pinned_promptfoo_without_a_cache() -> None:  # noqa: E501
    (step,) = _steps_named(_the_job(), "red team")
    run = " ".join(step["run"].split())
    assert run.startswith("cd examples/reference-mcp/redteam && npx -y " + PROMPTFOO)
    assert "eval -c promptfooconfig.yaml" in run
    assert f'-o "${RESULTS_ENV}"' in run
    assert "--no-cache" in run
    assert "promptfoo@" not in run.replace(PROMPTFOO, "")


# --- the join, verify and report ------------------------------------------------


def test_verify_cannot_fail_the_job() -> None:
    (step,) = _steps_named(_the_job(), "join")
    (verify_line,) = [
        line.strip() for line in step["run"].splitlines() if "itest verify" in line
    ]
    # `|| true` swallows the code; `|| echo` reports it. Either keeps the job
    # green, which is the point: the code is information, not a gate.
    assert re.search(r"\|\|\s*(true|echo\b)", verify_line), verify_line
    assert step.get("continue-on-error", False) is False  # `||` is enough


def test_the_artifact_is_exactly_the_results_file_and_the_page() -> None:
    job = _the_job()
    (upload,) = [s for s in job["steps"] if "upload-artifact" in str(s.get("uses"))]
    assert upload["uses"] == "actions/upload-artifact@v4"
    paths = [line.strip() for line in upload["with"]["path"].splitlines() if line]
    assert paths == [f"${{{{ github.workspace }}}}/{name}" for name in ARTIFACTS]
    assert upload["with"]["retention-days"] == 30
    # No glob anywhere near the harness or a promptfoo directory.
    assert "*" not in upload["with"]["path"]
    assert "if-no-files-found" in upload["with"]


def test_the_last_step_prints_the_story_to_the_log() -> None:
    job = _the_job()
    last = job["steps"][-1]
    assert "summary" in last["name"].lower()
    assert last.get("if") == "always()"
    assert "evidence promptfoo-ci" in last["run"]
    # The verdict word, the Answer's sentence, and its lines: the run page
    # reads the way the html does.
    assert "Verdict:" in last["run"]
    assert "Not run here:" in last["run"]
    assert "Red team" in last["run"]


# --- the itest commands, executed -------------------------------------------------


def _normalize(line: str, env: dict[str, str]) -> list[str]:
    """One shell line from a `run:` block as the argv `itest` would receive."""
    line = line.split("||")[0]  # `|| true`, `|| echo ...`
    line = line.split("|")[0]  # `| tee file`
    line = line.replace("2>&1", "")
    line = re.sub(r">\s*\S+", "", line)  # a stdout redirection
    line = re.sub(r"\$\{\{\s*github\.workspace\s*\}\}", env["GITHUB_WORKSPACE"], line)
    line = string.Template(line).safe_substitute(env)  # $VAR and ${VAR}
    argv = shlex.split(line)
    assert argv[0] == "itest"
    return argv[1:]


def _itest_commands(job: dict, env: dict[str, str]) -> list[list[str]]:
    commands: list[list[str]] = []
    for step in job["steps"]:
        for line in (step.get("run") or "").splitlines():
            line = line.strip()
            for part in line.split("&&"):
                part = part.strip()
                if part.startswith("itest "):
                    commands.append(_normalize(part, env))
    return commands


def _step(name: str) -> dict:
    (step,) = _steps_named(_the_job(), name)
    return step


def _script(step: dict, workspace: Path) -> str:
    """The step's `run:` block as the runner would execute it: expressions
    substituted; everything else is the shell's."""
    return re.sub(
        r"\$\{\{\s*github\.workspace\s*\}\}", str(workspace), step["run"]
    ).replace("${{ steps.red.outcome }}", "skipped")


@pytest.fixture(scope="module")
def job_run(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """The job's join, report and summary steps, run through bash with the
    runner's shell options, in a copy of the example with the committed results
    file standing in for promptfoo's output. The key is not needed: promptfoo
    is the one step this fixture does not run."""
    workspace = tmp_path_factory.mktemp("workspace")
    # The same copy `copy_example` makes: a clean clone, without the output a
    # local run of the example may have left behind (plan.json included — a
    # stale plan would make the join step sync a run this job never made).
    copy_example(workspace / "examples" / "reference-mcp")
    results = workspace / "redteam-results.json"
    results.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("REFERENCE_MCP_OPEN_URL", "ITEST_ENVIRONMENT")
    }
    env[RESULTS_ENV] = str(results)
    env["PATH"] = f"{Path(sys.executable).parent}{os.pathsep}{env['PATH']}"

    outputs: dict[str, object] = {"workspace": workspace}
    for name in ("join", "report", "summary"):
        result = subprocess.run(
            [*_shell_options(), "-c", _script(_step(name), workspace)],
            cwd=workspace,
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )
        outputs[name] = result.stdout + result.stderr
        outputs[f"{name}_exit"] = result.returncode
    return outputs


needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def test_the_job_runs_sync_verify_and_report_in_that_order() -> None:
    env = {"GITHUB_WORKSPACE": "/ws", RESULTS_ENV: "/ws/redteam-results.json"}
    commands = _itest_commands(_the_job(), env)
    assert commands == [
        ["sync", "--auto-approve", "--allow-unreachable"],
        ["verify"],
        ["report", "--html", "--out", "/ws/readiness.html"],
    ]


@needs_bash
@pytest.mark.slow
def test_the_join_step_syncs_the_results_and_prints_the_source_line(
    job_run: dict,
) -> None:
    assert job_run["join_exit"] == 0, job_run["join"]
    assert (
        "evidence promptfoo-ci (promptfoo): run eval-zyk-2026-09-23T18:26:29 at "
        "2026-09-23T18:26:29.565Z, 12 rows, 2 tools matched, 0 unmatched"
    ) in job_run["join"]


@needs_bash
@pytest.mark.slow
def test_the_join_step_runs_verify_on_the_safe_floor_and_it_passes(
    job_run: dict,
) -> None:
    assert "safe floor (static, readonly)" in job_run["join"]
    assert "8 integration points: 8 passing" in job_run["join"]
    assert "never enforced" not in job_run["join"]  # nothing to report: exit 0


@needs_bash
@pytest.mark.slow
def test_the_report_step_writes_the_page_with_the_evidence_lane(
    job_run: dict,
) -> None:
    assert job_run["report_exit"] == 0, job_run["report"]
    page = (job_run["workspace"] / "readiness.html").read_text(encoding="utf-8")
    assert "calls 10 · refused 0 · of 12 rows" in page  # get_guide's lane
    assert "promptfoo-ci (promptfoo)" in page
    assert "Verdict:" in job_run["report"]


@needs_bash
@pytest.mark.slow
def test_the_summary_step_tells_the_story_from_the_logs(job_run: dict) -> None:
    assert job_run["summary_exit"] == 0, job_run["summary"]
    lines = job_run["summary"].splitlines()
    assert lines[0] == "red team step: skipped"
    assert lines[1].startswith("evidence promptfoo-ci (promptfoo): run eval-zyk-")
    assert lines[2].startswith("8 integration points: 8 passing")
    assert lines[3].startswith("Verdict: PARTIAL — No findings. ")
    assert any(line.startswith("Ran: ") for line in lines[4:])
    assert any(line.startswith("Not run here: ") for line in lines[4:])
    assert any(
        line.startswith("Red team (2026-09-23): 12 attempts.") for line in lines[4:]
    )
    assert "did not run" not in job_run["summary"]
