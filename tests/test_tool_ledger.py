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
``stale``          hand-edited AND generated against a schema the tool no
                   longer has — nobody has re-read it since the tool moved
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


def test_verify_emits_the_tool_ledger_for_a_declared_server(workdir: Path) -> None:
    payload = _verify()
    ledger = payload["tools"]
    (server,) = ledger["servers"]
    assert server["server"] == "reference-mcp"
    assert server["environment"] == "staging"
    assert server["declaration"] == ".itest/tools/reference-mcp.yaml"
    assert server["run_at"].endswith("Z")
    assert server["summary"]["declared"] == 8
    assert server["summary"]["live"] == 8
    assert [f["id"] for f in server["families"]] == ["A", "B", "C", "D"]

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
    a1 = checks[("delete_record", "A1")]
    assert a1["test"] == f"{ENGINE_FILE}::test_engine[delete_record-A1]"
    b2 = checks[("delete_record", "B2")]
    assert b2["test"] == f"{ACTIVE_FILE}::test_delete_record__B2"
    # Nothing ran for real yet: the engine library is 31A's and the fixtures are
    # unfilled. "not run" is what it says — never pass, never a zero.
    assert {c["status"] for c in checks.values()} == {"not_run"}
    assert "not implemented in itest.checks" in a1["detail"]
    assert "fill in b2_fixtures" in b2["detail"]
    assert {c["state"] for c in checks.values()} == {"current"}
    assert server["summary"]["verified"] == 0


def test_the_emitted_ledger_validates_through_the_model_and_renders(
    workdir: Path,
) -> None:
    payload = _verify()
    ledger = report_model.ToolLedger.model_validate(payload["tools"])
    assert ledger.declared == 8
    manifest = load_manifest(workdir / ".itest" / "manifest.yaml")
    page = report_model.build(payload, manifest)
    html = report_render.render(page)
    blocks = report_render.extract_blocks(html)
    assert blocks["TOOLS"]  # the grouped tool table is drawn from real data
    assert page.verdict.word == "AT RISK"  # nothing verified is not VERIFIED


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
        '    if (trait_id, point["target"]) == ("A1", "delete_record"):\n'
        '        return _checks.CheckResult("critical", "anonymous call accepted", '
        "None)\n"
        '    if (trait_id, point["target"]) == ("D3", "update_record"):\n'
        '        return _checks.CheckResult("changed", "description moved", None)\n'
        '    return _checks.CheckResult("pass", f"{trait_id} held", None)\n\n\n'
        "_checks.run_engine_check = _engine\n",
        encoding="utf-8",
    )


def test_a_recorded_check_result_is_the_status(workdir: Path) -> None:
    _fake_checks(workdir)
    payload = _verify()
    checks = _checks(payload["tools"])
    assert checks[("delete_record", "A1")]["status"] == "critical"
    assert checks[("delete_record", "A1")]["detail"] == "anonymous call accepted"
    assert checks[("update_record", "D3")]["status"] == "changed"
    assert checks[("get_guide", "D1")] == {
        "trait": "D1",
        "status": "pass",
        "detail": "D1 held",
        "test": f"{ENGINE_FILE}::test_engine[get_guide-D1]",
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
    assert checks[("delete_record", "B2")]["state"] == "hand_edited"
    assert checks[("delete_record", "A1")]["state"] == "current"  # engine module


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
    assert checks[("delete_record", "B2")]["state"] == "stale"
    assert checks[("create_record", "B4")]["state"] == "hand_edited"
    (server,) = payload["tools"]["servers"]
    stale = [e for e in server["exceptions"] if e["kind"] == "stale"]
    assert server["exceptions"][: len(stale)] == stale  # named first
    assert {(e["tool"], e["trait"]) for e in stale} == {
        ("delete_record", "A2"),
        ("delete_record", "A3"),
        ("delete_record", "B2"),
        ("delete_record", "B4"),
    }


def test_a_retired_check_is_not_applicable(workdir: Path) -> None:
    manifest_file = workdir / ".itest" / "manifest.yaml"
    manifest = load_manifest(manifest_file)
    entry = next(t for t in manifest.tests if t.test_name == "test_delete_record__B2")
    entry.retired = True
    point = next(p for p in manifest.points if p.id == entry.point_id)
    point.traits_planned = [t for t in point.traits_planned if t != "B2"]
    save_manifest(manifest, manifest_file)

    check = _checks(_verify()["tools"])[("delete_record", "B2")]
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
    assert "B3" in {e["trait"] for e in orphans}


def test_state_is_computed_from_the_manifest_and_the_files(tmp_path: Path) -> None:
    """The unit: one binding file, one point, every state from its inputs."""
    table = load_traits()
    point_id = "p1"
    body = stubgen.GENERATED_HEADER + (
        "\n\ndef test_tool__B2(itest_target, itest_point, b2_fixtures):\n"
        f'    """itest point: {point_id}  trait: B2  schema: aaaaaaaaaaaa"""\n'
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
            test_name="test_tool__B2",
            ownership_hash=recorded,
            point_schema=schema,
            retired=retired,
            orphaned=orphaned,
        )

    assert table.get("B2").kind == "generated"
    assert state(owned, "aaaaaaaaaaaa") == "current"
    assert state("edited", "aaaaaaaaaaaa") == "hand_edited"
    assert state("edited", "bbbbbbbbbbbb") == "stale"
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
