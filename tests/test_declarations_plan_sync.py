"""Declaration-driven plan and sync, end to end against the reference MCP server.

This is where the declaration becomes points and the points become a suite. Every
claim the design makes about that path is pinned here, against the real server
over stdio — nothing is mocked, because the thing being tested is what a live
``tools/list`` does to a manifest.

What is proven, in order:

- **eight live tools become eight points**, and their ids are a function of
  ``(server, tool name)`` alone — identical across runs;
- **a reworded description is drift, not a new point**: the id holds, the entry is
  reported as ``changed``, and the tests covering it are NOT orphaned;
- **an override for a tool the server does not list is orphaned**, flagged and
  never deleted;
- **a declared mutation class the server contradicts refuses the whole plan**:
  exit 2, the tool named, and no manifest written;
- **the stub set comes from the table, not from code**: editing one
  ``applies_when`` line changes what sync generates;
- **an unreachable server is a line, not a crash**;
- **nothing about a declaration-free project moves** (the alex fixtures).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from typer.testing import CliRunner

from itest.cli import app
from itest.core.declarations import load_declarations
from itest.core.declarations import traits as traits_module
from itest.core.declarations.tools import tool_point_id
from itest.core.manifest import load_manifest

runner = CliRunner()

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER_PATH = REPO_ROOT / "examples" / "reference-mcp" / "server.py"
EXAMPLE = (
    REPO_ROOT / "examples" / "reference-mcp" / ".itest" / "tools" / "reference-mcp.yaml"
)
ALEX = REPO_ROOT / "tests" / "fixtures" / "alex" / "alex-s6.json"

#: Everything sync writes for one declared server lives in its own directory,
#: because the human-owned conftest.py beside the bindings is per server.
SERVER_DIR = "itest_tests/tools_reference_mcp"
READONLY_FILE = f"{SERVER_DIR}/test_reference_mcp__generated.py"
ACTIVE_FILE = f"{SERVER_DIR}/test_reference_mcp__generated_active.py"
ENGINE_FILE = f"{SERVER_DIR}/test_reference_mcp__engine.py"
ENGINE_ACTIVE_FILE = f"{SERVER_DIR}/test_reference_mcp__engine_active.py"
CONFTEST_FILE = f"{SERVER_DIR}/conftest.py"

#: The reference server's tools. Pinned in tests/test_reference_mcp.py.
EXPECTED_TOOLS = {
    "get_guide",
    "search_records",
    "fetch_record",
    "create_record",
    "update_record",
    "delete_record",
    "lookalike_read",
    "enrich",
}

#: A state document with nothing in it: these tests are about the declared side,
#: and plan still reads Terraform for the detected side.
EMPTY_STATE = {
    "format_version": "1.0",
    "terraform_version": "1.9.0",
    "values": {"root_module": {"resources": []}},
}

POLICY = """\
version: 1
environments:
  staging:
    tiers: [static, readonly, active]
