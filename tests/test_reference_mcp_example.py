"""The reference-mcp example runs from its own directory, as shipped.

Every other test that touches ``examples/reference-mcp`` rewrites it into a
temporary checkout. These read the committed files where they are, because
what a first-time reader clones is what has to work.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from itest.cli import app
from itest.core import environments, planner, stubgen
from itest.core.declarations import load_declarations
from itest.core.declarations.tools import build_target
from itest.core.manifest import load_manifest
from itest.report.render import extract_blocks

runner = CliRunner()

#: The reference server's eight tools (pinned in tests/test_reference_mcp.py).
TOOLS = (
    "get_guide",
    "search_records",
    "fetch_record",
    "create_record",
    "update_record",
    "delete_record",
    "lookalike_read",
    "enrich",
)

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_DIR = REPO_ROOT / "examples" / "reference-mcp"
POLICY_FILE = EXAMPLE_DIR / ".itest" / "environments.yaml"
DECLARATION_FILE = EXAMPLE_DIR / ".itest" / "tools" / "reference-mcp.yaml"
OPEN_DECLARATION_FILE = EXAMPLE_DIR / ".itest" / "tools" / "reference-mcp-open.yaml"

_spec = importlib.util.spec_from_file_location(
    "reference_mcp_server_example", EXAMPLE_DIR / "server.py"
)
assert _spec and _spec.loader
reference_mcp = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = reference_mcp
_spec.loader.exec_module(reference_mcp)


@pytest.fixture(scope="module")
def http_server() -> Iterator[Any]:
    """Terminal one of the two-terminal demo: `python server.py --http`."""
    with reference_mcp.serve_in_thread(token="dry-run-token") as running:
        yield running


def _stdio_declaration():
    """The stdio declaration, beside the open-mount one the example also ships."""
    return next(
        d for d in load_declarations(EXAMPLE_DIR) if d.server == "reference-mcp"
    )


def copy_example(destination: Path) -> Path:
    """A clean copy of the committed example: what a user has after cloning."""
    shutil.copytree(
        EXAMPLE_DIR,
        destination,
        ignore=shutil.ignore_patterns("__pycache__", "itest_tests", "manifest.yaml"),
    )
    for generated in ("plan.json", "diagram.mmd", "environment"):
        (destination / ".itest" / generated).unlink(missing_ok=True)
    return destination


def no_python_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """PATH holding no interpreter at all — nor terraform. What the example
    launches must not depend on which `python` a user's shell finds first."""
    empty = tmp_path / "empty-bin"
    empty.mkdir(exist_ok=True)
    monkeypatch.setenv("PATH", str(empty))


# --- the environment policy ---------------------------------------------------


def test_the_example_ships_an_environment_policy() -> None:
    policy = environments.load_policy(POLICY_FILE)
    assert policy.environments["staging"].tiers == ["static", "readonly", "active"]
    assert policy.environments["prod"].tiers == ["static", "readonly"]
    assert policy.environments["prod"].production is True


def test_the_policy_permits_active_in_staging_and_refuses_it_in_prod() -> None:
    assert environments.resolve(EXAMPLE_DIR, override="staging").allows("active")
    prod = environments.resolve(EXAMPLE_DIR, override="prod")
    assert not prod.allows("active")
    assert prod.production


def test_the_example_declaration_loads_against_its_own_policy() -> None:
    """`active_allowed_in: [staging]` is refused without a policy permitting it;
    with the shipped one it loads."""
    declaration = _stdio_declaration()
    assert declaration.environments.active_allowed_in == ["staging"]


