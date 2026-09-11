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
from itest.core.declarations.traits import load_traits, trait_table_hash
from itest.core.manifest import load_manifest, save_manifest
from itest.traits.ids import LEGACY_TRAIT_IDS, trait_ident

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
    "get_guide": [
        "authority.anonymous",
        "authority.backing_least_privilege",
        "blast.mutation_class",
        "change.inventory",
        "change.schema_drift",
        "change.description_drift",
    ],
    "search_records": [
        "authority.anonymous",
        "authority.tenant_isolation",
        "authority.backing_least_privilege",
        "blast.mutation_class",
        "containment.parameter_scope",
        "containment.expression_passthrough",
        "containment.output_hygiene",
        "change.inventory",
        "change.schema_drift",
        "change.description_drift",
    ],
    "fetch_record": [
        "authority.anonymous",
        "authority.tenant_isolation",
        "authority.backing_least_privilege",
        "blast.mutation_class",
        "containment.parameter_scope",
        "containment.expression_passthrough",
        "containment.output_hygiene",
        "change.inventory",
        "change.schema_drift",
        "change.description_drift",
    ],
    "create_record": [
        "authority.anonymous",
        "authority.tenant_isolation",
        "authority.backing_least_privilege",
        "blast.mutation_class",
        "blast.audit",
        "containment.parameter_scope",
        "containment.expression_passthrough",
        "containment.output_hygiene",
        "change.inventory",
        "change.schema_drift",
        "change.description_drift",
    ],
    "update_record": [
        "authority.anonymous",
        "authority.tenant_isolation",
        "authority.backing_least_privilege",
        "blast.mutation_class",
        "blast.audit",
        "containment.parameter_scope",
        "containment.expression_passthrough",
        "containment.output_hygiene",
        "change.inventory",
        "change.schema_drift",
        "change.description_drift",
    ],
    "delete_record": [
        "authority.anonymous",
        "authority.tenant_isolation",
        "authority.backing_least_privilege",
        "blast.mutation_class",
        "blast.destructive_gating",
        "blast.audit",
        "containment.parameter_scope",
        "containment.expression_passthrough",
        "containment.output_hygiene",
        "change.inventory",
        "change.schema_drift",
        "change.description_drift",
    ],
    "lookalike_read": [
        "authority.anonymous",
        "authority.tenant_isolation",
        "authority.backing_least_privilege",
        "blast.mutation_class",
        "containment.parameter_scope",
        "containment.expression_passthrough",
        "containment.output_hygiene",
        "change.inventory",
        "change.schema_drift",
        "change.description_drift",
    ],
    "enrich": [
        "authority.anonymous",
        "authority.tenant_isolation",
        "authority.backing_least_privilege",
        "blast.mutation_class",
        "blast.egress",
        "containment.parameter_scope",
        "containment.expression_passthrough",
        "containment.output_hygiene",
        "change.inventory",
        "change.schema_drift",
        "change.description_drift",
    ],
}


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
    generated = {
        "authority.tenant_isolation",
        "authority.backing_least_privilege",
        "authority.delegation",
        "blast.destructive_gating",
        "blast.audit",
    }
    expected = {
        f"test_{tool}__{trait_ident(trait)}"
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
    assert "authority.tenant_isolation" not in _planned(workdir)["get_guide"]

    _edit_table(tmp_path, monkeypatch, A2="always")
    after_hash = trait_table_hash()
    assert after_hash != before_hash

    payload = _plan_json()
    assert payload["trait_changes"] == [
        {
            "point_id": tool_point_id("reference-mcp", "get_guide"),
            "server": "reference-mcp",
            "tool": "get_guide",
            "trait": "authority.tenant_isolation",
            "kind": "generated",
            "change": "gained",
            "reason": "rule: always",
        }
    ]
    human = _plan().output
    assert "Trait changes (1):" in human
    assert (
        "+authority.tenant_isolation on reference-mcp/get_guide (rule: always)" in human
    )
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
    assert "test_get_guide__authority__tenant_isolation" in _functions(
        workdir / ACTIVE_FILE
    )
    manifest = _manifest(workdir)
    assert "authority.tenant_isolation" in _planned(workdir)["get_guide"]
    assert manifest.trait_table_hash == after_hash
    # The stub's docstring records the schema it was generated against.
    guide = next(p for p in manifest.points if p.target == "get_guide")
    block = text.split("def test_get_guide__authority__tenant_isolation(")[1]
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
        (tool, "blast.egress", "engine") for tool in EXPECTED_TRAITS if tool != "enrich"
    }

    result = _sync()
    assert result.exit_code == 0, result.output
    assert "added 0 stub(s)" in result.output
    assert "registered 7 engine check(s)" in result.output
    assert (workdir / ACTIVE_FILE).read_text(encoding="utf-8") == stubs_before
    assert (workdir / ENGINE_FILE).read_text(encoding="utf-8") == engine_before
    assert not (workdir / READONLY_FILE).exists()
    assert all("blast.egress" in traits for traits in _planned(workdir).values())