"""


def _declare(base_dir: Path, *, name: str = "reference-mcp", **changes: object) -> Path:
    """Write the example declaration into ``base_dir``, with the command rewired.

    The committed example launches `python server.py` in its own directory; a
    test checkout holds no server.py, so the argv becomes this interpreter and
    the absolute path. Everything else is the real file.
    """
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["transport"]["command"] = [sys.executable, str(SERVER_PATH)]
    for key, value in changes.items():
        document[key] = value
    path = base_dir / ".itest" / "tools" / f"{name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return make_workdir(tmp_path, monkeypatch)


def make_workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A checkout with the declaration, a policy that permits active, and state."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".itest").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".itest" / "environments.yaml").write_text(POLICY, encoding="utf-8")
    (tmp_path / "state.json").write_text(json.dumps(EMPTY_STATE), encoding="utf-8")
    _declare(tmp_path)
    return tmp_path


def _plan(*extra: str):
    return runner.invoke(app, ["plan", "--tf-json", "state.json", *extra])


def _sync(*extra: str):
    return runner.invoke(
        app, ["sync", "--auto-approve", "--tf-json", "state.json", *extra]
    )


def _functions(path: Path) -> set[str]:
    """Every ``def test_...`` in a generated file, by name. Empty if absent."""
    if not path.exists():
        return set()
    return {
        line.split("(")[0].removeprefix("def ").strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("def test_")
    }


# --- points ------------------------------------------------------------------


def test_plan_turns_the_eight_live_tools_into_eight_points(workdir: Path) -> None:
    result = _plan("--output", "json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    tools = [p for p in payload["new_points"] if p["type"] == "mcp_tool"]
    assert len(tools) == 8
    assert {p["target"] for p in tools} == EXPECTED_TOOLS
    assert {p["source"] for p in tools} == {"reference-mcp"}
    assert {p["origin"] for p in tools} == {"declared"}
    assert {p["hcl_address"] for p in tools} == {".itest/tools/reference-mcp.yaml"}


def test_the_human_plan_lists_the_tools(workdir: Path) -> None:
    result = _plan()
    assert result.exit_code == 0, result.output
    assert "8 new, 0 unchanged, 0 orphaned test(s)" in result.output
    assert "reference-mcp -> delete_record" in result.output
    assert "destructive (detected)" in result.output
    assert "[approval confirm_param]" in result.output
    assert "[egress enrichment-provider.example]" in result.output


def test_point_ids_are_a_function_of_server_and_tool_name_only(workdir: Path) -> None:
    first = json.loads(_plan("--output", "json").output)["new_points"]
    second = json.loads(_plan("--output", "json").output)["new_points"]
    by_name = {p["target"]: p["id"] for p in first}
    assert by_name == {p["target"]: p["id"] for p in second}
    for name, point_id in by_name.items():
        assert point_id == tool_point_id("reference-mcp", name)


def test_the_mutation_class_is_detected_and_its_provenance_recorded(
    workdir: Path,
) -> None:
    """Facts in, traits out: the example declares no class for any tool, so every
    point says `detected` and names what the server's own listing implied."""
    points = json.loads(_plan("--output", "json").output)["new_points"]
    attributes = {p["target"]: p["attributes"] for p in points}

    assert attributes["delete_record"]["mutation"] == "destructive"
    assert attributes["create_record"]["mutation"] == "write"
    assert attributes["fetch_record"]["mutation"] == "read"
    assert attributes["get_guide"]["mutation"] == "informational"
    # The behavioural liar reads as a read, and correctly so: nothing in the
    # listing disagrees with anything else.
    assert attributes["lookalike_read"]["mutation"] == "read"
    assert {a["mutation_source"] for a in attributes.values()} == {"detected"}


def test_a_declared_class_that_agrees_is_recorded_as_confirmed(workdir: Path) -> None:
    _declare(
        workdir,
        tools={
            "delete_record": {"approval": "confirm_param", "mutation": "destructive"}
        },
    )
    points = json.loads(_plan("--output", "json").output)["new_points"]
    delete = next(p for p in points if p["target"] == "delete_record")
    assert delete["attributes"]["mutation"] == "destructive"
    assert delete["attributes"]["mutation_source"] == "confirmed"


def test_the_attributes_carry_what_the_traits_table_asks_about(workdir: Path) -> None:
    points = json.loads(_plan("--output", "json").output)["new_points"]
    attributes = {p["target"]: p["attributes"] for p in points}

    assert attributes["enrich"]["egress"] == {
        "to": "enrichment-provider.example",
        "data": ["email"],
    }
    assert attributes["fetch_record"]["egress"] is None
    assert attributes["delete_record"]["approval"] == "confirm_param"
    assert attributes["fetch_record"]["approval"] == "none"
    # A parameterless tool has nothing free-form to pass through.
    assert attributes["get_guide"]["has_free_form_input"] is False
    assert attributes["search_records"]["has_free_form_input"] is True
    # The two server-level facts the table addresses, on every point.
    assert attributes["get_guide"]["second_tenant_env"] == (
        "REFERENCE_MCP_TOKEN_TENANT_B"
    )
    assert attributes["get_guide"]["audit_sink"] == "stderr-json"
    # The drift hashes are attributes, not identity.
    assert len(attributes["delete_record"]["schema_hash"]) == 12
    assert len(attributes["delete_record"]["description_hash"]) == 12
    assert attributes["delete_record"]["annotations"]["destructiveHint"] is True


# --- the cross-check refuses rather than choosing -----------------------------


def test_a_stale_declared_class_refuses_the_plan(workdir: Path) -> None:
    """The one case where ITest will not generate anything: the declaration says
    `read` and the server says `destructive`. One of the two is wrong."""
    _declare(
        workdir,
        tools={"delete_record": {"approval": "confirm_param", "mutation": "read"}},
    )
    result = _plan()
    assert result.exit_code == 2, result.output
    assert "delete_record" in result.output
    assert "detected: destructive" in result.output
    assert "source: annotation" in result.output
    assert "declared: read" in result.output
    # Nothing was written: not the manifest, and not even the plan.
    assert not (workdir / ".itest" / "manifest.yaml").exists()
    assert not (workdir / ".itest" / "plan.json").exists()


