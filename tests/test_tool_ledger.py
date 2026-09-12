"""verify emits the tool ledger, and every check in it carries a lifecycle state.

The ledger is ``tools.servers[]`` in ``verify --output json`` — exactly the shape
of ``tests/fixtures/report/tool-ledger.json``, which the readiness page renders.
Every row is built from what verify knows: the manifest's tool points and their
``traits_planned``, the test registered for each (tool, trait), the outcome
pytest reported, and — when the check recorded one — the ``CheckResult``.

Each check also says how much its test can be trusted as a statement about the
tool *today*:

``current``        ITest-owned (ownership hash matches) and generated against
                   the tool's current schema
``hand_edited``    a human changed the file (ownership hash differs)
``stale``          generated against a schema the tool no longer has — nobody
                   has re-read it since the tool moved. Hand-edited, it stays
                   stale until a human does; ITest-owned, the next sync
                   regenerates it
``not_applicable`` retired: the trait no longer applies
``orphan``         the tool is gone; the test is kept, never deleted

VERIFIED is a coverage claim: stale, not-applicable and orphaned checks do not
count toward it, and a server with any stale check cannot be VERIFIED.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_declarations_plan_sync import (
    ACTIVE_FILE,
    CONFTEST_FILE,
    ENGINE_FILE,
    _sync,
    make_workdir,
    runner,
)

from itest.cli import app
from itest.core import stubgen, verifier
from itest.core.declarations.traits import load_traits
from itest.core.manifest import load_manifest, save_manifest
from itest.report import model as report_model
from itest.report import render as report_render

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TOOL_LEDGER = FIXTURES / "report" / "tool-ledger.json"


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    workdir = make_workdir(tmp_path, monkeypatch)
    assert _sync().exit_code == 0
    return workdir


def _verify(*extra: str) -> dict:
    result = runner.invoke(
        app, ["verify", "--environment", "staging", "--output", "json", *extra]
    )
    assert result.exit_code in (0, 1), result.output
    return json.loads(result.output)


def _checks(ledger: dict) -> dict[tuple[str, str], dict]:
    return {
        (tool["name"], check["trait"]): check
        for server in ledger["servers"]
        for tool in server["tools"]
        for check in tool["checks"]
    }


# --- verify emits the ledger -------------------------------------------------------


READ_TOOLS = ("get_guide", "search_records", "fetch_record", "lookalike_read", "enrich")
MUTATING_TOOLS = ("create_record", "update_record", "delete_record")
LISTING_TRAITS = (
    "blast.mutation_class",
    "change.inventory",
    "change.schema_drift",
    "change.description_drift",
)
#: Engine traits in the table that the check library has no check for yet.
UNIMPLEMENTED_ENGINE = (
    "blast.egress",
    "containment.parameter_scope",
    "containment.expression_passthrough",
    "containment.output_hygiene",
)
GENERATED = (
    "authority.tenant_isolation",
    "authority.backing_least_privilege",
    "authority.delegation",
    "blast.destructive_gating",
    "blast.audit",
)


def test_verify_emits_the_tool_ledger_for_a_declared_server(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("REFERENCE_MCP_TOKEN", raising=False)
    payload = _verify()
    ledger = payload["tools"]
    (server,) = ledger["servers"]
    assert server["server"] == "reference-mcp"
    assert server["environment"] == "staging"
    assert server["declaration"] == ".itest/tools/reference-mcp.yaml"
    assert server["run_at"].endswith("Z")
    assert server["summary"]["declared"] == 8
    assert server["summary"]["live"] == 8
    assert [f["id"] for f in server["families"]] == [
        "authority",
        "blast",
        "containment",
        "change",
    ]

    manifest = load_manifest(workdir / ".itest" / "manifest.yaml")
    by_name = {t["name"]: t for t in server["tools"]}
    assert set(by_name) == {p.target for p in manifest.points}
    for point in manifest.points:
        tool = by_name[point.target]
        assert tool["point_id"] == point.id
        assert tool["mutation"] == point.attributes["mutation"]
        assert tool["schema_hash"] == point.attributes["schema_hash"]
        assert [c["trait"] for c in tool["checks"]] == point.traits_planned

    checks = _checks(ledger)
    # An engine trait's row is its engine case; a generated trait's, its binding.
    d1 = checks[("delete_record", "change.inventory")]
    assert d1["test"] == f"{ENGINE_FILE}::test_engine[delete_record-change.inventory]"
    b2 = checks[("delete_record", "blast.destructive_gating")]
    assert b2["test"] == f"{ACTIVE_FILE}::test_delete_record__blast__destructive_gating"
    # Over stdio the process boundary is the authentication boundary, so no
    # tool carries an authority.anonymous cell — not a failing one, not a
    # not_verifiable one. The rule is the table's, and the ledger reflects it.
    assert not any(trait == "authority.anonymous" for _, trait in checks)
    # The engine checks ran for real, against reference-mcp over stdio, with no
    # credential exported. Every status is the library's own CheckResult.
    for (tool, trait), check in checks.items():
        if trait in LISTING_TRAITS:
            # No credential resolves, so the run is anonymous; the stdio server
            # admits an anonymous listing and the listing checks pass on it.
            assert check["status"] == "pass", (tool, trait, check["detail"])
        elif trait in UNIMPLEMENTED_ENGINE:
            assert check["status"] == "not_verifiable", (tool, trait)
            assert check["detail"] == f"no engine check for {trait}"
        elif trait in GENERATED:
            # The fixtures are unfilled: not run — never pass, never a zero.
            assert check["status"] == "not_run", (tool, trait)
    assert "fill in blast__destructive_gating_fixtures" in b2["detail"]
    # The one critical is the catch: lookalike_read observed mutating behind
    # readOnlyHint. Every other observed read tool passes.
    critical = [k for k, c in checks.items() if c["status"] == "critical"]
    assert critical == [("lookalike_read", "blast.mutation_class_observed")]
    for tool in ("get_guide", "search_records", "fetch_record", "enrich"):
        assert checks[(tool, "blast.mutation_class_observed")]["status"] == "pass"
    assert {c["state"] for c in checks.values()} == {"current"}
    assert server["summary"]["verified"] == 0
    assert server["summary"]["critical"] == 1


def test_with_the_credential_exported_the_listing_checks_pass(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The way a user supplies it: the named variable, exported. The run is
    then authenticated: B1 and D1-D3 take the authenticated listing and pass on
    every tool, and the token never reaches the ledger."""
    token = "ledger-token-6d1e-do-not-log"
    monkeypatch.setenv("REFERENCE_MCP_TOKEN", token)
    payload = _verify()
    checks = _checks(payload["tools"])
    for (tool, trait), check in checks.items():
        if trait in LISTING_TRAITS:
            assert check["status"] == "pass", (tool, trait, check["detail"])
    assert checks[("delete_record", "blast.mutation_class")]["detail"] == (
        "destructive: annotation destructiveHint; name delete_*"
    )
    assert (
        checks[("lookalike_read", "blast.mutation_class")]["status"] == "pass"
    )  # the pinned limit
    # The credential does not conjure an anonymous check over stdio either.
    assert not any(trait == "authority.anonymous" for _, trait in checks)
    assert token not in json.dumps(payload)


