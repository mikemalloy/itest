"""The check library (``itest/checks``): the contract, the registry, and the
engine checks A1, B1 (agreement), D1, D2 and D3.

Everything runs against ``examples/reference-mcp/`` — over stdio, and on a
loopback port for the guarded and open mounts — plus a tiny variant stdio server
for the two B1 branches the reference server deliberately does not have (an
annotation/name conflict and a tool nothing can classify). Transport failures
are produced by mocks or by a command that exits at once. Nothing here reaches
the network.

The project a check runs in is a temporary directory holding a copy of the
reference declaration and a manifest built from the live listing, exactly the
two files ``itest verify`` would find at a project root.
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import itest.checks as checks
from itest.checks import (
    CheckResult,
    authority,
    blast_radius,
    change,
    clear_cache,
    run_engine_check,
    run_generated_check,
)
from itest.core.declarations.loader import _load_one
from itest.core.declarations.tools import build_points, tool_point_id
from itest.core.declarations.traits import load_traits
from itest.core.manifest import Manifest, save_manifest
from itest.probes import mcp as mcp_probe
from itest.probes.mcp import CallResult, McpTarget

REPO_ROOT = Path(__file__).resolve().parents[1]
_SERVER_PATH = REPO_ROOT / "examples" / "reference-mcp" / "server.py"
_DECLARATION = (
    REPO_ROOT / "examples" / "reference-mcp" / ".itest" / "tools" / "reference-mcp.yaml"
)
_spec = importlib.util.spec_from_file_location("reference_mcp_server", _SERVER_PATH)
assert _spec and _spec.loader
reference_mcp = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = reference_mcp
_spec.loader.exec_module(reference_mcp)

SERVER = "reference-mcp"
SENTINEL = "sentinel-cannot-exist-0000"
TOKEN = "zzq-checks-credential-91c2d4-do-not-log"
ENV = "REFERENCE_MCP_TOKEN"

ENGINE_TRAITS = ("A1", "B1", "D1", "D2", "D3")


# --- fixtures -----------------------------------------------------------------


@pytest.fixture(scope="module")
def reference() -> Iterator[Any]:
    with reference_mcp.serve_in_thread(token=TOKEN) as running:
        yield running


@pytest.fixture(autouse=True)
def _fresh_run(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Every test is its own verify run: an empty listing cache, and a shell that
    holds the credential unless a test says otherwise."""
    monkeypatch.setenv(ENV, TOKEN)
    clear_cache()
    yield
    clear_cache()


def stdio_target(**kwargs: Any) -> McpTarget:
    return McpTarget(
        kind="stdio",
        command=[sys.executable, str(_SERVER_PATH)],
        credential_env=ENV,
        **kwargs,
    )


def http_target(url: str) -> McpTarget:
    return McpTarget(kind="http", url=url, credential_env=ENV, allow_private_hosts=True)


@pytest.fixture(scope="module")
def manifest_points() -> list[Any]:
    """The reference server's eight mcp_tool points, built the way plan builds
    them: the declaration cross-checked against the live stdio listing."""
    declaration = _load_one(_DECLARATION)
    tools = mcp_probe.list_tools(stdio_target())
    return build_points(declaration, tools, load_traits())


def write_project(
    root: Path, points: list[Any], *, declaration_text: str | None = None
) -> Path:
    tools_dir = root / ".itest" / "tools"
    tools_dir.mkdir(parents=True, exist_ok=True)
    text = declaration_text or _DECLARATION.read_text(encoding="utf-8")
    (tools_dir / f"{SERVER}.yaml").write_text(text, encoding="utf-8")
    manifest = Manifest(generated_at=points[0].first_seen, points=list(points))
    save_manifest(manifest, root / ".itest" / "manifest.yaml")
    return root


@pytest.fixture
def project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, manifest_points: list[Any]
) -> Path:
    root = write_project(tmp_path, manifest_points)
    monkeypatch.chdir(root)
    return root