def test_a_conflict_leaves_an_existing_manifest_untouched(workdir: Path) -> None:
    assert _sync().exit_code == 0
    before = (workdir / ".itest" / "manifest.yaml").read_text(encoding="utf-8")

    _declare(
        workdir,
        tools={"delete_record": {"approval": "confirm_param", "mutation": "read"}},
    )
    assert _plan().exit_code == 2
    assert _sync().exit_code == 2
    assert (workdir / ".itest" / "manifest.yaml").read_text(encoding="utf-8") == before


def test_a_declared_trait_the_table_cannot_place_is_refused(workdir: Path) -> None:
    _declare(workdir, tools={"get_guide": {"traits": ["Z9"]}})
    result = _plan()
    assert result.exit_code == 2, result.output
    assert "Z9" in result.output
    assert "get_guide" in result.output


# --- drift: an attribute changed, the identity did not ------------------------


def _variant_server(workdir: Path) -> Path:
    """A copy of the reference server with one tool's description rewritten."""
    original = SERVER_PATH.read_text(encoding="utf-8")
    marker = 'description="Fetch one record by id."'
    assert marker in original
    variant = workdir / "variant_server.py"
    variant.write_text(
        original.replace(marker, 'description="Fetch one record by id, quickly."'),
        encoding="utf-8",
    )
    return variant


def test_a_reworded_description_is_changed_not_new(workdir: Path) -> None:
    assert _sync().exit_code == 0
    manifest = load_manifest(workdir / ".itest" / "manifest.yaml")
    before = next(p for p in manifest.points if p.target == "fetch_record")
    stub_text = (workdir / ACTIVE_FILE).read_text(encoding="utf-8")

    document = yaml.safe_load(
        (workdir / ".itest" / "tools" / "reference-mcp.yaml").read_text()
    )
    document["transport"]["command"] = [sys.executable, str(_variant_server(workdir))]
    (workdir / ".itest" / "tools" / "reference-mcp.yaml").write_text(
        yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
    )

    result = _plan()
    assert result.exit_code == 0, result.output
    assert "1 changed" in result.output
    assert "0 new" in result.output
    assert "description_hash" in result.output
    assert "fetch_record" in result.output

    payload = json.loads(_plan("--output", "json").output)
    assert [p["target"] for p in payload["changed_points"]] == ["fetch_record"]
    changed = payload["changed_points"][0]
    # The identity held; only the attribute moved.
    assert changed["id"] == before.id
    assert (
        changed["attributes"]["description_hash"]
        != (before.attributes["description_hash"])
    )
    assert changed["attributes"]["schema_hash"] == before.attributes["schema_hash"]

    # Sync records the new hash, orphans nothing, and adds no stub.
    result = _sync()
    assert result.exit_code == 0, result.output
    assert "added 0 stub(s)" in result.output
    assert "flagged 0 orphan(s)" in result.output
    manifest = load_manifest(workdir / ".itest" / "manifest.yaml")
    after = next(p for p in manifest.points if p.target == "fetch_record")
    assert after.id == before.id
    assert (
        after.attributes["description_hash"] != (before.attributes["description_hash"])
    )
    assert all(t.status != "orphaned" for t in manifest.tests)
    assert (workdir / ACTIVE_FILE).read_text(encoding="utf-8") == stub_text