def test_the_emitted_ledger_validates_through_the_model_and_renders(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("REFERENCE_MCP_TOKEN", raising=False)
    payload = _verify()
    ledger = report_model.ToolLedger.model_validate(payload["tools"])
    assert ledger.declared == 8
    manifest = load_manifest(workdir / ".itest" / "manifest.yaml")
    page = report_model.build(payload, manifest)
    html = report_render.render(page)
    blocks = report_render.extract_blocks(html)
    assert blocks["TOOLS"]  # the grouped tool table is drawn from real data
    # lookalike_read, caught mutating behind readOnlyHint, blocks the release.
    assert page.verdict.word == "BLOCKED"


def test_a_declaration_free_verify_emits_no_tools_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_declarations_plan_sync import ALEX

    monkeypatch.chdir(tmp_path)
    assert (
        runner.invoke(app, ["sync", "--auto-approve", "--tf-json", str(ALEX)]).exit_code
        == 0
    )
    result = runner.invoke(app, ["verify", "--output", "json"])
    assert "tools" not in json.loads(result.output)


def _fake_checks(workdir: Path) -> None:
    """Stand in for 31A's library inside verify's pytest run, through the
    human-owned conftest (the one file a human is meant to edit)."""
    conftest = workdir / CONFTEST_FILE
    conftest.write_text(
        conftest.read_text(encoding="utf-8")
        + "\n\nimport itest.checks as _checks\n\n\n"
        "def _engine(trait_id, point, target, *, authenticated):\n"
        '    if (trait_id, point["target"]) == '
        '("blast.mutation_class", "delete_record"):\n'
        '        return _checks.CheckResult("critical", "anonymous call accepted", '
        "None)\n"
        '    if (trait_id, point["target"]) == '
        '("change.description_drift", "update_record"):\n'
        '        return _checks.CheckResult("changed", "description moved", None)\n'
        '    return _checks.CheckResult("pass", f"{trait_id} held", None)\n\n\n'
        "_checks.run_engine_check = _engine\n",
        encoding="utf-8",
    )


def test_a_recorded_check_result_is_the_status(workdir: Path) -> None:
    _fake_checks(workdir)
    payload = _verify()
    checks = _checks(payload["tools"])
    assert checks[("delete_record", "blast.mutation_class")]["status"] == "critical"
    assert (
        checks[("delete_record", "blast.mutation_class")]["detail"]
        == "anonymous call accepted"
    )
    assert checks[("update_record", "change.description_drift")]["status"] == "changed"
    assert checks[("get_guide", "change.inventory")] == {
        "trait": "change.inventory",
        "code": "CHANGE-1",
        "standards": ["ASI04", "LLM04", "semgrep-client-1", "ACS-AgBOM"],
        "status": "pass",
        "detail": "change.inventory held",
        "test": f"{ENGINE_FILE}::test_engine[get_guide-change.inventory]",
        "state": "current",
    }
    (server,) = payload["tools"]["servers"]
    kinds = [e["kind"] for e in server["exceptions"]]
    assert kinds[:2] == ["critical", "changed"]
    manifest = load_manifest(workdir / ".itest" / "manifest.yaml")
    page = report_model.build(payload, manifest)
    assert page.verdict.word == "BLOCKED"


# --- lifecycle state -------------------------------------------------------------


def test_a_hand_edited_file_marks_its_checks_hand_edited(workdir: Path) -> None:
    path = workdir / ACTIVE_FILE
    path.write_text(path.read_text(encoding="utf-8") + "# reviewed\n", "utf-8")
    checks = _checks(_verify()["tools"])
    assert (
        checks[("delete_record", "blast.destructive_gating")]["state"] == "hand_edited"
    )
    assert (
        checks[("delete_record", "change.inventory")]["state"] == "current"
    )  # engine module


def test_hand_edited_against_an_old_schema_is_stale(workdir: Path) -> None:
    path = workdir / ACTIVE_FILE
    path.write_text(path.read_text(encoding="utf-8") + "# reviewed\n", "utf-8")
    # The tool's schema moved since the binding was generated and reviewed.
    manifest_file = workdir / ".itest" / "manifest.yaml"
    manifest = load_manifest(manifest_file)
    point = next(p for p in manifest.points if p.target == "delete_record")
    point.attributes["schema_hash"] = "0123456789ab"
    save_manifest(manifest, manifest_file)

    payload = _verify()
    checks = _checks(payload["tools"])
    assert checks[("delete_record", "blast.destructive_gating")]["state"] == "stale"
    assert checks[("create_record", "blast.audit")]["state"] == "hand_edited"
    (server,) = payload["tools"]["servers"]
    stale = [e for e in server["exceptions"] if e["kind"] == "stale"]
    assert server["exceptions"][: len(stale)] == stale  # named first
    assert {(e["tool"], e["trait"]) for e in stale} == {
        ("delete_record", "authority.tenant_isolation"),
        ("delete_record", "authority.backing_least_privilege"),
        ("delete_record", "blast.destructive_gating"),
        ("delete_record", "blast.audit"),
    }


def test_a_retired_check_is_not_applicable(workdir: Path) -> None:
    manifest_file = workdir / ".itest" / "manifest.yaml"
    manifest = load_manifest(manifest_file)
    entry = next(
        t
        for t in manifest.tests
        if t.test_name == "test_delete_record__blast__destructive_gating"
    )
    entry.retired = True
    point = next(p for p in manifest.points if p.id == entry.point_id)
    point.traits_planned = [
        t for t in point.traits_planned if t != "blast.destructive_gating"
    ]
    save_manifest(manifest, manifest_file)

    check = _checks(_verify()["tools"])[("delete_record", "blast.destructive_gating")]
    assert check["state"] == "not_applicable"
    assert check["status"] == "n/a"


def test_an_orphaned_check_is_named_and_counted(workdir: Path) -> None:
    manifest_file = workdir / ".itest" / "manifest.yaml"
    manifest = load_manifest(manifest_file)
    gone = next(p for p in manifest.points if p.target == "enrich")
    manifest.points.remove(gone)
    for test in manifest.tests:
        if test.point_id == gone.id:
            test.status = "orphaned"
    save_manifest(manifest, manifest_file)

    (server,) = _verify()["tools"]["servers"]
    assert "enrich" not in {t["name"] for t in server["tools"]}
    assert server["summary"]["orphaned"] == 1
    orphans = [e for e in server["exceptions"] if e["kind"] == "orphan"]
    assert {e["tool"] for e in orphans} == {"enrich"}
    assert "blast.egress" in {e["trait"] for e in orphans}


def test_state_is_computed_from_the_manifest_and_the_files(tmp_path: Path) -> None:
    """The unit: one binding file, one point, every state from its inputs."""
    table = load_traits()
    point_id = "p1"
    body = stubgen.GENERATED_HEADER + (
        "\n\ndef test_tool__blast__destructive_gating("
        "itest_target, itest_point, blast__destructive_gating_fixtures):\n"
        f'    """itest point: {point_id}  trait: blast.destructive_gating  '
        'schema: aaaaaaaaaaaa"""\n'
    )
    path = tmp_path / "t.py"
    path.write_text(body, encoding="utf-8")
    owned = stubgen.content_hash(body)

    def state(
        recorded: str, schema: str, *, retired: bool = False, orphaned: bool = False
    ) -> str:
        return verifier.check_state(
            base_dir=tmp_path,
            path="t.py",
            test_name="test_tool__blast__destructive_gating",
            ownership_hash=recorded,
            point_schema=schema,
            retired=retired,
            orphaned=orphaned,
        )

    assert table.get("blast.destructive_gating").kind == "generated"
    assert state(owned, "aaaaaaaaaaaa") == "current"
    assert state("edited", "aaaaaaaaaaaa") == "hand_edited"
    assert state("edited", "bbbbbbbbbbbb") == "stale"
    # Owning the file does not make a check frozen against an old schema
    # current: until a sync regenerates it, it is stale.
    assert state(owned, "bbbbbbbbbbbb") == "stale"
    assert state(owned, "aaaaaaaaaaaa", retired=True) == "not_applicable"
    assert state(owned, "aaaaaaaaaaaa", orphaned=True) == "orphan"


# --- the contract, the verdict, the page ------------------------------------------


def test_the_fixture_ledger_carries_state_and_validates() -> None:
    document = json.loads(TOOL_LEDGER.read_text(encoding="utf-8"))
    assert set(document["state_vocabulary"]) == set(report_model.CHECK_STATES)
    assert "not_run" in document["status_vocabulary"]
    ledger = report_model.ToolLedger.model_validate(document["tools"])
    states = {c.state for s in ledger.servers for t in s.tools for c in t.checks}
    assert states <= set(report_model.CHECK_STATES)
    assert "current" in states


@pytest.fixture
def green_alex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from test_declarations_plan_sync import ALEX

    monkeypatch.chdir(tmp_path)
    assert (
        runner.invoke(app, ["sync", "--auto-approve", "--tf-json", str(ALEX)]).exit_code
        == 0
    )
    manifest = load_manifest(tmp_path / ".itest" / "manifest.yaml")
    verify = json.loads(runner.invoke(app, ["verify", "--output", "json"]).output)
    for point in verify["points"]:
        point["status"] = "passing"
    return manifest, verify


def test_a_stale_check_flips_the_band_to_at_risk(green_alex) -> None:
    manifest, verify = green_alex
    ledger = json.loads(TOOL_LEDGER.read_text(encoding="utf-8"))["tools"]
    server = ledger["servers"][0]
    server["summary"]["changed"] = 0
    server["summary"]["verified"] = server["summary"]["declared"]
    server["exceptions"] = []
    for tool in server["tools"]:
        for check in tool["checks"]:
            check["status"] = "pass"
            check.pop("change", None)
    verify["tools"] = ledger
    assert report_model.build(verify, manifest).verdict.word == "VERIFIED"

    server["tools"][0]["checks"][0]["state"] = "stale"
    page = report_model.build(verify, manifest)
    assert page.verdict.word == "AT RISK"
    blocks = report_render.build_blocks(page)
    assert blocks["PAGE"]["toolBand"]["word"] == "TOOLS AT RISK"


def test_the_rendered_page_tags_state_and_says_what_needs_attention(
    green_alex,
) -> None:
    manifest, verify = green_alex
    ledger = json.loads(TOOL_LEDGER.read_text(encoding="utf-8"))["tools"]
    checks = ledger["servers"][0]["tools"][0]["checks"]
    checks[0]["state"] = "stale"
    checks[1]["state"] = "hand_edited"
    checks[2]["state"] = "hand_edited"
    ledger["servers"][0]["tools"][1]["checks"][0]["state"] = "hand_edited"
    verify["tools"] = ledger
    page = report_model.build(verify, manifest)
    html = report_render.render(page)
    blocks = report_render.extract_blocks(html)

    rows = [r for g in blocks["TOOLS"] for r in g["rows"]]
    tags = [c.get("state") for r in rows for c in r["cells"] if c]
    assert tags.count("stale") == 1
    assert tags.count("hand-edited") == 3
    # A current check carries no tag: the tag is for what needs a human.
    assert "current" not in tags
    assert blocks["PAGE"]["tools"]["attention"] == [
        "reference-mcp needs attention: 3 hand-edited, 1 stale"
    ]


# --- an owned binding whose schema moved -------------------------------------------


def _point_id(workdir: Path, tool: str) -> str:
    manifest = load_manifest(workdir / ".itest" / "manifest.yaml")
    return next(p.id for p in manifest.points if p.target == tool)


def _frozen_schemas(workdir: Path, point_id: str) -> set[str]:
    """The ``schema:`` every binding for ``point_id`` was frozen with."""
    text = (workdir / ACTIVE_FILE).read_text(encoding="utf-8")
    return {
        line.rsplit("schema: ", 1)[1].strip().rstrip('"')
        for line in text.splitlines()
        if f"itest point: {point_id} " in line
    }


def _record_schema(workdir: Path, tool: str, schema: str) -> None:
    """The manifest now says the tool's schema is ``schema``; no file moves."""
    manifest_file = workdir / ".itest" / "manifest.yaml"
    manifest = load_manifest(manifest_file)
    point = next(p for p in manifest.points if p.target == tool)
    point.attributes["schema_hash"] = schema
    save_manifest(manifest, manifest_file)


def _refreeze(workdir: Path, point_id: str, schema: str) -> None:
    """Rewrite one tool's bindings as though generated against ``schema``, and
    record the result as ITest's (the file stays owned)."""
    path = workdir / ACTIVE_FILE
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    for i, line in enumerate(lines):
        if f"itest point: {point_id} " in line:
            head = line.rsplit("schema: ", 1)[0]
            lines[i] = f'{head}schema: {schema}"""\n'
    path.write_text("".join(lines), encoding="utf-8")
    manifest_file = workdir / ".itest" / "manifest.yaml"
    manifest = load_manifest(manifest_file)
    for test in manifest.tests:
        if test.path == ACTIVE_FILE:
            test.ownership_hash = stubgen.file_hash(path)
    save_manifest(manifest, manifest_file)


OLD_SCHEMA = "0123456789ab"
DELETE_GENERATED = {
    ("delete_record", t)
    for t in (
        "authority.tenant_isolation",
        "authority.backing_least_privilege",
        "blast.destructive_gating",
        "blast.audit",
    )
}


def test_an_owned_binding_whose_schema_moved_is_stale_without_a_sync(
    workdir: Path,
) -> None:
    """Nothing regenerated it, so nobody has read it against the tool as it is:
    ITest owning the file does not make it current."""
    _record_schema(workdir, "delete_record", OLD_SCHEMA)

    payload = _verify()
    checks = _checks(payload["tools"])
    for key in DELETE_GENERATED:
        assert checks[key]["state"] == "stale", key
    assert checks[("create_record", "blast.audit")]["state"] == "current"
    assert (
        checks[("delete_record", "change.inventory")]["state"] == "current"
    )  # engine module
    (server,) = payload["tools"]["servers"]
    stale = [e for e in server["exceptions"] if e["kind"] == "stale"]
    assert {(e["tool"], e["trait"]) for e in stale} == DELETE_GENERATED
    assert all("itest sync" in e["message"] for e in stale)
    assert all("by hand" not in e["message"] for e in stale)


def test_sync_regenerates_an_owned_binding_whose_schema_moved(
    workdir: Path,
) -> None:
    """The schema moves (the manifest and the bindings both say the old one,
    the live server the new); one sync regenerates the owned bindings, and
    verify finds them current."""
    point_id = _point_id(workdir, "delete_record")
    (live,) = _frozen_schemas(workdir, point_id)
    _record_schema(workdir, "delete_record", OLD_SCHEMA)
    _refreeze(workdir, point_id, OLD_SCHEMA)
    assert _frozen_schemas(workdir, point_id) == {OLD_SCHEMA}

    result = _sync()
    assert result.exit_code == 0, result.output
    assert "regenerated 4 check(s)" in result.output
    assert _frozen_schemas(workdir, point_id) == {live}
    # Still ITest's: the recorded hash is the regenerated file's.
    manifest = load_manifest(workdir / ".itest" / "manifest.yaml")
    recorded = {t.ownership_hash for t in manifest.tests if t.path == ACTIVE_FILE}
    assert recorded == {stubgen.file_hash(workdir / ACTIVE_FILE)}

    checks = _checks(_verify()["tools"])
    for key in DELETE_GENERATED:
        assert checks[key]["state"] == "current", key


def test_a_no_op_sync_still_regenerates_an_owned_binding_left_behind(
    workdir: Path,
) -> None:
    """The manifest already records the live schema (an older sync moved it and
    did not regenerate), so the plan is a no-op — and the binding is still
    regenerated, because a file ITest owns is ITest's to bring up to date."""
    point_id = _point_id(workdir, "delete_record")
    (live,) = _frozen_schemas(workdir, point_id)
    _refreeze(workdir, point_id, OLD_SCHEMA)
    assert (
        _checks(_verify()["tools"])[("delete_record", "blast.destructive_gating")][
            "state"
        ]
        == "stale"
    )

    result = _sync()
    assert result.exit_code == 0, result.output
    assert _frozen_schemas(workdir, point_id) == {live}
    checks = _checks(_verify()["tools"])
    for key in DELETE_GENERATED:
        assert checks[key]["state"] == "current", key


def test_sync_never_regenerates_a_hand_edited_binding(workdir: Path) -> None:
    """A human edited it: frozen and reported, never rewritten."""
    path = workdir / ACTIVE_FILE
    path.write_text(path.read_text(encoding="utf-8") + "# reviewed\n", "utf-8")
    point_id = _point_id(workdir, "delete_record")
    _record_schema(workdir, "delete_record", OLD_SCHEMA)
    edited = path.read_text(encoding="utf-8")

    result = _sync()
    assert result.exit_code == 0, result.output
    assert "regenerated" not in result.output
    assert path.read_text(encoding="utf-8") == edited
    assert OLD_SCHEMA not in _frozen_schemas(workdir, point_id)
