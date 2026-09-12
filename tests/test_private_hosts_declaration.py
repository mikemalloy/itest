"""``transport.allow_private_hosts``: the one way a declaration reaches a local
network target.

The private-host guard refuses loopback, link-local, RFC1918 and unique-local
before any connection, and the reference server's HTTP mounts are on loopback —
so the one transport where ``authority.anonymous`` is meaningful could not be
reached from a declaration at all. The opt-in exists for local reference and
test servers; a real deployment never needs it, and a declaration that sets it
must be visible in review and in the plan, never silent.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from test_declarations_plan_sync import EXAMPLE, POLICY, runner

from itest.cli import app
from itest.core.declarations import load_declarations
from itest.core.declarations.schema import Declaration
from itest.core.declarations.tools import build_target
from itest.probes.mcp import McpProbeError, list_tools

REPO_ROOT = Path(__file__).resolve().parents[1]
_SERVER_PATH = REPO_ROOT / "examples" / "reference-mcp" / "server.py"
_spec = importlib.util.spec_from_file_location(
    "reference_mcp_server_private_hosts", _SERVER_PATH
)
assert _spec and _spec.loader
reference_mcp = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = reference_mcp
_spec.loader.exec_module(reference_mcp)

OPEN_URL_ENV = "REFERENCE_MCP_OPEN_URL"


@pytest.fixture(scope="module")
def reference() -> Iterator[Any]:
    with reference_mcp.serve_in_thread(token="private-hosts-token") as running:
        yield running


def _open_declaration(**changes: Any) -> Declaration:
    document: dict[str, Any] = {
        "server": "reference-mcp-open",
        "transport": {"kind": "http", "url_env": OPEN_URL_ENV, **changes},
        "sentinels": {"nonexistent_id": "sentinel-cannot-exist-0000"},
    }
    return Declaration.model_validate(document)


def test_allow_private_hosts_defaults_to_false() -> None:
    """Absence grants nothing: the guard stays on unless the file says so."""
    declaration = _open_declaration()
    assert declaration.transport.allow_private_hosts is False
    assert _open_declaration(allow_private_hosts=True).transport.allow_private_hosts


def test_the_opt_in_is_plumbed_to_the_target(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(OPEN_URL_ENV, "http://127.0.0.1:9/mcp")
    assert build_target(_open_declaration()).allow_private_hosts is False
    assert build_target(_open_declaration(allow_private_hosts=True)).allow_private_hosts


def test_a_loopback_url_is_refused_before_connecting_without_the_opt_in(
    reference: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The open mount answers anyone: a refusal can only be the guard's."""
    monkeypatch.setenv(OPEN_URL_ENV, reference.open_url)
    target = build_target(_open_declaration())
    with pytest.raises(McpProbeError) as excinfo:
        list_tools(target)
    assert "refuses" in str(excinfo.value)
    assert "allow_private_hosts" in str(excinfo.value)