def point(manifest_points: list[Any], name: str, **attributes: Any) -> dict:
    """The manifest point for ``name`` as the plain dict 31B passes."""
    (found,) = [p for p in manifest_points if p.target == name]
    merged = {**found.attributes, **attributes}
    return {
        "id": found.id,
        "type": found.type,
        "server": found.source,
        "target": found.target,
        "attributes": merged,
    }


def strings_in(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, inner in value.items():
            yield str(key)
            yield from strings_in(inner)
    elif isinstance(value, list | tuple):
        for inner in value:
            yield from strings_in(inner)


# --- the contract -------------------------------------------------------------


def test_check_result_is_the_contract() -> None:
    result = CheckResult(status="pass", detail="ok")
    assert result.evidence is None
    with pytest.raises(AttributeError):
        result.status = "fail"  # type: ignore[misc]  # frozen


def test_the_public_signatures_are_exactly_the_contract() -> None:
    engine = inspect.signature(run_engine_check)
    assert list(engine.parameters) == ["trait_id", "point", "target", "authenticated"]
    assert engine.parameters["authenticated"].kind is inspect.Parameter.KEYWORD_ONLY
    generated = inspect.signature(run_generated_check)
    assert list(generated.parameters) == ["trait_id", "point", "target", "fixtures"]
    assert generated.parameters["fixtures"].kind is inspect.Parameter.KEYWORD_ONLY


def test_the_registry_holds_exactly_the_engine_checks() -> None:
    assert set(checks.ENGINE_CHECKS) == set(ENGINE_TRAITS)
    assert checks.ENGINE_CHECKS["A1"].__name__ == "check_a1"
    assert checks.ENGINE_CHECKS["B1"].__name__ == "check_b1"
    assert checks.ENGINE_CHECKS["D3"].__name__ == "check_d3"
    assert dict(checks.GENERATED_CHECKS) == {}


def test_an_unknown_trait_is_not_verifiable_and_never_raises(
    project: Path, manifest_points: list[Any]
) -> None:
    result = run_engine_check(
        "Q7", point(manifest_points, "get_guide"), stdio_target(), authenticated=True
    )
    assert result == CheckResult(
        status="not_verifiable", detail="no engine check for Q7"
    )


def test_a_generated_trait_has_no_check_yet(
    project: Path, manifest_points: list[Any]
) -> None:
    result = run_generated_check(
        "A2", point(manifest_points, "fetch_record"), stdio_target(), fixtures={}
    )
    assert result.status == "not_verifiable"
    assert result.detail == "no generated check for A2 yet"


@pytest.mark.parametrize("trait", ENGINE_TRAITS)
def test_every_check_has_an_explainable_docstring(trait: str) -> None:
    doc = inspect.getdoc(checks.ENGINE_CHECKS[trait]) or ""
    assert "pass" in doc and "not_verifiable" in doc, trait
    assert "ASI0" in doc, f"{trait} names no OWASP Agentic row"
    assert "Semgrep" in doc, f"{trait} names no Semgrep cheatsheet row"


# --- A1: refuses anonymous ----------------------------------------------------


def test_a1_sentinel_arguments_follow_the_rules() -> None:
    schema = {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "count": {"type": "integer"},
            "ratio": {"type": "number"},
            "confirm": {"type": "boolean"},
            "note": {"type": "string"},
        },
        "required": ["id", "count", "ratio", "confirm"],
    }
    assert authority.sentinel_arguments(schema, SENTINEL) == {
        "id": SENTINEL,
        "count": 0,
        "ratio": 0,
        "confirm": False,
    }


def test_a1_a_required_parameter_with_no_sentinel_form_is_refused() -> None:
    schema = {
        "properties": {"filter": {"type": "object"}},
        "required": ["filter"],
    }
    with pytest.raises(authority.NoSentinel) as excinfo:
        authority.sentinel_arguments(schema, SENTINEL)
    assert "filter" in str(excinfo.value)