def test_a_read_tool_that_turns_destructive_is_not_missed(workdir: Path) -> None:
    """Same name, same schema, same description; only the annotation moved from
    read-only to destructive. Under `mutation: detect` that is drift on the
    class itself — `changed`, named in the plan — and the traits the new class
    makes applicable (B2 destructive gating, B4 audit) are gained by the
    existing tool. What must not happen is a quiet `unchanged`."""
    assert _sync().exit_code == 0
    before = next(
        p
        for p in load_manifest(workdir / ".itest" / "manifest.yaml").points
        if p.target == "fetch_record"
    )
    assert before.attributes["mutation"] == "read"

    original = SERVER_PATH.read_text(encoding="utf-8")
    marker = (
        'description="Fetch one record by id.",\n'
        "        annotations=ToolAnnotations(readOnlyHint=True),"
    )
    assert marker in original
    variant = workdir / "destructive_fetch_server.py"
    variant.write_text(
        original.replace(
            marker,
            'description="Fetch one record by id.",\n'
            "        annotations=ToolAnnotations("
            "readOnlyHint=False, destructiveHint=True),",
        ),
        encoding="utf-8",
    )
    path = workdir / ".itest" / "tools" / "reference-mcp.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["transport"]["command"] = [sys.executable, str(variant)]
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    result = _plan("--output", "json")
    # `mutation: detect`: the flip is drift, not a conflict.
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    live = next(
        p
        for p in payload["unchanged_points"] + payload["changed_points"]
        if p["target"] == "fetch_record"
    )
    # The live listing is read correctly, and the hashes did not move ...
    assert live["attributes"]["mutation"] == "destructive"
    assert live["attributes"]["schema_hash"] == before.attributes["schema_hash"]
    assert (
        live["attributes"]["description_hash"] == before.attributes["description_hash"]
    )
    # ... so the only thing that can report it is drift on the class itself.
    assert "fetch_record" in [p["target"] for p in payload["changed_points"]]
    assert "mutation" in payload["changed_attributes"][before.id]
    gained = {
        (c["tool"], c["trait"])
        for c in payload["trait_changes"]
        if c["change"] == "gained"
    }
    assert {("fetch_record", "B2"), ("fetch_record", "B4")} <= gained

    human = _plan()
    assert "mutation: read → destructive (annotation" in human.output
    assert "+B2 on reference-mcp/fetch_record (rule: mutation == destructive)" in (
        human.output
    )

    result = _sync()
    assert result.exit_code == 0, result.output
    assert "test_fetch_record__B2" in _functions(workdir / ACTIVE_FILE)
    after = next(
        p
        for p in load_manifest(workdir / ".itest" / "manifest.yaml").points
        if p.target == "fetch_record"
    )
    assert after.id == before.id
    assert "B2" in after.traits_planned


def test_an_override_for_a_tool_the_server_does_not_list_is_orphaned(
    workdir: Path,
) -> None:
    _declare(
        workdir,
        tools={
            "delete_record": {"approval": "confirm_param"},
            "renamed_away": {"approval": "human"},
        },
    )
    result = _plan()
    assert result.exit_code == 0, result.output
    assert "Orphaned tool overrides (1)" in result.output
    assert "renamed_away" in result.output

    payload = json.loads(_plan("--output", "json").output)
    assert payload["orphaned_tool_overrides"] == [
        {
            "server": "reference-mcp",
            "tool": "renamed_away",
            "declaration": ".itest/tools/reference-mcp.yaml",
        }
    ]
    # Flagged, never deleted: the declaration still says what it said.
    document = yaml.safe_load(
        (workdir / ".itest" / "tools" / "reference-mcp.yaml").read_text()
    )
    assert "renamed_away" in document["tools"]
    # And it is not a point: the server does not have the tool.
    assert all(p["target"] != "renamed_away" for p in payload["new_points"])


# --- sync: the stub set comes from the table ---------------------------------

#: What sync writes for `delete_record` (destructive, free-form input, audited,
#: with a second tenant declared, on a server that runs as a service identity).
#: Only the table's *generated* traits get a per-tool stub, and all of them
#: are active-tier; the engine traits are recorded in `traits_planned` and run
#: by the engine from the manifest, with no per-tool code.
DELETE_STUBS = {
    "test_delete_record__A2",
    "test_delete_record__A3",
    "test_delete_record__B2",
    "test_delete_record__B4",
}
DELETE_ENGINE = ["A1", "B1", "C1", "C2", "C3", "D1", "D2", "D3"]


def _traits_planned(workdir: Path) -> dict[str, list[str]]:
    manifest = load_manifest(workdir / ".itest" / "manifest.yaml")
    return {p.target: p.traits_planned for p in manifest.points}


