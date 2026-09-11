"""The trait table is live: every sync recomputes every tool's trait set.

A tool's checks used to be decided once, when the tool was new. Now each sync
evaluates the current table against each tool's *current* attributes and
compares the answer with ``traits_planned`` — what the last sync decided — so a
change to the table, or to a tool, is a diff in the plan and produces or
retires checks:

- a newly applicable **generated** trait appends a stub; a newly applicable
  **engine** trait writes nothing (the engine runs it from the manifest);
- a trait that no longer applies **retires** its stub: kept on disk, kept in the
  manifest, never run, and said so — nothing generated is ever deleted;
- a trait that applies again **un-retires** the same entry, under the same id;
- a hand-picked ``traits:`` list in the declaration still beats the table.

Everything runs against the real reference server over stdio, like
``test_declarations_plan_sync.py``, whose helpers these tests share.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest
import yaml
from test_declarations_plan_sync import (
    ACTIVE_FILE,
    ALEX,
    ENGINE_FILE,
    READONLY_FILE,
    REPO_ROOT,
    SERVER_PATH,
    _declare,
    _edit_table,
    _functions,
    _plan,
    _sync,
    make_workdir,
    runner,
)

from itest.cli import app
from itest.core.declarations.tools import tool_point_id
from itest.core.declarations.traits import trait_table_hash
from itest.core.manifest import load_manifest, save_manifest

P30 = REPO_ROOT / "tests" / "fixtures" / "p30-manifests"


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return make_workdir(tmp_path, monkeypatch)


def _manifest(workdir: Path):
    return load_manifest(workdir / ".itest" / "manifest.yaml")


def _planned(workdir: Path) -> dict[str, list[str]]:
    return {p.target: p.traits_planned for p in _manifest(workdir).points}


def _plan_json(*extra: str) -> dict:
    result = _plan("--output", "json", *extra)
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


# --- the trait set the reference declaration produces ---------------------------

#: Every tool on the reference server, and every trait the shipped table gives
#: it. A table edit that moves any of these is a visible diff here as well as in
#: traits.yaml. The server runs as a service identity (A3 everywhere, A4
#: nowhere), declares a second tenant (A2 on everything non-informational) and
#: an audit sink (B4 on writes), and only `enrich` declares egress (B3).
EXPECTED_TRAITS = {
    "get_guide": ["A1", "A3", "B1", "D1", "D2", "D3"],
    "search_records": [
        "A1", "A2", "A3", "B1", "C1", "C2", "C3", "D1", "D2", "D3",
    ],
    "fetch_record": ["A1", "A2", "A3", "B1", "C1", "C2", "C3", "D1", "D2", "D3"],
    "create_record": [
        "A1", "A2", "A3", "B1", "B4", "C1", "C2", "C3", "D1", "D2", "D3",
    ],
    "update_record": [
        "A1", "A2", "A3", "B1", "B4", "C1", "C2", "C3", "D1", "D2", "D3",
    ],
    "delete_record": [
        "A1", "A2", "A3", "B1", "B2", "B4", "C1", "C2", "C3", "D1", "D2", "D3",
    ],
    "lookalike_read": [
        "A1", "A2", "A3", "B1", "C1", "C2", "C3", "D1", "D2", "D3",
    ],
    "enrich": [
        "A1", "A2", "A3", "B1", "B3", "C1", "C2", "C3", "D1", "D2", "D3",
    ],
}  # fmt: skip


def test_the_reference_declaration_produces_the_pinned_trait_set(
    workdir: Path,
) -> None:
    assert _sync().exit_code == 0
    assert _planned(workdir) == EXPECTED_TRAITS
    assert _manifest(workdir).trait_table_hash == trait_table_hash()


def test_only_generated_traits_get_a_per_tool_stub(workdir: Path) -> None:
    """Engine traits are run by the engine from the manifest: no per-tool code
    exists for them, so a sync writes a stub only for A2/A3/A4/B2/B4."""
    assert _sync().exit_code == 0
    generated = {"A2", "A3", "A4", "B2", "B4"}
    expected = {
        f"test_{tool}__{trait}"
        for tool, traits in EXPECTED_TRAITS.items()
        for trait in traits
        if trait in generated
    }
    assert _functions(workdir / ACTIVE_FILE) == expected
    entries = _manifest(workdir).tests
    assert all(t.trait is not None for t in entries)
    bindings = {t.trait for t in entries if t.path == ACTIVE_FILE}
    engine = {t.trait for t in entries if t.path != ACTIVE_FILE}
    assert bindings <= generated
    assert not engine & generated  # the engine module runs only engine traits


# --- unchanged is a no-op -----------------------------------------------------


def test_an_unchanged_table_and_unchanged_tools_change_no_traits(
    workdir: Path,
) -> None:
    assert _sync().exit_code == 0
    payload = _plan_json()
    assert payload["trait_changes"] == []
    assert payload["trait_table_hash"] == payload["previous_trait_table_hash"]
    assert "Trait changes" not in _plan().output
    result = _sync()
    assert result.exit_code == 0, result.output
    assert "No changes to apply" in result.output


# --- a table edit gains a check on an existing tool ------------------------------


def test_editing_the_table_gains_a_check_on_an_existing_tool(
    workdir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The heart of it. get_guide is informational, so the shipped A2 rule
    withholds tenant isolation from it; widen the rule and the tool that already
    exists must gain the check on the next sync."""
    assert _sync().exit_code == 0
    before_hash = _manifest(workdir).trait_table_hash
    stubs_before = (workdir / ACTIVE_FILE).read_text(encoding="utf-8")
    assert "A2" not in _planned(workdir)["get_guide"]

    _edit_table(tmp_path, monkeypatch, A2="always")
    after_hash = trait_table_hash()
    assert after_hash != before_hash

    payload = _plan_json()
    assert payload["trait_changes"] == [
        {
            "point_id": tool_point_id("reference-mcp", "get_guide"),
            "server": "reference-mcp",
            "tool": "get_guide",
            "trait": "A2",
            "kind": "generated",
            "change": "gained",
            "reason": "rule: always",
        }
    ]
    human = _plan().output
    assert "Trait changes (1):" in human
    assert "+A2 on reference-mcp/get_guide (rule: always)" in human
    assert (
        f"trait table changed ({before_hash} → {after_hash}): "
        "1 tools gained checks, 0 tools retired checks." in human
    )
    assert "1 trait change(s)" in human.splitlines()[0]

    result = _sync()
    assert result.exit_code == 0, result.output
    assert "added 1 stub(s)" in result.output
    text = (workdir / ACTIVE_FILE).read_text(encoding="utf-8")
    # Appended, never rewritten.
    assert text.startswith(stubs_before)
    assert "test_get_guide__A2" in _functions(workdir / ACTIVE_FILE)
    manifest = _manifest(workdir)
    assert "A2" in _planned(workdir)["get_guide"]
    assert manifest.trait_table_hash == after_hash
    # The stub's docstring records the schema it was generated against.
    guide = next(p for p in manifest.points if p.target == "get_guide")
    block = text.split("def test_get_guide__A2(")[1]
    assert f"schema: {guide.attributes['schema_hash']}" in block
    assert _sync().output.count("No changes to apply") == 1


