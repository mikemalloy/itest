"""The reference-mcp example runs from its own directory, as shipped.

Every other test that touches ``examples/reference-mcp`` rewrites it into a
temporary checkout. These read the committed files where they are, because
what a first-time reader clones is what has to work.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from itest.core import environments
from itest.core.declarations import load_declarations

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_DIR = REPO_ROOT / "examples" / "reference-mcp"
POLICY_FILE = EXAMPLE_DIR / ".itest" / "environments.yaml"


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