def test_a_loopback_url_is_probed_with_the_opt_in(
    reference: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(OPEN_URL_ENV, reference.open_url)
    target = build_target(_open_declaration(allow_private_hosts=True))
    assert len(list_tools(target)) == 8


def test_the_opt_in_is_only_for_the_host_rule() -> None:
    declaration = _open_declaration(allow_private_hosts=True)
    assert declaration.transport.kind == "http"  # nothing else loosened


# --- the plan says so, per server ------------------------------------------------


def _write(base_dir: Path, name: str, document: dict) -> None:
    path = base_dir / ".itest" / "tools" / f"{name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


@pytest.fixture
def two_servers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reference: Any
) -> Path:
    """The stdio reference declaration beside an open-mount one that opts in."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".itest").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".itest" / "environments.yaml").write_text(POLICY, encoding="utf-8")
    stdio = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    stdio["transport"]["command"] = [sys.executable, str(_SERVER_PATH)]
    _write(tmp_path, "reference-mcp", stdio)
    _write(
        tmp_path,
        "reference-mcp-open",
        {
            "server": "reference-mcp-open",
            "transport": {
                "kind": "http",
                "url_env": OPEN_URL_ENV,
                "allow_private_hosts": True,
            },
            "sentinels": {"nonexistent_id": "sentinel-cannot-exist-0000"},
            "environments": {"active_allowed_in": ["staging"]},
        },
    )
    monkeypatch.setenv(OPEN_URL_ENV, reference.open_url)
    return tmp_path


def test_plan_names_the_server_that_allowed_private_hosts(two_servers: Path) -> None:
    result = runner.invoke(app, ["plan"])
    assert result.exit_code == 0, result.output
    assert "Private hosts allowed by declaration (1):" in result.output
    assert "reference-mcp-open" in result.output
    assert "private hosts allowed by declaration" in result.output
    # The stdio server has no url to opt in with, and is not named.
    block = result.output.split("Private hosts allowed by declaration (1):")[1]
    assert "  ! reference-mcp\n" not in block

    payload = json.loads(runner.invoke(app, ["plan", "--output", "json"]).output)
    assert payload["private_hosts_allowed"] == ["reference-mcp-open"]
    assert payload["unreachable_servers"] == {}
    assert sorted({p["source"] for p in payload["new_points"]}) == [
        "reference-mcp",
        "reference-mcp-open",
    ]


def test_a_plan_with_no_opt_in_says_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Append-only: a project whose declarations keep the guard prints exactly
    the plan it always printed."""
    from test_declarations_plan_sync import _plan, make_workdir

    make_workdir(tmp_path, monkeypatch)
    result = _plan()
    assert result.exit_code == 0, result.output
    assert "Private hosts" not in result.output
    payload = json.loads(_plan("--output", "json").output)
    assert payload["private_hosts_allowed"] == []


# --- the shipped open-mount declaration ------------------------------------------

EXAMPLE_DIR = REPO_ROOT / "examples" / "reference-mcp"


def test_the_example_ships_an_open_mount_declaration_that_opts_in(
    reference: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deliberately defective example: the server's UNGUARDED mount, declared
    with no auth scheme — which is what makes authority.anonymous apply."""
    from itest.core.declarations.tools import build_points
    from itest.core.declarations.traits import load_traits, trait_decisions

    declarations = {d.server: d for d in load_declarations(EXAMPLE_DIR)}
    assert set(declarations) == {"reference-mcp", "reference-mcp-open"}
    open_mount = declarations["reference-mcp-open"]
    assert open_mount.transport.kind == "http"
    assert open_mount.transport.url_env == OPEN_URL_ENV
    assert open_mount.transport.allow_private_hosts is True
    assert open_mount.auth.scheme == "none"
    assert open_mount.auth.credential_env is None
    assert open_mount.sentinels == declarations["reference-mcp"].sentinels
    text = (EXAMPLE_DIR / ".itest" / "tools" / "reference-mcp-open.yaml").read_text(
        encoding="utf-8"
    )
    assert "defect" in text.lower() and "not a template" in text.lower()

    monkeypatch.setenv(OPEN_URL_ENV, reference.open_url)
    table = load_traits()
    tools = list_tools(build_target(open_mount, EXAMPLE_DIR))
    for point in build_points(open_mount, tools, table):
        decision = next(
            d
            for d in trait_decisions(point, table)
            if d.trait.id == "authority.anonymous"
        )
        assert decision.applies is True, point.target


def test_a_stdio_declaration_never_reads_as_a_private_host_opt_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The opt-in loosens the host rule of an http url. A stdio server has no
    url to opt in with, so setting the field there is inert and the plan
    must not name it under "Private hosts allowed by declaration"."""
    from test_declarations_plan_sync import _plan, make_workdir

    workdir = make_workdir(tmp_path, monkeypatch)
    path = workdir / ".itest" / "tools" / "reference-mcp.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["transport"]["allow_private_hosts"] = True
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    result = _plan()
    assert result.exit_code == 0, result.output
    assert "Private hosts" not in result.output
    payload = json.loads(_plan("--output", "json").output)
    assert payload["private_hosts_allowed"] == []
