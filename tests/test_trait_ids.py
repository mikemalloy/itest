"""Trait ids are readable slugs, with a display code and standards mappings.

The AN-style ids (A1, B2, …) read as OWASP ids to a security reader, and they
are ours. Every trait is now addressed by a family-prefixed slug
(``authority.anonymous``); ``code`` (``AUTH-1``) is a display label for narrow
columns and never an identity; ``standards`` lists the published ids the trait
answers. What was written with the old ids — a manifest, a verify ledger, a
declaration's ``traits:`` list, a generated binding's call — still loads: the
old id is mapped to the new one on read, once, and a sync writes the new one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from test_declarations_plan_sync import _sync, make_workdir, runner

from itest.cli import app
from itest.core.declarations.traits import TraitTableError, load_traits
from itest.core.manifest import load_manifest, save_manifest
from itest.report import model as report_model
from itest.report import render as report_render
from itest.traits.ids import LEGACY_TRAIT_IDS, migrate_trait_id, trait_ident

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TOOL_LEDGER = FIXTURES / "report" / "tool-ledger.json"

#: The canonical map, old id -> slug. Pinned: a change here is a rename every
#: manifest in the field has to survive.
CANONICAL = {
    "A1": "authority.anonymous",
    "A2": "authority.tenant_isolation",
    "A3": "authority.backing_least_privilege",
    "A4": "authority.delegation",
    "B1": "blast.mutation_class",
    "B2": "blast.destructive_gating",
    "B3": "blast.egress",
    "B4": "blast.audit",
    "C1": "containment.parameter_scope",
    "C2": "containment.expression_passthrough",
    "C3": "containment.output_hygiene",
    "D1": "change.inventory",
    "D2": "change.schema_drift",
    "D3": "change.description_drift",
}

#: slug -> (code, family, standards). The published ids each trait answers,
#: verified 2026-09-11 against the 2026 lists: OWASP Top 10 for Agentic
#: Applications (ASI..), OWASP LLM Top 10 2026 (LLM.. — Excessive Agency is
#: LLM03 in 2026), Semgrep MCP cheatsheet rows, CWE, and the OWASP Agent
#: Control Standard's AgBOM. Every row carries at least one.
TABLE = {
    "authority.anonymous": (
        "AUTH-1",
        "authority",
        ["ASI03", "LLM02", "semgrep-server-4", "CWE-306"],
    ),
    "authority.tenant_isolation": (
        "AUTH-2",
        "authority",
        ["ASI03", "LLM02", "semgrep-server-18", "CWE-639"],
    ),
    "authority.backing_least_privilege": (
        "AUTH-3",
        "authority",
        ["ASI03", "LLM03", "CWE-269"],
    ),
    "authority.delegation": (
        "AUTH-4",
        "authority",
        ["ASI03", "semgrep-server-8", "CWE-441"],
    ),
    "blast.mutation_class": ("BLAST-1", "blast", ["ASI02", "LLM03", "ACS-AgBOM"]),
    "blast.mutation_class_observed": ("BLAST-1b", "blast", ["ASI02", "LLM03"]),
    "blast.destructive_gating": ("BLAST-2", "blast", ["ASI02", "LLM03"]),
    "blast.egress": ("BLAST-3", "blast", ["ASI04", "LLM02", "semgrep-server-21"]),
    "blast.audit": ("BLAST-4", "blast", ["ACS-AgBOM", "CWE-778"]),
    "containment.parameter_scope": (
        "CONTAIN-1",
        "containment",
        ["ASI02", "semgrep-server-19", "CWE-639"],
    ),
    "containment.expression_passthrough": (
        "CONTAIN-2",
        "containment",
        ["ASI05", "semgrep-server-20", "semgrep-server-22", "CWE-77", "CWE-89"],
    ),
    "containment.output_hygiene": (
        "CONTAIN-3",
        "containment",
        ["LLM02", "LLM10", "CWE-209"],
    ),
    "change.inventory": (
        "CHANGE-1",
        "change",
        ["ASI04", "LLM04", "semgrep-client-1", "ACS-AgBOM"],
    ),
    "change.schema_drift": ("CHANGE-2", "change", ["ASI04", "LLM04", "ACS-AgBOM"]),
    "change.description_drift": (
        "CHANGE-3",
        "change",
        ["ASI04", "ASI01", "LLM01", "semgrep-client-2"],
    ),
}


# --- the ids ----------------------------------------------------------------


def test_the_legacy_map_is_the_canonical_one() -> None:
    assert LEGACY_TRAIT_IDS == CANONICAL


def test_a_slug_becomes_an_identifier_by_one_rule() -> None:
    """Where a function or file name cannot hold a dot, the dot becomes a double
    underscore — in one helper, so every generated name agrees."""
    assert trait_ident("authority.anonymous") == "authority__anonymous"
    assert trait_ident("blast.destructive_gating") == "blast__destructive_gating"
    for slug in CANONICAL.values():
        assert "." not in trait_ident(slug)
        assert trait_ident(slug).isidentifier()


def test_an_old_id_migrates_and_a_new_one_is_left_alone() -> None:
    assert migrate_trait_id("A1") == "authority.anonymous"
    assert migrate_trait_id("authority.anonymous") == "authority.anonymous"
    assert migrate_trait_id("something.else") == "something.else"


# --- the table --------------------------------------------------------------


def test_the_shipped_table_is_slugs_codes_and_standards() -> None:
    table = load_traits()
    assert table.ids == list(TABLE)
    # Every old id maps to a row; a row added after the rename has no old id.
    assert set(CANONICAL.values()) <= set(table.ids)
    assert table.families == {
        "authority": "Authority",
        "blast": "Blast radius",
        "containment": "Containment",
        "change": "Change",
    }
    for trait in table.traits:
        code, family, standards = TABLE[trait.id]
        assert (trait.code, trait.family, trait.standards) == (
            code,
            family,
            standards,
        ), trait.id
        assert trait.id.startswith(f"{trait.family}.")


def _table(tmp_path: Path, **changes: object) -> Path:
    extra = changes.pop("_extra", [])
    row = {
        "id": "authority.anonymous",
        "code": "AUTH-1",
        "family": "authority",
        "name": "refuses anonymous",
        "applies_when": "always",
        "tier": "readonly",
        "kind": "engine",
        "recipe": "tool_authn.md",
        "standards": ["ASI03"],
    }
    row.update(changes)
    rows = [row, *extra]
    document = {
        "version": 1,
        "families": {"authority": "Authority", "blast": "Blast radius"},
        "traits": rows,
    }
    path = tmp_path / "traits.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "typo",
    [
        "ASl03",
        "LMM02",
        "semgrep4",
        "semgrep-foo-1",  # a tab the cheatsheet does not have
        "semgrep-server-",  # no row
        "OWASP-A01",
        "cwe-79",
        "CWE-",
        "ACS-",
    ],
)
def test_a_standards_typo_is_refused_naming_the_value(
    tmp_path: Path, typo: str
) -> None:
    with pytest.raises(TraitTableError) as excinfo:
        load_traits(_table(tmp_path, standards=["ASI03", typo]))
    message = str(excinfo.value)
    assert repr(typo) in message and "authority.anonymous" in message
    for prefix in ("ASI", "LLM", "semgrep-server-", "semgrep-client-", "CWE-", "ACS-"):
        assert prefix in message


def test_every_known_standards_prefix_and_an_empty_list_load(tmp_path: Path) -> None:
    ok = [
        "ASI03",
        "LLM02",
        "semgrep-server-4",
        "semgrep-client-1",
        "CWE-862",
        "ACS-AgBOM",
    ]
    assert load_traits(_table(tmp_path, standards=ok)).traits[0].standards == ok
    assert load_traits(_table(tmp_path, standards=[])).traits[0].standards == []


def test_every_shipped_row_carries_at_least_one_standard() -> None:
    for trait in load_traits().traits:
        assert trait.standards, trait.id


def test_an_id_outside_its_family_is_refused(tmp_path: Path) -> None:
    with pytest.raises(TraitTableError) as excinfo:
        load_traits(_table(tmp_path, id="blast.anonymous"))
    assert "blast.anonymous" in str(excinfo.value)
    assert "authority" in str(excinfo.value)


def test_an_old_style_id_is_refused_in_the_table(tmp_path: Path) -> None:
    with pytest.raises(TraitTableError) as excinfo:
        load_traits(_table(tmp_path, id="A1"))
    assert "'A1'" in str(excinfo.value)


def test_a_malformed_or_duplicate_code_is_refused(tmp_path: Path) -> None:
    with pytest.raises(TraitTableError) as excinfo:
        load_traits(_table(tmp_path, code="auth1"))
    assert "auth1" in str(excinfo.value)
    twin = {
        "id": "blast.egress",
        "code": "AUTH-1",
        "family": "blast",
        "name": "egress",
        "applies_when": "always",
        "tier": "readonly",
        "kind": "engine",
        "recipe": "tool_egress.md",
    }
    with pytest.raises(TraitTableError) as excinfo:
        load_traits(_table(tmp_path, _extra=[twin]))
    assert "AUTH-1" in str(excinfo.value)


# --- migration: a manifest written with the old ids ---------------------------------


LEGACY_MANIFEST = """\
schema_version: 2
generated_at: '2026-09-11T00:00:00Z'
trait_table_hash: 5458224b3c52
points:
- id: p1
  type: mcp_tool
  source: srv
  target: tool
  attributes:
    traits: [A1, B2]
  traits_planned: [A1, B2, D3]
  hcl_address: .itest/tools/srv.yaml
  origin: declared
  first_seen: '2026-09-11T00:00:00Z'
  last_seen: '2026-09-11T00:00:00Z'
