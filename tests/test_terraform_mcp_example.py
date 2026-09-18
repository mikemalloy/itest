"""The terraform-mcp-server example's declaration loads offline.

``examples/terraform-mcp-server/`` points ITest at a server ITest did not
write — HashiCorp's, unmodified, from its own image, over streamable HTTP on a
loopback port. Nothing here touches the network: the declaration is loaded and
validated the way ``examples/reference-mcp``'s is, and the committed files are
held to the rule that matters most for a declaration in a repository: a URL
and a credential are NAMES, and neither is ever literal — not in a field, and
not in a comment either.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

from itest.core.declarations import load_declarations
from itest.core.declarations.tools import build_target

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_DIR = REPO_ROOT / "examples" / "terraform-mcp-server"
DECLARATION_FILE = EXAMPLE_DIR / ".itest" / "tools" / "terraform-mcp-server.yaml"
README_FILE = EXAMPLE_DIR / "README.md"
URL_ENV = "TF_MCP_URL"

#: What a pasted URL, host or token looks like. A scheme, a dotted quad, a
#: `host:port`, the loopback name, an HCP Terraform token prefix, a bearer
#: header, or a long run of token alphabet with no word separators at all.
_LITERALS = (
    re.compile(r"://"),
    re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"),
    re.compile(r":\d{2,5}\b"),
    re.compile(r"localhost", re.IGNORECASE),
    re.compile(r"atlasv1\."),
    re.compile(r"bearer\s+\S", re.IGNORECASE),
    re.compile(r"^[A-Za-z0-9+/=]{20,}$"),
)


def _strings(value: Any) -> list[str]:
    """Every string anywhere in a dumped declaration, keys included."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        found: list[str] = []
        for key, item in value.items():
            found.extend(_strings(key))
            found.extend(_strings(item))
        return found
    if isinstance(value, list):
        return [s for item in value for s in _strings(item)]
    return []


def _declaration():
    declarations = load_declarations(EXAMPLE_DIR)
    assert [d.server for d in declarations] == ["terraform-mcp-server"]
    return declarations[0]


# --- the declaration loads, and says only what it has to ------------------------


def test_the_declaration_loads_through_the_loader() -> None:
    """The file name is the server name; the loader accepts it as shipped, with
    no environment policy beside it — none is named, so none is needed."""
    declaration = _declaration()
    assert declaration.server == "terraform-mcp-server"
    assert not (EXAMPLE_DIR / ".itest" / "environments.yaml").exists()
    assert declaration.environments.active_allowed_in == []


def test_the_transport_is_http_by_name_with_the_private_host_opt_in() -> None:
    transport = _declaration().transport
    assert transport.kind == "http"
    assert transport.command is None
    assert transport.url_env == URL_ENV
    assert transport.allow_private_hosts is True


def test_the_opt_in_carries_the_comment_the_docs_require() -> None:
    """A declaration that loosens the SSRF guard must say, where a reviewer
    reads it, that a real deployment never needs it."""
    text = DECLARATION_FILE.read_text(encoding="utf-8")
    opt_in = text.index("allow_private_hosts: true")
    comment = text[:opt_in].rsplit("url_env:", 1)[1]
    assert "real deployment never needs it" in comment
    assert "every plan names a server that sets it" in comment


def test_no_credential_is_declared_and_no_token_is_named() -> None:
    """The registry toolset needs no token, and this example never calls a
    tool: no auth block, no credential name, and never a TFE_TOKEN."""
    auth = _declaration().auth
    assert auth.scheme == "none"
    assert auth.credential_env is None
    assert auth.second_tenant_env is None
    assert "TFE_TOKEN" not in DECLARATION_FILE.read_text(encoding="utf-8")


def test_the_declaration_is_minimal() -> None:
    """Reach only. No per-tool entry, no declared class, no facts beyond the
    transport and the sentinel: the drift catch that comes later must start
    from a clean declaration."""
    declaration = _declaration()
    assert declaration.tools == {}
    assert declaration.defaults.mutation == "detect"
    assert declaration.audit.sink is None
    assert declaration.observation.snapshot_tool is None
    assert declaration.sentinels.readable_record is None
    assert declaration.sentinels.nonexistent_id


# --- no URL and no credential, anywhere -----------------------------------------


def test_no_field_holds_a_literal_url_host_or_token() -> None:
    declaration = _declaration()
    for text in _strings(declaration.model_dump(mode="json")):
        for pattern in _LITERALS:
            assert not pattern.search(text), (pattern.pattern, text)


@pytest.mark.parametrize(
    ("path", "patterns"),
    [
        pytest.param(DECLARATION_FILE, _LITERALS[:5], id="declaration"),
        # The README's docker command publishes a port pair (`-p 8080:8080`)
        # and binds inside the container (`TRANSPORT_HOST=0.0.0.0`): neither is
        # a URL, so those two patterns are the ones it is not held to.
        pytest.param(README_FILE, _LITERALS[:1] + _LITERALS[3:5], id="readme"),
    ],
)
def test_no_committed_file_holds_a_literal_url_even_in_a_comment(
    path: Path, patterns: tuple[re.Pattern[str], ...]
) -> None:
    """The env var's value is written nowhere: not as an `export` example in a
    comment, not as a link in the README. Names only."""
    for line in path.read_text(encoding="utf-8").splitlines():
        for pattern in patterns:
            assert not pattern.search(line), (path.name, pattern.pattern, line)
    assert URL_ENV in path.read_text(encoding="utf-8")


# --- the probe target, built without connecting --------------------------------


def test_the_target_carries_the_opt_in_and_no_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """build_target reads the name and never connects; the loopback opt-in and
    the absence of a credential both reach the McpTarget."""
    monkeypatch.setenv(URL_ENV, "http://127.0.0.1:9/mcp")
    target = build_target(_declaration(), EXAMPLE_DIR)
    assert target is not None
    assert target.kind == "http"
    assert target.allow_private_hosts is True
    assert target.credential_env is None


def test_an_unset_url_is_reported_not_guessed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(URL_ENV, raising=False)
    assert build_target(_declaration(), EXAMPLE_DIR) is None


# --- what ships ---------------------------------------------------------------------


def test_the_example_is_tracked_by_git() -> None:
    """`.itest/` is gitignored everywhere, so the declaration must be
    force-added. Present on disk but untracked passes locally and fails for
    everyone who clones."""
    if not (REPO_ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    tracked = subprocess.run(
        ["git", "ls-files", "--", str(EXAMPLE_DIR.relative_to(REPO_ROOT))],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert sorted(tracked) == [
        "examples/terraform-mcp-server/.itest/tools/terraform-mcp-server.yaml",
        "examples/terraform-mcp-server/README.md",
    ]


def test_the_readme_pins_the_image_and_names_the_variable() -> None:
    text = README_FILE.read_text(encoding="utf-8")
    assert "hashicorp/terraform-mcp-server:1.3.0" in text
    assert "--toolsets=registry" in text
    assert "registry,terraform" in text
    assert "itest plan" in text
    for command in ("itest sync", "itest verify", "itest report"):
        assert command not in text, command