def test_sync_generates_one_stub_per_tool_and_applicable_generated_trait(
    workdir: Path,
) -> None:
    result = _sync()
    assert result.exit_code == 0, result.output
    # A2 on the seven non-informational tools, A3 on all eight, B2 on the one
    # destructive tool, B4 on the three audited writes.
    assert "added 19 stub(s)" in result.output

    active = _functions(workdir / ACTIVE_FILE)
    assert len(active) == 19
    assert not (workdir / READONLY_FILE).exists()  # no generated readonly trait
    assert DELETE_STUBS <= active
    planned = _traits_planned(workdir)
    assert set(DELETE_ENGINE) <= set(planned["delete_record"])
    # get_guide is informational and takes no arguments: no isolation check, no
    # containment check, no gating, no egress, no audit. The identity check
    # still applies: the server acts as a service identity for every tool.
    assert planned["get_guide"] == ["A1", "A3", "B1", "D1", "D2", "D3"]
    assert {f for f in active if f.startswith("test_get_guide__")} == {
        "test_get_guide__A3"
    }
    # B3 is the one egress check, and only `enrich` declares an egress edge.
    assert [t for t, traits in planned.items() if "B3" in traits] == ["enrich"]


def test_the_active_tier_lives_in_its_own_file(
    workdir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """So a disallowed environment never imports a mutating probe: verify
    --ignore-s a file whose every test is gated, and can only --deselect one
    sharing a file with runnable siblings. Every shipped generated trait is
    active, so B3 is made a generated readonly trait here to show the split."""
    _edit_table(tmp_path, monkeypatch, B3={"kind": "generated"})
    assert _sync().exit_code == 0
    manifest = load_manifest(workdir / ".itest" / "manifest.yaml")
    by_path: dict[str, set[str]] = {}
    for test in manifest.tests:
        by_path.setdefault(test.path, set()).add(test.tier)
    assert by_path[READONLY_FILE] == {"readonly"}
    assert by_path[ACTIVE_FILE] == {"active"}
    assert _functions(workdir / READONLY_FILE) == {"test_enrich__B3"}


def test_a_stub_records_its_point_id_and_its_trait_id(workdir: Path) -> None:
    """P31's recipes and P32's ledger both find a check by its trait id, so the
    frozen docstring carries the address: the point, the trait, and the schema
    hash the binding was generated against."""
    assert _sync().exit_code == 0
    text = (workdir / ACTIVE_FILE).read_text(encoding="utf-8")
    point = next(
        p
        for p in load_manifest(workdir / ".itest" / "manifest.yaml").points
        if p.target == "delete_record"
    )
    assert point.id == tool_point_id("reference-mcp", "delete_record")
    block = text.split("def test_delete_record__B2(")[1]
    assert (
        f'"""itest point: {point.id}  trait: B2  '
        f'schema: {point.attributes["schema_hash"]}"""' in block
    )


def test_a_second_sync_adds_nothing(workdir: Path) -> None:
    assert _sync().exit_code == 0
    before = (workdir / ACTIVE_FILE).read_text(encoding="utf-8")
    result = _sync()
    assert result.exit_code == 0, result.output
    assert "No changes to apply" in result.output
    assert (workdir / ACTIVE_FILE).read_text(encoding="utf-8") == before


def _edit_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **edits: str | dict
) -> None:
    """Serve an edited copy of the shipped table where ``importlib.resources``
    would find it. ``B3="always"`` replaces a row's ``applies_when``;
    ``B3={"kind": "generated"}`` updates any of its fields."""
    table = yaml.safe_load(
        traits_module.resources.files(traits_module.TABLE_PACKAGE)
        .joinpath(traits_module.TABLE_RESOURCE)
        .read_text(encoding="utf-8")
    )
    for trait in table["traits"]:
        edit = edits.get(trait["id"])
        if isinstance(edit, str):
            trait["applies_when"] = edit
        elif edit:
            trait.update(edit)
    # A sibling of tmp_path, never inside it: tmp_path is the checkout under
    # test, and the edited table must not become a file sync or pytest sees.
    directory = tmp_path.parent / f"{tmp_path.name}-edited-table"
    directory.mkdir(exist_ok=True)
    (directory / traits_module.TABLE_RESOURCE).write_text(
        yaml.safe_dump(table, sort_keys=False), encoding="utf-8"
    )
    monkeypatch.setattr(
        traits_module, "resources", SimpleNamespace(files=lambda _pkg: directory)
    )