FRONT_DOOR = "server refuses anonymous sessions; per-tool call not attempted"
DEFERRED = (
    "anonymous session admitted; this tool mutates, so its own guard can only be "
    "proven by an active-tier call on a non-production environment"
)
READ_TOOLS = {"get_guide", "search_records", "fetch_record", "lookalike_read", "enrich"}
MUTATING_TOOLS = {"create_record", "update_record", "delete_record"}


def test_a1_the_guarded_mount_passes_every_tool_at_the_front_door(
    project: Path,
    manifest_points: list[Any],
    reference: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The anonymous session is refused, so no tool is called — read or not."""
    calls: list[Any] = []
    monkeypatch.setattr(authority, "call_tool", lambda *a, **k: calls.append(a))
    for p in manifest_points:
        result = run_engine_check(
            "A1",
            point(manifest_points, p.target),
            http_target(reference.guarded_url),
            authenticated=False,
        )
        assert result.status == "pass", (p.target, result.detail)
        assert result.detail == FRONT_DOOR
        assert result.evidence["anonymous_listing"] == "refused"
        assert "401" in result.evidence["anonymous_listing_detail"]
        assert result.evidence["called"] is False
    assert calls == []


def test_a1_a_read_tool_answered_on_the_open_mount_fails(
    project: Path, manifest_points: list[Any], reference: Any
) -> None:
    result = run_engine_check(
        "A1",
        point(manifest_points, "get_guide"),
        http_target(reference.open_url),
        authenticated=False,
    )
    assert result.status == "fail", result.detail
    assert result.detail.startswith("anonymous call succeeded on a read tool")
    assert result.evidence["anonymous_listing"] == "admitted"
    assert result.evidence["anonymous_tool_count"] == 8
    assert result.evidence["called"] is True
    assert result.evidence["arguments"] == {}


def test_a1_fetch_record_on_the_open_mount_fails(
    project: Path, manifest_points: list[Any], reference: Any
) -> None:
    """fetch_record answers the sentinel with a 404-shaped tool error. The
    anonymous call reached the read tool and its code ran: that is the
    demonstrated admission, and the detail says it was a tool error."""
    result = run_engine_check(
        "A1",
        point(manifest_points, "fetch_record"),
        http_target(reference.open_url),
        authenticated=False,
    )
    assert result.status == "fail", result.detail
    assert "404" in result.detail
    assert result.evidence["arguments"] == {"id": SENTINEL}
    assert result.evidence["call_status"] == "error"


def test_a1_delete_record_on_the_open_mount_is_deferred_to_the_active_tier(
    project: Path, manifest_points: list[Any], reference: Any
) -> None:
    before = {key: dict(value) for key, value in reference.records.items()}
    result = run_engine_check(
        "A1",
        point(manifest_points, "delete_record"),
        http_target(reference.open_url),
        authenticated=False,
    )
    assert result.status == "not_verifiable", result.detail
    assert result.detail == DEFERRED
    assert result.evidence["anonymous_listing"] == "admitted"
    assert result.evidence["called"] is False
    assert reference.records == before  # nothing was called, so nothing moved


def test_a1_every_mutating_tool_on_an_open_server_is_deferred(
    project: Path, manifest_points: list[Any], reference: Any
) -> None:
    for name in sorted(MUTATING_TOOLS):
        result = run_engine_check(
            "A1",
            point(manifest_points, name),
            http_target(reference.open_url),
            authenticated=False,
        )
        assert (result.status, result.detail) == ("not_verifiable", DEFERRED), name


def test_a1_a_read_call_refused_inside_an_admitted_session_passes(
    project: Path, manifest_points: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A server can admit anonymous sessions and still refuse the call."""

    def refused(*args: Any, **kwargs: Any) -> CallResult:
        return CallResult(
            ok=False,
            status="refused",
            detail="calling 'get_guide': the server refused with HTTP 403",
        )

    monkeypatch.setattr(authority, "call_tool", refused)
    result = run_engine_check(
        "A1", point(manifest_points, "get_guide"), stdio_target(), authenticated=True
    )
    assert result.status == "pass", result.detail
    assert result.detail.startswith("unauthenticated tools/call refused: ")
    assert result.evidence["anonymous_listing"] == "admitted"


def test_a1_over_stdio_without_the_credential(
    project: Path, manifest_points: list[Any]
) -> None:
    """stdio has no transport guard: the reference server admits a subprocess
    launched without its credential. A read answered is a fail; a destructive
    tool is deferred to the active tier and is not called."""
    target = stdio_target()
    read = run_engine_check(
        "A1", point(manifest_points, "search_records"), target, authenticated=True
    )
    assert read.status == "fail"
    destructive = run_engine_check(
        "A1", point(manifest_points, "delete_record"), target, authenticated=True
    )
    assert (destructive.status, destructive.detail) == ("not_verifiable", DEFERRED)
    assert destructive.evidence["called"] is False


def test_a1_never_produces_critical(
    project: Path, manifest_points: list[Any], reference: Any
) -> None:
    """critical means a demonstrated admission on a mutating tool, which only an
    active-tier call can show. The readonly A1 never says it."""
    for target in (
        stdio_target(),
        http_target(reference.guarded_url),
        http_target(reference.open_url),
    ):
        for p in manifest_points:
            result = run_engine_check(
                "A1", point(manifest_points, p.target), target, authenticated=False
            )
            assert result.status != "critical", (p.target, result.detail)
    assert "CRITICAL" not in Path(authority.__file__).read_text(encoding="utf-8")


def test_a1_a_transport_error_on_the_call_is_not_verifiable(
    project: Path, manifest_points: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def timed_out(*args: Any, **kwargs: Any) -> CallResult:
        return CallResult(
            ok=False, status="error", detail="calling 'get_guide' timed out after 1s"
        )

    monkeypatch.setattr(authority, "call_tool", timed_out)
    result = run_engine_check(
        "A1", point(manifest_points, "get_guide"), stdio_target(), authenticated=True
    )
    assert result.status == "not_verifiable"
    assert "timed out" in result.detail


def test_a1_an_unreachable_server_is_not_verifiable(
    project: Path, manifest_points: list[Any]
) -> None:
    dead = McpTarget(kind="stdio", command=[sys.executable, "-c", "pass"])
    for name in ("get_guide", "delete_record"):
        result = run_engine_check(
            "A1", point(manifest_points, name), dead, authenticated=False
        )
        assert result.status == "not_verifiable"
        assert result.evidence["anonymous_listing"] == "error"
        assert result.evidence["called"] is False


def test_a1_without_a_declaration_there_is_no_sentinel(
    tmp_path: Path, manifest_points: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = run_engine_check(
        "A1", point(manifest_points, "fetch_record"), stdio_target(), authenticated=True
    )
    assert result.status == "not_verifiable"
    assert "sentinels.nonexistent_id" in result.detail


def test_a1_an_unknown_class_is_never_called(
    project: Path, manifest_points: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Any] = []
    monkeypatch.setattr(authority, "call_tool", lambda *a, **k: calls.append(a))
    result = run_engine_check(
        "A1",
        point(manifest_points, "get_guide", mutation="unknown"),
        stdio_target(),
        authenticated=True,
    )
    assert calls == []
    assert result.status == "not_verifiable"


def test_a1_a_tool_hidden_from_the_anonymous_listing_is_not_called(
    project: Path, manifest_points: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Any] = []
    monkeypatch.setattr(authority, "call_tool", lambda *a, **k: calls.append(a))
    hidden = point(manifest_points, "get_guide")
    hidden["target"] = "hidden_reader"
    result = run_engine_check("A1", hidden, stdio_target(), authenticated=True)
    assert calls == []
    assert result.status == "not_verifiable"
    assert "not in the anonymous listing" in result.detail


def test_a1_the_stricter_of_recorded_and_live_class_decides(
    project: Path, manifest_points: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manifest that says `read` for delete_record (stale, or edited) must not
    talk A1 into calling it: the live listing says destructive."""
    calls: list[Any] = []
    monkeypatch.setattr(authority, "call_tool", lambda *a, **k: calls.append(a))
    result = run_engine_check(
        "A1",
        point(manifest_points, "delete_record", mutation="read"),
        stdio_target(),
        authenticated=True,
    )
    assert calls == []
    assert (result.status, result.detail) == ("not_verifiable", DEFERRED)


# --- B1: mutation class (agreement) -------------------------------------------


def test_b1_agreement_passes_and_names_its_sources(
    project: Path, manifest_points: list[Any]
) -> None:
    result = run_engine_check(
        "B1",
        point(manifest_points, "delete_record"),
        stdio_target(),
        authenticated=True,
    )
    assert result.status == "pass", result.detail
    assert "annotation destructiveHint" in result.detail
    assert "name delete_*" in result.detail
    assert result.evidence["live"] == {"class": "destructive", "source": "annotation"}


def test_b1_a_confirming_declaration_is_named(
    project: Path, manifest_points: list[Any]
) -> None:
    result = run_engine_check(
        "B1",
        point(manifest_points, "delete_record", mutation_source="confirmed"),
        stdio_target(),
        authenticated=True,
    )
    assert result.status == "pass"
    assert "declared destructive" in result.detail


def test_b1_passes_lookalike_read_by_design(
    project: Path, manifest_points: list[Any]
) -> None:
    """THE PINNED LIMIT. lookalike_read is annotated read-only and mutates. Its
    annotation and its name agree, so the agreement check passes it — correctly,
    because an annotation lie is only visible by observing behaviour, which is
    the active-tier "B1 observed" recipe. When that ships, this test stays: the
    agreement check's answer does not change, a second check's does."""
    result = run_engine_check(
        "B1",
        point(manifest_points, "lookalike_read"),
        stdio_target(),
        authenticated=True,
    )
    assert result.status == "pass"
    doc = inspect.getdoc(blast_radius.check_b1) or ""
    assert "lookalike_read" in doc
    assert "observ" in doc


def test_b1_a_class_that_moved_is_changed(
    project: Path, manifest_points: list[Any]
) -> None:
    result = run_engine_check(
        "B1",
        point(manifest_points, "create_record", mutation="read"),
        stdio_target(),
        authenticated=True,
    )
    assert result.status == "changed"
    assert "read" in result.detail and "write" in result.detail
    assert result.evidence["manifest"]["mutation"] == "read"
    assert result.evidence["live"]["class"] == "write"


_VARIANT_SERVER = textwrap.dedent(
    """
    from mcp.server.mcpserver import MCPServer
    from mcp_types import ToolAnnotations

    server = MCPServer(name="variant", version="1.0.0")

    @server.tool(
        description="Says it only reads.",
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    def create_widget(name: str) -> str:
        return name

    @server.tool(description="Nobody can tell what this does.")
    def frobnicate(thing: str) -> str:
        return thing

    server.run("stdio")
    """
)


@pytest.fixture
def variant(tmp_path: Path) -> McpTarget:
    script = tmp_path / "variant.py"
    script.write_text(_VARIANT_SERVER, encoding="utf-8")
    return McpTarget(kind="stdio", command=[sys.executable, str(script)])


def variant_point(name: str, mutation: str, source: str = "detected") -> dict:
    return {
        "id": tool_point_id("variant", name),
        "type": "mcp_tool",
        "server": "variant",
        "target": name,
        "attributes": {"mutation": mutation, "mutation_source": source},
    }


def test_b1_annotation_and_name_disagreeing_unsettled_fails(
    variant: McpTarget,
) -> None:
    result = run_engine_check(
        "B1", variant_point("create_widget", "read"), variant, authenticated=True
    )
    assert result.status == "fail", result.detail
    assert "read-only" in result.detail
    assert "create_*" in result.detail
    assert "declare it" in result.detail


def test_b1_a_declaration_settles_the_disagreement(variant: McpTarget) -> None:
    result = run_engine_check(
        "B1",
        variant_point("create_widget", "read", source="confirmed"),
        variant,
        authenticated=True,
    )
    assert result.status == "pass", result.detail
    assert "declared read" in result.detail


def test_b1_unknown_is_not_verifiable(variant: McpTarget) -> None:
    result = run_engine_check(
        "B1", variant_point("frobnicate", "unknown"), variant, authenticated=True
    )
    assert result.status == "not_verifiable"
    assert "unknown" in result.detail


def test_b1_a_declaration_alone_is_not_verifiable(variant: McpTarget) -> None:
    """The server says nothing and the declaration filled the gap: one statement
    with nothing to agree with. Only observing the tool could check it."""
    result = run_engine_check(
        "B1",
        variant_point("frobnicate", "write", source="declared"),
        variant,
        authenticated=True,
    )
    assert result.status == "not_verifiable"
    assert "declared write" in result.detail


def test_b1_a_tool_the_server_no_longer_lists_is_not_verifiable(
    project: Path, manifest_points: list[Any]
) -> None:
    missing = point(manifest_points, "get_guide")
    missing["target"] = "withdrawn_tool"
    result = run_engine_check("B1", missing, stdio_target(), authenticated=True)
    assert result.status == "not_verifiable"
    assert "D1" in result.detail


# --- D1: inventory ------------------------------------------------------------


def test_d1_a_listed_tool_passes(project: Path, manifest_points: list[Any]) -> None:
    result = run_engine_check(
        "D1", point(manifest_points, "fetch_record"), stdio_target(), authenticated=True
    )
    assert result.status == "pass"
    assert result.evidence["undeclared"] == []


def test_d1_an_absent_tool_is_an_orphan(
    project: Path, manifest_points: list[Any]
) -> None:
    missing = point(manifest_points, "get_guide")
    missing["target"] = "withdrawn_tool"
    result = run_engine_check("D1", missing, stdio_target(), authenticated=True)
    assert result.status == "fail"
    assert "not in tools/list; orphan" in result.detail


def test_d1_reports_an_undeclared_tool_on_every_result(
    tmp_path: Path, manifest_points: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    without_enrich = [p for p in manifest_points if p.target != "enrich"]
    monkeypatch.chdir(write_project(tmp_path, without_enrich))
    for name in ("fetch_record", "get_guide"):
        result = run_engine_check(
            "D1", point(manifest_points, name), stdio_target(), authenticated=True
        )
        assert result.status == "pass"
        assert result.evidence["undeclared"] == ["enrich"]


def test_d1_without_a_manifest_undeclared_is_unknown_not_empty(
    tmp_path: Path, manifest_points: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = run_engine_check(
        "D1", point(manifest_points, "fetch_record"), stdio_target(), authenticated=True
    )
    assert result.status == "pass"
    assert result.evidence["undeclared"] is None


# --- D2 / D3: schema and description drift ------------------------------------


@pytest.mark.parametrize(
    ("trait", "field"), [("D2", "schema_hash"), ("D3", "description_hash")]
)
def test_d2_d3_a_matching_hash_passes(
    trait: str, field: str, project: Path, manifest_points: list[Any]
) -> None:
    result = run_engine_check(
        trait, point(manifest_points, "enrich"), stdio_target(), authenticated=True
    )
    assert result.status == "pass", result.detail


@pytest.mark.parametrize(
    ("trait", "field"), [("D2", "schema_hash"), ("D3", "description_hash")]
)
def test_d2_d3_a_stale_hash_is_changed(
    trait: str, field: str, project: Path, manifest_points: list[Any]
) -> None:
    stale = point(manifest_points, "enrich", **{field: "000000000000"})
    live = point(manifest_points, "enrich")["attributes"][field]
    result = run_engine_check(trait, stale, stdio_target(), authenticated=True)
    assert result.status == "changed"
    assert "000000000000" in result.detail and live in result.detail
    assert result.evidence == {
        "server": SERVER,
        "tool": "enrich",
        "recorded": "000000000000",
        "live": live,
    }


@pytest.mark.parametrize("trait", ["D2", "D3"])
def test_d2_d3_an_absent_tool_is_not_verifiable(
    trait: str, project: Path, manifest_points: list[Any]
) -> None:
    missing = point(manifest_points, "enrich")
    missing["target"] = "withdrawn_tool"
    result = run_engine_check(trait, missing, stdio_target(), authenticated=True)
    assert result.status == "not_verifiable"


@pytest.mark.parametrize("trait", ["B1", "D1", "D2", "D3"])
def test_listing_checks_are_not_verifiable_when_the_server_is_not_there(
    trait: str, project: Path, manifest_points: list[Any]
) -> None:
    dead = McpTarget(kind="stdio", command=[sys.executable, "-c", "pass"])
    result = run_engine_check(
        trait, point(manifest_points, "enrich"), dead, authenticated=False
    )
    assert result.status == "not_verifiable"
    assert result.detail.startswith("could not list tools")


@pytest.mark.parametrize("trait", ENGINE_TRAITS)
def test_an_unusable_target_is_not_verifiable_not_an_exception(
    trait: str, project: Path, manifest_points: list[Any]
) -> None:
    for target in (
        McpTarget(kind="http"),  # no url
        McpTarget(kind="http", url="http://10.0.0.5/mcp"),  # private, no opt-in
    ):
        result = run_engine_check(
            trait, point(manifest_points, "get_guide"), target, authenticated=False
        )
        assert result.status == "not_verifiable", (trait, result)


# --- authenticated listings ---------------------------------------------------


def test_an_authenticated_listing_uses_the_credential(
    project: Path, manifest_points: list[Any], reference: Any
) -> None:
    result = run_engine_check(
        "D1",
        point(manifest_points, "fetch_record"),
        http_target(reference.guarded_url),
        authenticated=True,
    )
    assert result.status == "pass", result.detail


def test_an_authenticated_listing_without_the_credential_is_not_verifiable(
    project: Path,
    manifest_points: list[Any],
    reference: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(ENV)
    result = run_engine_check(
        "D2",
        point(manifest_points, "fetch_record"),
        http_target(reference.guarded_url),
        authenticated=True,
    )
    assert result.status == "not_verifiable"
    assert ENV in result.detail


def test_an_anonymous_listing_on_the_guarded_mount_is_not_verifiable(
    project: Path, manifest_points: list[Any], reference: Any
) -> None:
    result = run_engine_check(
        "D3",
        point(manifest_points, "fetch_record"),
        http_target(reference.guarded_url),
        authenticated=False,
    )
    assert result.status == "not_verifiable"
    assert "refused" in result.detail


# --- the per-run listing cache ------------------------------------------------


def test_one_listing_per_server_per_run(
    project: Path, manifest_points: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[bool] = []
    real = checks._base.list_tools

    def counting(target: McpTarget, **kwargs: Any):
        calls.append(kwargs.get("anonymous", False))
        return real(target, **kwargs)

    monkeypatch.setattr(checks._base, "list_tools", counting)
    target = stdio_target()
    for name in ("enrich", "fetch_record", "get_guide"):
        for trait in ("B1", "D1", "D2", "D3"):
            run_engine_check(
                trait, point(manifest_points, name), target, authenticated=True
            )
    assert calls == [False]

    clear_cache()
    run_engine_check("D1", point(manifest_points, "enrich"), target, authenticated=True)
    assert calls == [False, False]


# --- scrubbing ----------------------------------------------------------------


def test_a_credential_echoed_by_the_server_is_scrubbed(
    tmp_path: Path, manifest_points: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real path: make the sentinel the token itself, so fetch_record's 404
    error echoes the credential back into A1's detail and evidence."""
    text = _DECLARATION.read_text(encoding="utf-8").replace(SENTINEL, TOKEN)
    monkeypatch.chdir(write_project(tmp_path, manifest_points, declaration_text=text))
    result = run_engine_check(
        "A1", point(manifest_points, "fetch_record"), stdio_target(), authenticated=True
    )
    assert result.status == "fail"
    assert "***" in result.detail
    for text in [result.detail, *strings_in(result.evidence)]:
        assert TOKEN not in text


def test_every_string_in_a_result_is_scrubbed(
    project: Path, manifest_points: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def leaky(point: dict, target: McpTarget, *, authenticated: bool) -> CheckResult:
        return CheckResult(
            status="fail",
            detail=f"server said {TOKEN}",
            evidence={
                "nested": [{"echo": f"Bearer {TOKEN}"}],
                TOKEN: "as a key",
                "schema_hash": "123456789012",
            },
        )

    monkeypatch.setitem(checks.ENGINE_CHECKS, "Z9", leaky)
    result = run_engine_check(
        "Z9", point(manifest_points, "get_guide"), stdio_target(), authenticated=True
    )
    assert TOKEN not in result.detail
    for text in strings_in(result.evidence):
        assert TOKEN not in text
    # A hash is data, not an account id: the scrubber must not pseudonymize it.
    assert result.evidence["schema_hash"] == "123456789012"


@pytest.mark.parametrize("trait", ENGINE_TRAITS)
def test_no_result_on_a_real_run_carries_the_token(
    trait: str, project: Path, manifest_points: list[Any], reference: Any
) -> None:
    for target in (
        stdio_target(),
        http_target(reference.guarded_url),
        http_target(reference.open_url),
    ):
        for p in manifest_points:
            result = run_engine_check(
                trait, point(manifest_points, p.target), target, authenticated=True
            )
            for text in [result.detail, *strings_in(result.evidence)]:
                assert TOKEN not in text, (trait, p.target)


# --- refuse-by-default, structurally ------------------------------------------


def test_no_check_calls_a_write_or_destructive_tool(
    project: Path,
    manifest_points: list[Any],
    reference: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spy on every tools/call the library makes, across every engine check,
    every reference tool, and all three mounts. None may target a write,
    destructive or unknown tool, and none may pass allow_mutating."""
    seen: list[dict[str, Any]] = []
    real = authority.call_tool

    def spy(target: McpTarget, name: str, arguments: dict, **kwargs: Any):
        seen.append({"name": name, **kwargs})
        return real(target, name, arguments, **kwargs)

    monkeypatch.setattr(authority, "call_tool", spy)
    classes = {p.target: p.attributes["mutation"] for p in manifest_points}
    for target in (
        stdio_target(),
        http_target(reference.guarded_url),
        http_target(reference.open_url),
    ):
        for p in manifest_points:
            for trait in ENGINE_TRAITS:
                run_engine_check(
                    trait, point(manifest_points, p.target), target, authenticated=False
                )

    assert seen, "A1 should have called the read tools"
    for call in seen:
        assert classes[call["name"]] in ("read", "informational"), call
        assert call.get("allow_mutating", False) is False, call
        assert call["authenticated"] is False, call


def test_only_authority_imports_call_tool_and_nothing_opts_into_mutation() -> None:
    package = Path(checks.__file__).parent
    for source in package.glob("*.py"):
        text = source.read_text(encoding="utf-8")
        assert "allow_mutating=True" not in text, source.name
        if source.name != "authority.py":
            assert "call_tool" not in text, source.name
    assert not hasattr(change, "call_tool")
    assert not hasattr(blast_radius, "call_tool")
