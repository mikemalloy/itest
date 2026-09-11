"""The reference-mcp example runs from its own directory, as shipped.

Every other test that touches ``examples/reference-mcp`` rewrites it into a
temporary checkout. These read the committed files where they are, because
what a first-time reader clones is what has to work.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from itest.core import environments, planner
from itest.core.declarations import load_declarations
from itest.core.declarations.tools import build_target

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_DIR = REPO_ROOT / "examples" / "reference-mcp"
POLICY_FILE = EXAMPLE_DIR / ".itest" / "environments.yaml"
DECLARATION_FILE = EXAMPLE_DIR / ".itest" / "tools" / "reference-mcp.yaml"


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
    (declaration,) = load_declarations(EXAMPLE_DIR)
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
    (declaration,) = load_declarations(EXAMPLE_DIR)
    target = build_target(declaration, EXAMPLE_DIR)
    assert target is not None
    assert target.command[1:] == ["server.py"]
    assert target.cwd == str(EXAMPLE_DIR.resolve())


def test_a_bare_python_is_the_interpreter_itest_runs_under() -> None:
    """Not the first `python` on PATH: that one may not have the server's
    dependencies, and the venv's `itest` is often run without activating it."""
    (declaration,) = load_declarations(EXAMPLE_DIR)
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

    points, orphaned, unreachable = planner.plan_declarations(example)
    assert unreachable == {}
    assert len(points) == 8
    assert orphaned == []