def test_a_newly_applicable_engine_trait_writes_nothing(
    workdir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B3 is an engine trait: widen it to every tool and seven tools gain it in
    traits_planned, and not one line of code is written — the engine module
    picks the new cases up from the manifest."""
    assert _sync().exit_code == 0
    stubs_before = (workdir / ACTIVE_FILE).read_text(encoding="utf-8")
    engine_before = (workdir / ENGINE_FILE).read_text(encoding="utf-8")

    _edit_table(tmp_path, monkeypatch, B3="always")
    payload = _plan_json()
    gained = {(c["tool"], c["trait"], c["kind"]) for c in payload["trait_changes"]}
    assert gained == {
        (tool, "B3", "engine") for tool in EXPECTED_TRAITS if tool != "enrich"
    }

    result = _sync()
    assert result.exit_code == 0, result.output
    assert "added 0 stub(s)" in result.output
    assert "registered 7 engine check(s)" in result.output
    assert (workdir / ACTIVE_FILE).read_text(encoding="utf-8") == stubs_before
    assert (workdir / ENGINE_FILE).read_text(encoding="utf-8") == engine_before
    assert not (workdir / READONLY_FILE).exists()
    assert all("B3" in traits for traits in _planned(workdir).values())


# --- a trait that stops applying is retired, never deleted ----------------------


def test_a_trait_that_stops_applying_retires_its_stub_in_place(
    workdir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _sync().exit_code == 0
    stubs_before = (workdir / ACTIVE_FILE).read_text(encoding="utf-8")
    entry_before = next(
        t for t in _manifest(workdir).tests if t.test_name == "test_delete_record__B2"
    )

    _edit_table(tmp_path, monkeypatch, B2="mutation == nothing")
    payload = _plan_json()
    assert [(c["tool"], c["trait"], c["change"]) for c in payload["trait_changes"]] == [
        ("delete_record", "B2", "retired")
    ]
    human = _plan().output
    assert "−B2 on reference-mcp/delete_record (rule: mutation == nothing)" in human
    assert "0 tools gained checks, 1 tools retired checks." in human

    result = _sync()
    assert result.exit_code == 0, result.output
    assert "retired 1 check(s)" in result.output
    # Not deleted: the file is byte-identical and the entry is still registered.
    assert (workdir / ACTIVE_FILE).read_text(encoding="utf-8") == stubs_before
    entry = next(
        t for t in _manifest(workdir).tests if t.test_name == "test_delete_record__B2"
    )
    assert entry.retired is True
    assert entry.id == entry_before.id
    assert "B2" not in _planned(workdir)["delete_record"]

    # Not run: verify leaves it out of collection and says why.
    result = runner.invoke(
        app, ["verify", "--environment", "staging", "--output", "json"]
    )
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    outcomes = {t["canonical"]: t["outcome"] for t in report["tests"]}
    assert outcomes[entry.canonical] == "not_applicable"
    ran = [o for o in outcomes.values() if o not in ("gated", "not_applicable")]
    assert len(ran) == len(outcomes) - 1

    # And when the rule applies again, the same entry comes back: same id, same
    # function, no second stub appended.
    monkeypatch.undo()
    monkeypatch.chdir(workdir)
    payload = _plan_json()
    assert [(c["tool"], c["trait"], c["change"]) for c in payload["trait_changes"]] == [
        ("delete_record", "B2", "gained")
    ]
    result = _sync()
    assert result.exit_code == 0, result.output
    assert "added 0 stub(s)" in result.output
    assert (workdir / ACTIVE_FILE).read_text(encoding="utf-8") == stubs_before
    entry = next(
        t for t in _manifest(workdir).tests if t.test_name == "test_delete_record__B2"
    )
    assert entry.retired is False
    assert entry.id == entry_before.id


def test_a_declared_none_of_these_beats_any_table(
    workdir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    assert _planned(workdir)["get_guide"] == []

    _edit_table(tmp_path, monkeypatch, **{tid: "always" for tid in ("A2", "B2", "C1")})
    payload = _plan_json()
    assert not [c for c in payload["trait_changes"] if c["tool"] == "get_guide"]
    assert _sync().exit_code == 0
    assert _planned(workdir)["get_guide"] == []
    assert not {f for f in _functions(workdir / ACTIVE_FILE) if "get_guide" in f}


def test_point_and_entry_ids_hold_across_runs(workdir: Path) -> None:
    assert _sync().exit_code == 0
    first = _manifest(workdir)
    for path in (workdir / ".itest").glob("plan.json"):
        path.unlink()
    assert _sync().exit_code == 0
    second = _manifest(workdir)
    assert [p.id for p in first.points] == [p.id for p in second.points]
    assert [t.id for t in first.tests] == [t.id for t in second.tests]


# --- a tool's mutation class is drift ----------------------------------------------


def _variant(workdir: Path, old: str, new: str) -> None:
    """Point the declaration at a copy of the reference server with one edit."""
    original = SERVER_PATH.read_text(encoding="utf-8")
    assert old in original
    variant = workdir / "variant_server.py"
    variant.write_text(original.replace(old, new), encoding="utf-8")
    path = workdir / ".itest" / "tools" / "reference-mcp.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["transport"]["command"] = [sys.executable, str(variant)]
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def test_a_tool_that_stops_being_destructive_retires_its_gating_checks(
    workdir: Path,
) -> None:
    """delete_record's annotation stops saying destructive. Same schema, same
    description; the class it resolves to moves, and B2 (destructive gating)
    retires with the reason naming the attribute that moved."""
    assert _sync().exit_code == 0
    # The one destructiveHint=True in the reference server is delete_record's.
    _variant(workdir, "destructiveHint=True", "destructiveHint=False")
    payload = _plan_json()
    retired = {
        (c["tool"], c["trait"], c["reason"])
        for c in payload["trait_changes"]
        if c["change"] == "retired"
    }
    assert ("delete_record", "B2", "mutation changed") in retired
    human = _plan().output
    assert "−B2 on reference-mcp/delete_record (mutation changed)" in human
    assert "mutation: destructive → " in human


# --- migration: a P30-era checkout loads, and the first sync fills it ----------------


def _p30_checkout(workdir: Path, name: str) -> None:
    source = P30 / name
    shutil.copytree(source / ".itest", workdir / ".itest", dirs_exist_ok=True)
    shutil.copytree(source / "itest_tests", workdir / "itest_tests")


def test_a_p30_manifest_loads_and_the_first_sync_fills_it(workdir: Path) -> None:
    _p30_checkout(workdir, "reference-mcp")
    _declare(workdir)  # the fixture's own argv points at another machine
    legacy = _manifest(workdir)
    assert legacy.trait_table_hash is None
    assert all(p.traits_planned is None for p in legacy.points)
    assert len(legacy.tests) == 66
    legacy_files = {
        path: (workdir / path).read_text(encoding="utf-8")
        for path in (
            "itest_tests/test_tools_reference_mcp.py",
            "itest_tests/test_tools_reference_mcp_active.py",
        )
    }

    payload = _plan_json()
    changes = {(c["tool"], c["trait"], c["change"]) for c in payload["trait_changes"]}
    # The table grew A3 and C3 since P30; nothing was retired.
    assert changes == {(tool, "A3", "gained") for tool in EXPECTED_TRAITS} | {
        (tool, "C3", "gained") for tool in EXPECTED_TRAITS if tool != "get_guide"
    }
    assert payload["new_points"] == [] and payload["changed_points"] == []

    result = _sync()
    assert result.exit_code == 0, result.output
    assert "added 8 stub(s)" in result.output  # A3 is generated; C3 is engine
    manifest = _manifest(workdir)
    assert manifest.trait_table_hash == trait_table_hash()
    assert _planned(workdir) == EXPECTED_TRAITS
    # Every P30 entry is still registered, live, and learned which trait it
    # covers from the id P30 gave it.
    legacy_ids = {t.id for t in legacy.tests}
    assert legacy_ids <= {t.id for t in manifest.tests}
    for entry in manifest.tests:
        if entry.id in legacy_ids:
            assert entry.trait == entry.test_name.split("_")[1].upper()
            assert entry.retired is False
    # The new A3 bindings went to the server's own directory, beside its
    # conftest; nothing P30 wrote was touched.
    assert len(_functions(workdir / ACTIVE_FILE)) == 8
    for path, text in legacy_files.items():
        assert (workdir / path).read_text(encoding="utf-8") == text


def test_a_declaration_free_p30_manifest_round_trips_byte_for_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The alex regression: no declared tool, so none of the new fields appear —
    loading and saving a P30 manifest writes exactly the bytes it read."""
    monkeypatch.chdir(tmp_path)
    source = P30 / "alex-s6" / ".itest" / "manifest.yaml"
    manifest = load_manifest(source)
    save_manifest(manifest, tmp_path / "manifest.yaml")
    assert (tmp_path / "manifest.yaml").read_bytes() == source.read_bytes()


def test_a_declaration_free_sync_writes_the_p30_manifest_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["sync", "--auto-approve", "--tf-json", str(ALEX)])
    assert result.exit_code == 0, result.output

    def shape(path: Path) -> dict:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        document.pop("generated_at")
        for point in document["points"]:
            point.pop("first_seen")
            point.pop("last_seen")
        return document

    assert shape(tmp_path / ".itest" / "manifest.yaml") == shape(
        P30 / "alex-s6" / ".itest" / "manifest.yaml"
    )
    for name in ("test_event_edges.py", "test_iam_edges.py"):
        assert (tmp_path / "itest_tests" / name).read_bytes() == (
            P30 / "alex-s6" / "itest_tests" / name
        ).read_bytes()