# --- a trait that stops applying is retired, never deleted ----------------------


def test_a_trait_that_stops_applying_retires_its_stub_in_place(
    workdir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _sync().exit_code == 0
    stubs_before = (workdir / ACTIVE_FILE).read_text(encoding="utf-8")
    entry_before = next(
        t
        for t in _manifest(workdir).tests
        if t.test_name == "test_delete_record__blast__destructive_gating"
    )

    # The edited table lives in its own MonkeyPatch context: leaving it restores
    # the shipped table and nothing else (the fixture's chdir is untouched).
    with pytest.MonkeyPatch.context() as table:
        _edit_table(tmp_path, table, B2="mutation == nothing")
        payload = _plan_json()
        assert [
            (c["tool"], c["trait"], c["change"]) for c in payload["trait_changes"]
        ] == [("delete_record", "blast.destructive_gating", "retired")]
        human = _plan().output
        assert (
            "−blast.destructive_gating on reference-mcp/delete_record "
            "(rule: mutation == nothing)" in human
        )
        assert "0 tools gained checks, 1 tools retired checks." in human

        result = _sync()
        assert result.exit_code == 0, result.output
        assert "retired 1 check(s)" in result.output
        # Not deleted: the file is byte-identical and the entry is still registered.
        assert (workdir / ACTIVE_FILE).read_text(encoding="utf-8") == stubs_before
        entry = next(
            t
            for t in _manifest(workdir).tests
            if t.test_name == "test_delete_record__blast__destructive_gating"
        )
        assert entry.retired is True
        assert entry.id == entry_before.id
        assert "blast.destructive_gating" not in _planned(workdir)["delete_record"]

        # Not run: verify leaves it out of collection and says why.
        result = runner.invoke(
            app, ["verify", "--environment", "staging", "--output", "json"]
        )
        # 1, not 2: A1's real findings on reference-mcp's stdio read tools fail;
        # nothing errors.
        assert result.exit_code == 1, result.output
        report = json.loads(result.output)
        assert report["errored"] == 0
        outcomes = {t["canonical"]: t["outcome"] for t in report["tests"]}
        assert outcomes[entry.canonical] == "not_applicable"
        ran = [o for o in outcomes.values() if o not in ("gated", "not_applicable")]
        assert len(ran) == len(outcomes) - 1

    # And when the rule applies again, the same entry comes back: same id, same
    # function, no second stub appended.
    payload = _plan_json()
    assert [(c["tool"], c["trait"], c["change"]) for c in payload["trait_changes"]] == [
        ("delete_record", "blast.destructive_gating", "gained")
    ]
    result = _sync()
    assert result.exit_code == 0, result.output
    assert "added 0 stub(s)" in result.output
    assert (workdir / ACTIVE_FILE).read_text(encoding="utf-8") == stubs_before
    entry = next(
        t
        for t in _manifest(workdir).tests
        if t.test_name == "test_delete_record__blast__destructive_gating"
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

    _edit_table(
        tmp_path,
        monkeypatch,
        **{
            tid: "always"
            for tid in (
                "authority.tenant_isolation",
                "blast.destructive_gating",
                "containment.parameter_scope",
            )
        },
    )
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
    assert original.count(old) == 1, f"{old!r} must name exactly one place"
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
    assert ("delete_record", "blast.destructive_gating", "mutation changed") in retired
    human = _plan().output
    assert (
        "−blast.destructive_gating on reference-mcp/delete_record (mutation changed)"
        in human
    )
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
    assert changes == {
        (tool, "authority.backing_least_privilege", "gained")
        for tool in EXPECTED_TRAITS
    } | {
        (tool, "containment.output_hygiene", "gained")
        for tool in EXPECTED_TRAITS
        if tool != "get_guide"
    }
    assert payload["new_points"] == [] and payload["changed_points"] == []

    result = _sync()
    assert result.exit_code == 0, result.output
    assert "added 8 stub(s)" in result.output  # A3 is generated; C3 is engine
    manifest = _manifest(workdir)
    assert manifest.trait_table_hash == trait_table_hash()
    assert _planned(workdir) == EXPECTED_TRAITS
    # Every P30 entry is still registered and learned which trait it covers
    # from the id P30 gave it. A generated trait's stub stays live; an engine
    # trait's is retired — the engine module is what runs that trait now.
    kinds = {t.id: t.kind for t in load_traits().traits}
    legacy_ids = {t.id for t in legacy.tests}
    assert legacy_ids <= {t.id for t in manifest.tests}
    for entry in manifest.tests:
        if entry.id in legacy_ids:
            legacy = entry.test_name.split("_")[1].upper()
            assert entry.trait == LEGACY_TRAIT_IDS[legacy]
            assert entry.retired is (kinds[entry.trait] == "engine")
    # The new A3 bindings went to the server's own directory, beside its
    # conftest; nothing P30 wrote was touched.
    assert len(_functions(workdir / ACTIVE_FILE)) == 8
    for path, text in legacy_files.items():
        assert (workdir / path).read_text(encoding="utf-8") == text


#: The P30 checkout's per-tool stubs for traits that are engine traits now:
#: A1, B1, B3, C1, C2 and D1-D3 on the tools each applied to (66 stubs, less
#: the 11 for the generated A2, B2 and B4).
P30_ENGINE_STUBS = 55
P30_STUB_FILE = "itest_tests/test_tools_reference_mcp.py"


def test_a_p30_per_tool_engine_stub_is_retired_and_never_run(workdir: Path) -> None:
    """Before the engine module existed, sync wrote a per-tool stub for every
    trait. For an engine trait that stub is superseded: the first sync retires
    it in place — kept on disk, kept in the manifest, never run — and the
    engine module is the only thing that runs the trait."""
    _p30_checkout(workdir, "reference-mcp")
    _declare(workdir)
    stub_file = workdir / P30_STUB_FILE
    before = stub_file.read_text(encoding="utf-8")

    result = _sync()
    assert result.exit_code == 0, result.output
    assert f"retired {P30_ENGINE_STUBS} check(s)" in result.output

    by_name = {t.test_name: t for t in _manifest(workdir).tests}
    stub = by_name["test_a1_get_guide"]
    assert stub.retired is True
    assert stub.status != "orphaned"
    assert stub_file.read_text(encoding="utf-8") == before  # nothing deleted
    engine = by_name["test_engine[get_guide-authority.anonymous]"]
    assert engine.path == ENGINE_FILE
    assert engine.retired is False

    verify = runner.invoke(
        app, ["verify", "--environment", "staging", "--output", "json"]
    )
    assert verify.exit_code in (0, 1), verify.output
    payload = json.loads(verify.output)
    outcomes = {t["canonical"]: t["outcome"] for t in payload["tests"]}
    assert outcomes[f"{P30_STUB_FILE}::test_a1_get_guide"] == "not_applicable"
    retired = [t for t in _manifest(workdir).tests if t.retired]
    assert len(retired) == P30_ENGINE_STUBS
    assert {outcomes[t.canonical] for t in retired} == {"not_applicable"}
    checks = {
        (tool["name"], check["trait"]): check
        for server in payload["tools"]["servers"]
        for tool in server["tools"]
        for check in tool["checks"]
    }
    assert checks[("get_guide", "authority.anonymous")]["test"] == (
        f"{ENGINE_FILE}::test_engine[get_guide-authority.anonymous]"
    )

    again = _sync()
    assert again.exit_code == 0, again.output
    assert "restored" not in again.output
    assert sum(1 for t in _manifest(workdir).tests if t.retired) == P30_ENGINE_STUBS


def test_a_no_op_sync_retires_an_engine_stub_an_earlier_sync_left_live(
    workdir: Path,
) -> None:
    """A checkout synced before this rule existed has traits_planned recorded
    and the P30 engine stubs still live, so its plan is a no-op — and the stubs
    are retired anyway."""
    _p30_checkout(workdir, "reference-mcp")
    _declare(workdir)
    assert _sync().exit_code == 0
    manifest_file = workdir / ".itest" / "manifest.yaml"
    manifest = load_manifest(manifest_file)
    for test in manifest.tests:
        test.retired = False  # what the earlier sync left behind
    save_manifest(manifest, manifest_file)

    result = _sync()
    assert result.exit_code == 0, result.output
    assert "No changes to apply" not in result.output
    assert (
        f"Retired {P30_ENGINE_STUBS} per-tool stub(s) the engine module supersedes."
        in result.output
    )
    assert sum(1 for t in _manifest(workdir).tests if t.retired) == P30_ENGINE_STUBS


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


def test_every_p30_fixture_file_is_tracked_by_git() -> None:
    """The fixtures live under `.itest/` and `itest_tests/`, which .gitignore
    excludes everywhere. A file present on disk but never committed passes here
    and fails on every CI runner, so each one must be tracked explicitly."""
    import subprocess

    if not (REPO_ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    on_disk = {
        path.relative_to(REPO_ROOT).as_posix()
        for path in P30.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }
    tracked = set(
        subprocess.run(
            ["git", "ls-files", "--", str(P30.relative_to(REPO_ROOT))],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
    )
    assert on_disk - tracked == set(), "force-add these with `git add -f`"
    for name in ("reference-mcp", "alex-s6"):
        assert f"tests/fixtures/p30-manifests/{name}/.itest/manifest.yaml" in tracked