tests:
- id: e-p1-a1
  point_id: p1
  path: itest_tests/tools_srv/test_srv__engine.py
  test_name: test_engine[tool-A1]
  trait: A1
  ownership_hash: h1
  status: implemented
- id: t-p1-b2
  point_id: p1
  path: itest_tests/tools_srv/test_srv__generated_active.py
  test_name: test_tool__B2
  trait: B2
  ownership_hash: h2
  status: implemented
  tier: active
- id: t-p1-d3
  point_id: p1
  path: itest_tests/test_tools_srv.py
  test_name: test_d3_tool
  ownership_hash: h3
"""


def test_a_manifest_with_old_ids_loads_with_new_ones(tmp_path: Path) -> None:
    path = tmp_path / "manifest.yaml"
    path.write_text(LEGACY_MANIFEST, encoding="utf-8")
    manifest = load_manifest(path)
    (point,) = manifest.points
    assert point.traits_planned == [
        "authority.anonymous",
        "blast.destructive_gating",
        "change.description_drift",
    ]
    assert point.attributes["traits"] == [
        "authority.anonymous",
        "blast.destructive_gating",
    ]
    engine, binding, p30 = manifest.tests
    # An engine case's name is the manifest's own (the engine module names no
    # trait), so it moves to the new id; so does its id.
    assert engine.test_name == "test_engine[tool-authority.anonymous]"
    assert engine.id == "e-p1-authority.anonymous"
    assert engine.trait == "authority.anonymous"
    # A binding's name is a function on disk: kept. Its trait moves.
    assert binding.test_name == "test_tool__B2"
    assert binding.trait == "blast.destructive_gating"
    assert p30.trait is None  # a P30 stub learns its trait from its id at sync

    saved = tmp_path / "saved.yaml"
    save_manifest(manifest, saved)
    text = saved.read_text(encoding="utf-8")
    assert "trait: A1" not in text and "- A1" not in text
    assert "[tool-A1]" not in text


def test_a_declaration_free_manifest_is_untouched_by_the_migration(
    tmp_path: Path,
) -> None:
    source = FIXTURES / "p30-manifests" / "alex-s6" / ".itest" / "manifest.yaml"
    save_manifest(load_manifest(source), tmp_path / "manifest.yaml")
    assert (tmp_path / "manifest.yaml").read_bytes() == source.read_bytes()


def _to_legacy(text: str) -> str:
    """A P31-era manifest: the same document written with the old ids."""
    for old, new in sorted(CANONICAL.items(), key=lambda kv: -len(kv[1])):
        text = text.replace(f"-{new}]", f"-{old}]")  # engine case names
        text = text.replace(f"-{new}\n", f"-{old.lower()}\n")  # entry ids
        text = text.replace(f"trait: {new}\n", f"trait: {old}\n")
        text = text.replace(f"- {new}\n", f"- {old}\n")
    return text


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    workdir = make_workdir(tmp_path, monkeypatch)
    assert _sync().exit_code == 0
    return workdir


def test_a_legacy_manifest_is_rewritten_by_the_next_sync(workdir: Path) -> None:
    manifest_file = workdir / ".itest" / "manifest.yaml"
    legacy = _to_legacy(manifest_file.read_text(encoding="utf-8"))
    assert "trait: D1\n" in legacy and "[get_guide-D1]" in legacy
    manifest_file.write_text(legacy, encoding="utf-8")

    result = _sync()
    assert result.exit_code == 0, result.output
    text = manifest_file.read_text(encoding="utf-8")
    for old in CANONICAL:
        assert f"trait: {old}\n" not in text, old
        assert f"- {old}\n" not in text, old
        assert f"-{old}]" not in text, old
    # Nothing was re-registered twice: one engine case per (tool, engine trait).
    manifest = load_manifest(manifest_file)
    names = [t.test_name for t in manifest.tests]
    assert len(names) == len(set(names))
    assert "test_engine[get_guide-change.inventory]" in names


# --- verify's ledger and the page --------------------------------------------------


def _verify() -> dict:
    result = runner.invoke(
        app, ["verify", "--environment", "staging", "--output", "json"]
    )
    assert result.exit_code in (0, 1), result.output
    return json.loads(result.output)


def test_every_ledger_check_carries_its_code_and_standards(workdir: Path) -> None:
    payload = _verify()
    (server,) = payload["tools"]["servers"]
    assert [f["id"] for f in server["families"]] == [
        "authority",
        "blast",
        "containment",
        "change",
    ]
    for tool in server["tools"]:
        for check in tool["checks"]:
            code, _family, standards = TABLE[check["trait"]]
            assert check["code"] == code
            assert check["standards"] == standards


def test_a_ledger_written_with_old_ids_is_read_with_new_ones() -> None:
    document = json.loads(TOOL_LEDGER.read_text(encoding="utf-8"))["tools"]
    old = {new: legacy for legacy, new in CANONICAL.items()}
    families = {"authority": "A", "blast": "B", "containment": "C", "change": "D"}
    for server in document["servers"]:
        for family in server["families"]:
            family["id"] = families[family["id"]]
        for tool in server["tools"]:
            for check in tool["checks"]:
                check["trait"] = old[check["trait"]]
                del check["code"], check["standards"]
        for exception in server["exceptions"]:
            exception["trait"] = old[exception["trait"]]

    ledger = report_model.ToolLedger.model_validate(document)
    server = ledger.servers[0]
    assert [f.id for f in server.families] == list(families)
    traits = {c.trait for t in server.tools for c in t.checks}
    assert traits <= set(CANONICAL.values())
    assert {e.trait for e in server.exceptions} <= set(CANONICAL.values())


def test_the_tool_table_headers_carry_slug_code_and_standards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_declarations_plan_sync import ALEX

    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["sync", "--auto-approve", "--tf-json", str(ALEX)])
    manifest = load_manifest(tmp_path / ".itest" / "manifest.yaml")
    verify = json.loads(runner.invoke(app, ["verify", "--output", "json"]).output)
    verify["tools"] = json.loads(TOOL_LEDGER.read_text(encoding="utf-8"))["tools"]
    blocks = report_render.build_blocks(report_model.build(verify, manifest))

    headers = [h for group in blocks["TOOLS"] for h in group["headers"]]
    by_slug = {h["slug"]: h for h in headers}
    anonymous = by_slug["authority.anonymous"]
    assert anonymous["label"] == "authority.anonymous · AUTH-1"
    assert anonymous["code"] == "AUTH-1"
    assert anonymous["standards"] == ["ASI03", "LLM02", "semgrep-server-4", "CWE-306"]
    # The slug is the identity; the first id sits beside it as the
    # cross-reference, and the full list is in the detail.
    assert anonymous["std"] == "ASI03"
    assert anonymous["title"] == (
        "authority.anonymous · AUTH-1 — ASI03, LLM02, semgrep-server-4, CWE-306"
    )
    # Family order, then code: authority before blast before change.
    slugs = [h["slug"] for h in blocks["TOOLS"][0]["headers"]]
    assert slugs.index("authority.anonymous") < slugs.index("blast.mutation_class")
    assert slugs.index("blast.mutation_class") < slugs.index("change.schema_drift")
    html = report_render.render(report_model.build(verify, manifest))
    assert "ASI03, LLM02, semgrep-server-4, CWE-306" in html  # the hover detail
    # Every cell carries its trait's full list in its own detail.
    cells = [c for g in blocks["TOOLS"] for r in g["rows"] for c in r["cells"] if c]
    assert cells
    for cell in cells:
        assert cell["standards"], cell
        assert " — " in cell["title"] and cell["standards"][0] in cell["title"]
    first = blocks["TOOLS"][0]["rows"][0]["cells"][0]
    assert first["standards"] == ["ASI03", "LLM02", "semgrep-server-4", "CWE-306"]
    titles = [
        e["title"]
        for e in blocks["PAGE"]["tools"]["exceptions"]
        if "update_record" in e["title"]
    ]
    assert titles and "change.description_drift · CHANGE-3" in titles[0]


# --- the old id still reaches the check ----------------------------------------------


def test_a_binding_generated_with_an_old_id_still_dispatches() -> None:
    import itest.checks as checks
    from itest.probes.mcp import McpTarget

    target = McpTarget(kind="http")  # never reached: the ids decide first
    assert checks.run_engine_check(
        "B3", {"attributes": {}}, target, authenticated=False
    ).detail == ("no engine check for blast.egress")
    assert checks.run_generated_check(
        "B2", {"attributes": {}}, target, fixtures={}
    ).detail == ("no generated check for blast.destructive_gating yet")
    assert set(checks.ENGINE_CHECKS) == {
        "authority.anonymous",
        "blast.mutation_class",
        "blast.mutation_class_observed",
        "change.inventory",
        "change.schema_drift",
        "change.description_drift",
    }


def test_a_declaration_may_name_traits_by_slug_or_by_old_id() -> None:
    from itest.core.declarations.schema import ToolOverride

    assert ToolOverride(traits=["authority.anonymous"]).traits == [
        "authority.anonymous"
    ]
    assert ToolOverride(traits=["A1", "B2"]).traits == [
        "authority.anonymous",
        "blast.destructive_gating",
    ]
    with pytest.raises(ValueError):
        ToolOverride(traits=["Authority Anonymous"])


def test_the_manifest_spells_the_engine_test_the_way_stubgen_does() -> None:
    """The migration renames engine cases by name without importing stubgen
    (which imports the manifest module); this keeps the two spellings one."""
    from itest.core import manifest, stubgen

    assert manifest._ENGINE_TEST == stubgen.ENGINE_TEST