def test_editing_one_applies_when_line_changes_the_generated_suite(
    workdir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The proof that the rule is data. A2 withholds tenant isolation from the
    informational tool; make it apply always and every tool gets the stub. B3
    applies only where egress is declared; make it apply always and every tool
    gets the (engine) check."""
    _edit_table(tmp_path, monkeypatch, A2="always", B3="always")

    assert _sync().exit_code == 0
    active = _functions(workdir / ACTIVE_FILE)
    assert {f for f in active if f.endswith("__A2")} == {
        f"test_{name}__A2" for name in EXPECTED_TOOLS
    }
    assert len(active) == 19 + 1
    assert all("B3" in traits for traits in _traits_planned(workdir).values())


def test_a_tool_with_active_false_gets_no_active_stubs(workdir: Path) -> None:
    """Withholding the active tier for one tool is a declaration away, and it
    withholds the stubs rather than generating ones that must not run."""
    _declare(
        workdir,
        tools={
            "delete_record": {"approval": "confirm_param", "active": False},
        },
    )
    assert _sync().exit_code == 0
    active = _functions(workdir / ACTIVE_FILE)
    assert not {f for f in active if f.startswith("test_delete_record__")}
    # The readonly (engine) traits still apply; only the active ones went.
    assert _traits_planned(workdir)["delete_record"] == [
        "A1",
        "B1",
        "C3",
        "D1",
        "D2",
        "D3",
    ]


def test_none_of_these_withholds_every_check_for_a_tool(workdir: Path) -> None:
    _declare(
        workdir,
        tools={
            "get_guide": {
                "traits": ["none-of-these"],
                "notes": "Static prose. Nothing to authorise, mutate or leak.",
            }
        },
    )
    assert _sync().exit_code == 0
    generated = _functions(workdir / READONLY_FILE) | _functions(workdir / ACTIVE_FILE)
    assert not {f for f in generated if f.startswith("test_get_guide__")}
    assert _traits_planned(workdir)["get_guide"] == []
    # The point still exists: the tool is inventory whether or not it is checked.
    manifest = load_manifest(workdir / ".itest" / "manifest.yaml")
    assert any(p.target == "get_guide" for p in manifest.points)


def test_a_hand_picked_trait_list_replaces_the_table(workdir: Path) -> None:
    _declare(workdir, tools={"enrich": {"traits": ["A2", "B3", "D1"]}})
    assert _sync().exit_code == 0
    generated = _functions(workdir / READONLY_FILE) | _functions(workdir / ACTIVE_FILE)
    assert {f for f in generated if f.startswith("test_enrich__")} == {
        "test_enrich__A2"
    }
    assert _traits_planned(workdir)["enrich"] == ["A2", "B3", "D1"]


# --- an unreachable server is a line, not a crash ----------------------------


def test_an_http_server_with_no_url_variable_is_reported_and_skipped(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OTHER_MCP_URL", raising=False)
    (workdir / ".itest" / "tools" / "other-mcp.yaml").write_text(
        yaml.safe_dump(
            {
                "server": "other-mcp",
                "transport": {"kind": "http", "url_env": "OTHER_MCP_URL"},
                "sentinels": {"nonexistent_id": "sentinel-0000"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    result = _plan()
    assert result.exit_code == 0, result.output
    assert "unreachable: OTHER_MCP_URL not set" in result.output
    assert "other-mcp" in result.output
    # The reachable server was still planned in full.
    payload = json.loads(_plan("--output", "json").output)
    assert len([p for p in payload["new_points"] if p["type"] == "mcp_tool"]) == 8
    assert payload["unreachable_servers"] == {
        "other-mcp": "unreachable: OTHER_MCP_URL not set"
    }


def test_a_server_that_cannot_be_launched_is_reported_and_skipped(
    workdir: Path,
) -> None:
    """A command that is not an MCP server at all. The plan says so and goes on:
    a broken server must not blind the rest of the project."""
    script = workdir / "not_a_server.py"
    script.write_text("raise SystemExit('boom')\n", encoding="utf-8")
    document = yaml.safe_load(
        (workdir / ".itest" / "tools" / "reference-mcp.yaml").read_text()
    )
    document["transport"]["command"] = [sys.executable, str(script)]
    (workdir / ".itest" / "tools" / "reference-mcp.yaml").write_text(
        yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
    )
    result = _plan()
    assert result.exit_code == 0, result.output
    assert "reference-mcp" in result.output
    assert "unreachable" in result.output


def _make_unreachable(workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rewire the declared server to http behind a url variable that is unset."""
    monkeypatch.delenv("REFERENCE_MCP_URL", raising=False)
    path = workdir / ".itest" / "tools" / "reference-mcp.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["transport"] = {"kind": "http", "url_env": "REFERENCE_MCP_URL"}
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def test_an_unreachable_server_keeps_its_recorded_points_and_tests(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No evidence is not evidence of absence. A server ITest could not ask has
    not lost its tools, so nothing recorded about it may be orphaned or dropped."""
    assert _sync().exit_code == 0
    manifest_file = workdir / ".itest" / "manifest.yaml"
    before_text = manifest_file.read_text(encoding="utf-8")
    before = load_manifest(manifest_file)
    recorded_ids = {p.id for p in before.points if p.source == "reference-mcp"}
    assert len(recorded_ids) == 8

    _make_unreachable(workdir, monkeypatch)

    payload = json.loads(_plan("--output", "json").output)
    assert payload["unreachable_servers"] == {
        "reference-mcp": "unreachable: REFERENCE_MCP_URL not set"
    }
    assert payload["orphan_candidates"] == []
    assert {p["id"] for p in payload["held_points"]} == recorded_ids

    # Without the flag, sync names the server, fails, and writes nothing.
    result = _sync()
    assert result.exit_code == 1, result.output
    assert "reference-mcp" in result.output
    assert "unreachable: REFERENCE_MCP_URL not set" in result.output
    assert manifest_file.read_text(encoding="utf-8") == before_text


def test_allow_unreachable_applies_the_rest_and_holds_the_server_as_recorded(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the flag, the reachable side is applied (the alex state adds its
    fourteen points) while the unreachable server's points stay byte-for-byte
    what the manifest last recorded — not re-stamped as seen, not orphaned."""
    assert _sync().exit_code == 0
    before = load_manifest(workdir / ".itest" / "manifest.yaml")
    held_before = {p.id: p for p in before.points if p.source == "reference-mcp"}

    _make_unreachable(workdir, monkeypatch)

    result = runner.invoke(
        app,
        ["sync", "--auto-approve", "--allow-unreachable", "--tf-json", str(ALEX)],
    )
    assert result.exit_code == 0, result.output
    assert "reference-mcp" in result.output
    assert "flagged 0 orphan(s)" in result.output

    after = load_manifest(workdir / ".itest" / "manifest.yaml")
    held_after = {p.id: p for p in after.points if p.source == "reference-mcp"}
    assert held_after == held_before
    assert len([p for p in after.points if p.type != "mcp_tool"]) == 14
    assert all(t.status != "orphaned" for t in after.tests)
    assert {t.id for t in before.tests} <= {t.id for t in after.tests}


# --- itest add --server ------------------------------------------------------


def _handwritten(workdir: Path) -> Path:
    path = workdir / "itest_tests" / "test_by_hand.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "def test_delete_record_refuses_anonymous():\n"
        "    assert True  # a real check, written by a human\n",
        encoding="utf-8",
    )
    return path


def test_add_registers_a_hand_written_test_onto_a_tool_point(workdir: Path) -> None:
    assert _sync().exit_code == 0
    _handwritten(workdir)
    result = runner.invoke(
        app,
        [
            "add",
            "--server",
            "reference-mcp",
            "--point",
            "delete_record",
            "--file",
            "itest_tests/test_by_hand.py",
            "--function",
            "test_delete_record_refuses_anonymous",
            "--tier",
            "readonly",
        ],
    )
    assert result.exit_code == 0, result.output
    manifest = load_manifest(workdir / ".itest" / "manifest.yaml")
    entry = next(
        t
        for t in manifest.tests
        if t.test_name == "test_delete_record_refuses_anonymous"
    )
    assert entry.point_id == tool_point_id("reference-mcp", "delete_record")
    assert entry.status == "implemented"  # no generated skip line in the body
    assert entry.tier == "readonly"


def test_add_with_an_unknown_tool_names_the_tools_that_exist(workdir: Path) -> None:
    assert _sync().exit_code == 0
    _handwritten(workdir)
    result = runner.invoke(
        app,
        [
            "add",
            "--server",
            "reference-mcp",
            "--point",
            "no_such_tool",
            "--file",
            "itest_tests/test_by_hand.py",
            "--function",
            "test_delete_record_refuses_anonymous",
            "--tier",
            "readonly",
        ],
    )
    assert result.exit_code == 2
    assert "no_such_tool" in result.output
    assert "delete_record" in result.output


def test_add_still_refuses_a_function_that_is_not_in_the_file(workdir: Path) -> None:
    """P23's AST validation, unchanged: the file is parsed, never imported."""
    assert _sync().exit_code == 0
    _handwritten(workdir)
    result = runner.invoke(
        app,
        [
            "add",
            "--server",
            "reference-mcp",
            "--point",
            "delete_record",
            "--file",
            "itest_tests/test_by_hand.py",
            "--function",
            "test_does_not_exist",
            "--tier",
            "readonly",
        ],
    )
    assert result.exit_code == 2
    assert "not defined" in result.output


# --- a project with no declarations does not move -----------------------------


def test_a_declaration_free_project_plans_exactly_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The alex regression. No declarations, no new clauses, no new sections:
    the rollup line is the one it always was, to the byte."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["plan", "--tf-json", str(ALEX)])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0] == "ITest plan: 14 new, 0 unchanged, 0 orphaned test(s)."
    # Every new clause and section is append-only: none of them appear.
    # ("unchanged" contains "changed", so the clause is matched with its comma.)
    for absent in (" changed,", "Changed (", "unreachable", "Orphaned tool"):
        assert absent not in result.output

    result = runner.invoke(app, ["sync", "--auto-approve", "--tf-json", str(ALEX)])
    assert result.exit_code == 0, result.output
    manifest = load_manifest(tmp_path / ".itest" / "manifest.yaml")
    assert manifest.schema_version == 2
    assert all(p.type != "mcp_tool" for p in manifest.points)
    assert len(manifest.points) == 14


def test_declared_and_detected_points_coexist(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real project has both. The terraform side is untouched by declarations."""
    result = runner.invoke(app, ["sync", "--auto-approve", "--tf-json", str(ALEX)])
    assert result.exit_code == 0, result.output
    manifest = load_manifest(workdir / ".itest" / "manifest.yaml")
    kinds = {p.type for p in manifest.points}
    assert "mcp_tool" in kinds
    assert len([p for p in manifest.points if p.type == "mcp_tool"]) == 8
    assert len([p for p in manifest.points if p.type != "mcp_tool"]) == 14


def test_the_example_declaration_is_the_one_under_test(workdir: Path) -> None:
    """Guard against the test fixture drifting away from the committed example:
    the only thing rewritten is the argv that launches the server."""
    (declaration,) = load_declarations(workdir)
    committed = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    assert declaration.model_dump(mode="json")["tools"].keys() == (
        committed["tools"].keys()
    )
    assert (
        declaration.sentinels.nonexistent_id == committed["sentinels"]["nonexistent_id"]
    )


# --- the generated suite meets the rest of the engine -------------------------


def test_the_duplicated_point_type_constant_cannot_drift(workdir: Path) -> None:
    """sync spells "mcp_tool" out rather than importing it, so that a
    terraform-only sync never loads the declarations package. This is the test
    that keeps the two spellings the same one."""
    from itest.core import syncer
    from itest.core.declarations import tools as declared_tools

    assert syncer._TOOL_POINT_TYPE == declared_tools.POINT_TYPE


def test_verify_gates_the_active_tool_checks_off_the_safe_floor(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reason the active tier got its own file. A policy exists and nothing is
    bound, so the floor is static+readonly: every active check — the active
    engine module and every generated binding — is withheld at collection time
    and said so, while the readonly engine checks still run — for real."""
    monkeypatch.delenv("REFERENCE_MCP_TOKEN", raising=False)
    assert _sync().exit_code == 0
    result = runner.invoke(app, ["verify"])
    # reference-mcp over stdio has no guard: A1 fails its five read tools.
    assert result.exit_code == 1, result.output
    assert "8 integration points" in result.output
    assert "33 gated test(s) withheld by this environment" in result.output
    assert "Ran 48 tests" in result.output
    assert "No environment bound: running the safe floor" in result.output
    # Every point reports the coverage it has. The read tools fail A1. The
    # mutating tools pass on the listing checks (B1, D1-D3, run on the anonymous
    # listing); their A1 is deferred to the active tier and skips, which is not
    # a pass and is not a fail.
    assert result.output.count("[FAIL] reference-mcp -> ") == 5
    assert result.output.count("[PASS] reference-mcp -> ") == 3
    for tool in ("create_record", "update_record", "delete_record"):
        assert f"[PASS] reference-mcp -> {tool} " in result.output


def test_verify_runs_the_active_checks_when_the_environment_allows_them(
    workdir: Path,
) -> None:
    assert _sync().exit_code == 0
    result = runner.invoke(app, ["verify", "--environment", "staging"])
    assert result.exit_code == 1, result.output  # A1's real findings, not errors
    assert "0 errored" in result.output
    assert "gated" not in result.output
    assert "Ran 81 tests" in result.output