def test_the_example_policy_is_tracked_by_git() -> None:
    """`.itest/` is gitignored everywhere, so a file there must be force-added.
    Present on disk but untracked passes here and fails for everyone who clones."""
    if not (REPO_ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    tracked = subprocess.run(
        ["git", "ls-files", "--", str(EXAMPLE_DIR.relative_to(REPO_ROOT))],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert "examples/reference-mcp/.itest/environments.yaml" in tracked
    assert "examples/reference-mcp/.itest/tools/reference-mcp.yaml" in tracked
    assert "examples/reference-mcp/.itest/tools/reference-mcp-open.yaml" in tracked


# --- the transport command ----------------------------------------------------


def test_the_shipped_command_names_the_server_beside_the_project() -> None:
    document = yaml.safe_load(DECLARATION_FILE.read_text(encoding="utf-8"))
    assert document["transport"]["command"] == ["python", "server.py"]
    assert "repo root" not in DECLARATION_FILE.read_text(encoding="utf-8")


def test_a_stdio_target_launches_in_the_declarations_project_directory(
    tmp_path: Path,
) -> None:
    """Relative argv resolves against the directory holding `.itest/`, not the
    process's cwd and not the repository root."""
    declaration = _stdio_declaration()
    target = build_target(declaration, EXAMPLE_DIR)
    assert target is not None
    assert target.command[1:] == ["server.py"]
    assert target.cwd == str(EXAMPLE_DIR.resolve())


def test_a_bare_python_is_the_interpreter_itest_runs_under() -> None:
    """Not the first `python` on PATH: that one may not have the server's
    dependencies, and the venv's `itest` is often run without activating it."""
    declaration = _stdio_declaration()
    target = build_target(declaration, EXAMPLE_DIR)
    assert target is not None
    assert target.command[0] == sys.executable
    for word in ("python", "python3"):
        stated = declaration.model_copy(deep=True)
        stated.transport.command = [word, "server.py"]
        assert build_target(stated, EXAMPLE_DIR).command[0] == sys.executable
    other = declaration.model_copy(deep=True)
    other.transport.command = ["/opt/bin/python3.11", "server.py"]
    assert build_target(other, EXAMPLE_DIR).command[0] == "/opt/bin/python3.11"


def test_plan_launches_the_example_server_from_any_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`server.py` is found beside the project, whatever the cwd is — and with
    no `python` on PATH at all."""
    example = copy_example(tmp_path / "reference-mcp")
    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    no_python_on_path(tmp_path, monkeypatch)
    assert not os.environ["PATH"].startswith(str(Path(sys.executable).parent))

    points, orphaned, unreachable, private_hosts = planner.plan_declarations(example)
    # The open mount's url is unset here: named as unreachable, never a crash.
    assert unreachable == {
        "reference-mcp-open": "unreachable: REFERENCE_MCP_OPEN_URL not set"
    }
    assert private_hosts == ["reference-mcp-open"]
    assert len(points) == 8
    assert {p.source for p in points} == {"reference-mcp"}
    assert orphaned == []


# --- the contract: the example stays runnable ---------------------------------

SERVER_DIR = "itest_tests/tools_reference_mcp"
ENGINE = f"{SERVER_DIR}/test_reference_mcp__engine.py"
ENGINE_ACTIVE = f"{SERVER_DIR}/test_reference_mcp__engine_active.py"
GENERATED_ACTIVE = f"{SERVER_DIR}/test_reference_mcp__generated_active.py"
CONFTEST = f"{SERVER_DIR}/conftest.py"
OPEN_DIR = "itest_tests/tools_reference_mcp_open"
OPEN_ENGINE = f"{OPEN_DIR}/test_reference_mcp_open__engine.py"
OPEN_ENGINE_ACTIVE = f"{OPEN_DIR}/test_reference_mcp_open__engine_active.py"
OPEN_GENERATED_ACTIVE = f"{OPEN_DIR}/test_reference_mcp_open__generated_active.py"
OPEN_CONFTEST = f"{OPEN_DIR}/conftest.py"


def _ledger_checks(blocks: dict) -> list[tuple[str, str, str]]:
    """(server group heading, tool, status token) for every cell on the page."""
    return [
        (group["g"], row["n"], cell["txt"])
        for group in blocks["TOOLS"]
        for row in group["rows"]
        for cell in row["cells"]
        if cell
    ]


@pytest.mark.slow
def test_reference_mcp_runs_plan_sync_verify_report_from_its_own_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, http_server: Any
) -> None:
    """The two-terminal demo, as the README describes it: `server.py --http`
    in one terminal, both urls exported in the other, then the four commands a
    first-time reader runs from a clean copy of the example — no --tf-json, no
    terraform, no `python` on PATH, no stub file made by hand, no edit to
    anything shipped. The stdio server and the open HTTP mount are both probed."""
    example = copy_example(tmp_path / "reference-mcp")
    monkeypatch.chdir(example)
    no_python_on_path(tmp_path, monkeypatch)
    monkeypatch.setenv("REFERENCE_MCP_TOKEN", "dry-run-token")
    monkeypatch.setenv("REFERENCE_MCP_OPEN_URL", http_server.open_url)

    # plan: eight tools on each of the two servers, nothing unreachable, and
    # the open mount named for loosening the private-host guard.
    plan = runner.invoke(app, ["plan", "--output", "json"])
    assert plan.exit_code == 0, plan.output
    payload = json.loads(plan.output)
    assert payload["unreachable_servers"] == {}
    assert payload["private_hosts_allowed"] == ["reference-mcp-open"]
    for server in ("reference-mcp", "reference-mcp-open"):
        targets = [p["target"] for p in payload["new_points"] if p["source"] == server]
        assert sorted(targets) == sorted(TOOLS), server
    assert {p["type"] for p in payload["new_points"]} == {"mcp_tool"}
    human = runner.invoke(app, ["plan"])
    assert "private hosts allowed by declaration" in human.output

    # sync: answered at the prompt, as a person runs it.
    sync = runner.invoke(app, ["sync"], input="y\n")
    assert sync.exit_code == 0, sync.output
    assert "Applied:" in sync.output

    # The generated tree is exactly what sync writes, and nothing else. The open
    # mount declares no second tenant or audit sink, and its identity defaults
    # to passthrough, so its generated bindings are the two identity checks.
    written = sorted(
        path.relative_to(example).as_posix()
        for path in (example / "itest_tests").rglob("*.py")
    )
    assert written == sorted(
        [
            CONFTEST,
            ENGINE,
            ENGINE_ACTIVE,
            GENERATED_ACTIVE,
            OPEN_CONFTEST,
            OPEN_ENGINE,
            OPEN_ENGINE_ACTIVE,
            OPEN_GENERATED_ACTIVE,
        ]
    )
    manifest = load_manifest(example / ".itest" / "manifest.yaml")
    assert {t.path for t in manifest.tests} == {
        ENGINE,
        ENGINE_ACTIVE,
        GENERATED_ACTIVE,
        OPEN_ENGINE,
        OPEN_ENGINE_ACTIVE,
        OPEN_GENERATED_ACTIVE,
    }
    assert len([p for p in manifest.points if p.type == "mcp_tool"]) == 16
    for path in written:  # no hand-written stub anywhere
        text = (example / path).read_text(encoding="utf-8")
        assert stubgen.STUB_SKIP_LINE not in text, path
    # The anonymous check is planned only where an anonymous caller exists.
    planned = {(p.source, p.target): p.traits_planned for p in manifest.points}
    assert all(
        "authority.anonymous" not in planned[("reference-mcp", t)] for t in TOOLS
    )
    assert all(
        "authority.anonymous" in planned[("reference-mcp-open", t)] for t in TOOLS
    )

    # verify in staging: the suite runs; the open mount's findings are real
    # failures (an anonymous call answered on a read tool), never errors.
    verify = runner.invoke(app, ["verify", "--environment", "staging"])
    assert verify.exit_code == 1, verify.output
    assert "16 integration points" in verify.output
    assert "0 errored" in verify.output
    assert "gated" not in verify.output
    assert "reference-mcp-open -> " in verify.output

    # report: a page naming every tool under a verdict band.
    page = tmp_path / "readiness.html"
    # The page reflects the environment verified: report runs its verify in
    # staging too, so the active-tier checks are attempted, never held out.
    report = runner.invoke(
        app, ["report", "--html", "--environment", "staging", "--out", str(page)]
    )
    assert report.exit_code == 0, report.output
    html = page.read_text(encoding="utf-8")
    for tool in TOOLS:
        assert tool in html, tool
    blocks = extract_blocks(html)
    verdict = blocks["PAGE"]["verdict"]
    assert verdict["word"] == "BLOCKED"  # the open mount's anonymous reads
    assert f"Verdict: {verdict['word']}" in report.output
    assert 'class="verdict' in html
    assert "<b>staging</b>" in verdict["sub"]
    assert "no environment bound" not in verdict["sub"]
    cells = [status for _group, _tool, status in _ledger_checks(blocks)]
    assert cells and "HELD OUT" not in cells
    assert "NOT RUN" in cells or "NOT VERIFIABLE" in cells  # active tier attempted
    assert "FAIL" in cells
    # Every cell carries a standards id, and the band names what is not covered.
    headers = [h for group in blocks["TOOLS"] for h in group["headers"]]
    assert headers and all(h["std"] for h in headers)
    band = blocks["PAGE"]["standards"]
    assert [r["id"] for r in band["rows"]] == [f"ASI{n:02d}" for n in range(1, 11)]
    assert {r["label"] for r in band["rows"]} >= {"covered", "not covered"}


@pytest.mark.slow
def test_reference_mcp_stdio_only_needs_allow_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the HTTP server the open mount's url is unset: plan names it,
    sync refuses until --allow-unreachable accepts the gap, and the stdio run
    is then green — there is no anonymous cell to fail over stdio."""
    example = copy_example(tmp_path / "reference-mcp")
    monkeypatch.chdir(example)
    monkeypatch.setenv("REFERENCE_MCP_TOKEN", "dry-run-token")
    monkeypatch.delenv("REFERENCE_MCP_OPEN_URL", raising=False)

    plan = runner.invoke(app, ["plan"])
    assert plan.exit_code == 0, plan.output
    assert "unreachable: REFERENCE_MCP_OPEN_URL not set" in plan.output

    refused = runner.invoke(app, ["sync", "--auto-approve"])
    assert refused.exit_code == 1, refused.output
    assert "reference-mcp-open" in refused.output
    assert not (example / ".itest" / "manifest.yaml").exists()

    sync = runner.invoke(app, ["sync", "--auto-approve", "--allow-unreachable"])
    assert sync.exit_code == 0, sync.output
    manifest = load_manifest(example / ".itest" / "manifest.yaml")
    assert len(manifest.points) == 8
    verify = runner.invoke(app, ["verify", "--environment", "staging"])
    # 1: lookalike_read is caught mutating behind readOnlyHint (active tier).
    assert verify.exit_code == 1, verify.output
    assert "1 failing" in verify.output and "0 errored" in verify.output
    assert "[FAIL] reference-mcp -> lookalike_read" in verify.output
    assert "authority.anonymous" not in verify.output


# --- a dry run leaves the repo's own suite and status alone -----------------------


def test_the_project_suite_is_only_tests() -> None:
    """A bare `pytest` from the repo root collects tests/ and nothing else — not
    the itest_tests/ a dry run of an example generates."""
    import tomllib

    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    config = tomllib.loads(text)
    assert config["tool"]["pytest"]["ini_options"]["testpaths"] == ["tests"]


GENERATED = (
    "examples/reference-mcp/itest_tests/tools_reference_mcp/test_x.py",
    "examples/reference-mcp/itest_tests/tools_reference_mcp/conftest.py",
    "examples/reference-mcp/.itest/manifest.yaml",
    "examples/reference-mcp/.itest/plan.json",
    "examples/reference-mcp/.itest/diagram.mmd",
    "examples/reference-mcp/.itest/environment",
    "examples/reference-mcp/readiness.html",
    "examples/reference-api/itest_tests/test_route_edges.py",
)


@pytest.mark.parametrize("path", GENERATED)
def test_what_a_dry_run_generates_is_gitignored(path: str) -> None:
    if not (REPO_ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", "--no-index", path], cwd=REPO_ROOT
    )
    assert ignored.returncode == 0, f"{path} is not ignored"
