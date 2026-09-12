"""The standards view: one page, two lenses.

The trait families (authority / blast / containment / change) stay the
skeleton — a clean partition that carries the tier semantics and never gets
renumbered when OWASP publishes an edition. A trait maps to *several* published
ids, so standards cannot be the structure; they are a second lens over the same
checks. The rollup is derived from the checks in the ledger at emit time, never
from a hand-maintained list, and it names what ITest does NOT cover: an
uncovered ASI row printed as such is more credible than silence.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_declarations_plan_sync import _sync, make_workdir, runner

from itest.cli import app
from itest.core.declarations.traits import load_traits
from itest.report import model as report_model
from itest.report import render as report_render
from itest.traits import standards

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TOOL_LEDGER = FIXTURES / "report" / "tool-ledger.json"

ASI = [f"ASI{n:02d}" for n in range(1, 11)]
LLM = [f"LLM{n:02d}" for n in range(1, 11)]

ASI_TITLES = {
    "ASI01": "Agent Goal Hijack",
    "ASI02": "Tool Misuse",
    "ASI03": "Identity & Privilege Abuse",
    "ASI04": "Agentic Supply Chain Vulnerabilities",
    "ASI05": "Unexpected Code Execution",
    "ASI06": "Memory & Context Poisoning",
    "ASI07": "Insecure Inter-Agent Communication",
    "ASI08": "Cascading Failures",
    "ASI09": "Human-Agent Trust Exploitation",
    "ASI10": "Rogue Agents",
}


def _check(trait: str, status: str, state: str = "current") -> dict:
    row = load_traits().get(trait)
    return {
        "trait": trait,
        "code": row.code,
        "standards": list(row.standards),
        "status": status,
        "detail": "",
        "test": "",
        "state": state,
    }


def _by_id(rollup: list[dict]) -> dict[str, dict]:
    return {entry["id"]: entry for entry in rollup}


# --- the shipped catalog ------------------------------------------------------


def test_the_catalog_ships_the_full_asi_and_llm_lists() -> None:
    catalog = standards.load_standards()
    assert [e.id for e in catalog.framework("ASI").entries] == ASI
    assert {e.id: e.title for e in catalog.framework("ASI").entries} == ASI_TITLES
    assert [e.id for e in catalog.framework("LLM").entries] == LLM
    llm = {e.id: e.title for e in catalog.framework("LLM").entries}
    assert llm["LLM01"] == "Prompt Injection"
    assert llm["LLM03"] == "Excessive Agency"
    assert llm["LLM04"] == "Supply Chain"
    assert llm["LLM10"] == "Improper Output Handling"


def test_the_catalog_is_read_through_importlib_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_packaging import _fake_files

    shipped = (Path(standards.__file__).parent / standards.CATALOG_RESOURCE).read_text(
        "utf-8"
    )
    (tmp_path / standards.CATALOG_RESOURCE).write_text(
        shipped.replace("title: Agent Goal Hijack", "title: served by resources"),
        encoding="utf-8",
    )
    monkeypatch.setattr(standards, "resources", _fake_files("itest.traits", tmp_path))
    assert standards.load_standards().title("ASI01") == "served by resources"


def test_every_id_the_table_cites_has_a_title_in_the_catalog() -> None:
    catalog = standards.load_standards()
    for trait in load_traits().traits:
        for entry in trait.standards:
            assert catalog.title(entry) != entry, (trait.id, entry)


# --- the rollup is derived from the checks ------------------------------------


def test_the_rollup_counts_every_check_that_cites_an_id() -> None:
    checks = [
        _check("authority.anonymous", "pass"),
        _check("authority.anonymous", "fail"),
        _check("change.description_drift", "changed"),
    ]
    rollup = _by_id(standards.standards_rollup(checks))
    asi03 = rollup["ASI03"]
    assert asi03["title"] == "Identity & Privilege Abuse"
    assert asi03["coverage"] == "covered"
    assert asi03["checks"] == 2
    assert asi03["statuses"]["pass"] == 1 and asi03["statuses"]["fail"] == 1
    assert rollup["CWE-306"]["checks"] == 2
    assert rollup["ASI04"]["statuses"]["changed"] == 1

    # Add a check citing an id: its count moves, nothing else does.
    more = standards.standards_rollup(checks + [_check("change.inventory", "pass")])
    assert _by_id(more)["ASI04"]["checks"] == 2
    assert _by_id(more)["ASI03"]["checks"] == 2
    assert _by_id(more)["LLM04"]["checks"] == 1


def test_every_asi_entry_appears_and_an_uncited_one_is_not_covered() -> None:
    rollup = _by_id(standards.standards_rollup([_check("authority.anonymous", "pass")]))
    assert [i for i in rollup if i.startswith("ASI")] == ASI
    for uncited in ("ASI06", "ASI07", "ASI08", "ASI10"):
        entry = rollup[uncited]
        assert entry["coverage"] == "not_covered", uncited
        assert entry["checks"] == 0
        assert entry["note"], uncited
        assert entry["title"] == ASI_TITLES[uncited]
    assert "model-behaviour risk" in rollup["ASI06"]["note"]
    assert "multi-agent" in rollup["ASI07"]["note"]
    assert "never overstates" in rollup["ASI09"]["note"]


def test_a_covered_asi_entry_nothing_in_this_run_cites_is_not_covered_either() -> None:
    """ASI02 is covered by the table, but a run whose checks never cite it
    cannot claim it: coverage is a statement about this run's evidence."""
    rollup = _by_id(standards.standards_rollup([_check("authority.anonymous", "pass")]))
    assert rollup["ASI02"]["coverage"] == "not_covered"
    assert "no check in this run" in rollup["ASI02"]["note"]


def test_asi01_is_cited_by_description_drift_and_still_not_claimed() -> None:
    """Goal hijack is a model-behaviour risk. description_drift is its surface
    and cites it, so its counts show — and ITest still says not covered."""
    rollup = _by_id(
        standards.standards_rollup([_check("change.description_drift", "pass")])
    )
    assert rollup["ASI01"]["checks"] == 1
    assert rollup["ASI01"]["coverage"] == "not_covered"
    assert "change.description_drift" in rollup["ASI01"]["note"]


def test_asi05_is_partial_when_cited() -> None:
    rollup = _by_id(
        standards.standards_rollup(
            [_check("containment.expression_passthrough", "not_verifiable")]
        )
    )
    assert rollup["ASI05"]["coverage"] == "partial"
    assert "static analysis" in rollup["ASI05"]["note"]
    assert rollup["ASI05"]["statuses"]["not_verifiable"] == 1


def test_llm_ids_appear_only_when_cited() -> None:
    rollup = _by_id(standards.standards_rollup([_check("authority.anonymous", "pass")]))
    assert [i for i in rollup if i.startswith("LLM")] == ["LLM02"]
    assert rollup["LLM02"]["title"] == "Sensitive Information Disclosure"
    assert rollup["LLM02"]["coverage"] == "covered"
    assert "LLM05" not in rollup and "LLM06" not in rollup


def test_the_counts_by_status_include_not_applicable() -> None:
    checks = [
        _check("blast.mutation_class", "pass"),
        _check("blast.mutation_class", "critical"),
        _check("blast.mutation_class", "n/a", state="not_applicable"),
        _check("blast.mutation_class", "held_out"),
        _check("blast.mutation_class", "not_run"),
    ]
    entry = _by_id(standards.standards_rollup(checks))["ASI02"]
    assert entry["checks"] == 5
    assert entry["statuses"] == {
        "pass": 1,
        "fail": 0,
        "critical": 1,
        "changed": 0,
        "not_verifiable": 0,
        "not_applicable": 1,
        "held_out": 1,
        "not_run": 1,
    }


def test_an_empty_run_still_names_every_asi_row() -> None:
    rollup = standards.standards_rollup([])
    assert [e["id"] for e in rollup] == ASI
    assert {e["coverage"] for e in rollup} == {"not_covered"}


def test_the_rollup_is_ordered_by_framework_then_id() -> None:
    checks = [_check(t.id, "pass") for t in load_traits().traits]
    ids = [e["id"] for e in standards.standards_rollup(checks)]
    frameworks = [standards.framework_of(i) for i in ids]
    assert frameworks == sorted(frameworks, key=standards.FRAMEWORK_ORDER.index)
    assert ids[:10] == ASI
    assert ids.index("semgrep-server-4") < ids.index("semgrep-server-18")
    assert ids.index("CWE-77") < ids.index("CWE-306")


# --- verify emits it in the ledger ---------------------------------------------


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    workdir = make_workdir(tmp_path, monkeypatch)
    assert _sync().exit_code == 0
    return workdir


def _verify_json() -> dict:
    result = runner.invoke(
        app, ["verify", "--environment", "staging", "--output", "json"]
    )
    assert result.exit_code in (0, 1), result.output
    return json.loads(result.output)


def test_verify_emits_a_rollup_derived_from_its_own_checks(workdir: Path) -> None:
    payload = _verify_json()
    ledger = payload["tools"]
    checks = [c for s in ledger["servers"] for t in s["tools"] for c in t["checks"]]
    assert ledger["standards"] == standards.standards_rollup(checks)
    by_id = _by_id(ledger["standards"])
    assert [i for i in by_id if i.startswith("ASI")] == ASI
    # Over stdio nothing cites CWE-306 (no anonymous check), and ASI03 is
    # still cited by the identity and isolation bindings.
    assert "CWE-306" not in by_id
    assert by_id["ASI03"]["checks"] > 0
    assert by_id["ASI02"]["statuses"]["pass"] >= 8  # blast.mutation_class
    assert report_model.ToolLedger.model_validate(ledger).standards


# --- the page band and the command --------------------------------------------


@pytest.fixture
def ledger_page(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from test_declarations_plan_sync import ALEX
    from test_report import synced, verify_json

    project = synced(tmp_path, monkeypatch, ALEX)
    verify = verify_json(project)
    verify["tools"] = json.loads(TOOL_LEDGER.read_text(encoding="utf-8"))["tools"]
    from itest.core.manifest import load_manifest

    manifest = load_manifest(project / ".itest" / "manifest.yaml")
    return verify, manifest


def test_the_band_renders_one_row_per_asi_entry_and_the_grid_is_untouched(
    ledger_page,
) -> None:
    verify, manifest = ledger_page
    before = report_render.build_blocks(report_model.build(verify, manifest))
    band = before["PAGE"]["standards"]
    assert [r["id"] for r in band["rows"]] == ASI
    assert {r["title"] for r in band["rows"]} == set(ASI_TITLES.values())
    by_id = {r["id"]: r for r in band["rows"]}
    assert by_id["ASI03"]["coverage"] == "covered"
    assert by_id["ASI03"]["label"] == "covered"
    assert by_id["ASI03"]["checks"] == 2
    assert {b["l"]: b["n"] for b in by_id["ASI03"]["bar"]} == {"pass": 2}
    assert by_id["ASI06"]["coverage"] == "not_covered"
    assert by_id["ASI06"]["label"] == "not covered"
    assert by_id["ASI06"]["quiet"] is True and by_id["ASI03"]["quiet"] is False
    assert by_id["ASI06"]["note"]
    assert "ASI" in band["eyebrow"]
    # The families grid is the skeleton and does not move.
    assert len(before["TOOLPOSTURE"]) == 4
    assert [t["lab"].split(" checks")[0] for t in before["TOOLPOSTURE"]] == [
        "Authority",
        "Blast radius",
        "Containment",
        "Change",
    ]
    html = report_render.render(report_model.build(verify, manifest))
    assert "Agent Goal Hijack" in html and 'id="standardsBand"' in html


def test_a_ledger_without_a_rollup_gets_one_derived_the_same_way(ledger_page) -> None:
    """An older verify JSON has no tools.standards: the page derives it from
    the checks it does have, through the one function verify uses."""
    verify, manifest = ledger_page
    assert "standards" not in verify["tools"]
    page = report_model.build(verify, manifest)
    checks = [c for s in page.tools.servers for t in s.tools for c in t.checks]
    derived = standards.standards_rollup([c.model_dump() for c in checks])
    assert [e.model_dump() for e in page.tools.standards] == derived


def test_no_tools_means_a_named_empty_band(tmp_path: Path, monkeypatch) -> None:
    from test_declarations_plan_sync import ALEX
    from test_report import synced, verify_json

    project = synced(tmp_path, monkeypatch, ALEX)
    from itest.core.manifest import load_manifest

    page = report_model.build(
        verify_json(project), load_manifest(project / ".itest" / "manifest.yaml")
    )
    band = report_render.build_blocks(page)["PAGE"]["standards"]
    assert band["rows"] == []
    assert "No agent tools declared" in band["empty"]


def test_itest_standards_json_is_the_ledgers_rollup(workdir: Path) -> None:
    payload = _verify_json()
    source = workdir / "verify.json"
    source.write_text(json.dumps(payload), encoding="utf-8")
    result = runner.invoke(app, ["standards", "--from", str(source), "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == payload["tools"]["standards"]


def test_itest_standards_prints_the_rollup_and_names_what_is_not_covered(
    workdir: Path,
) -> None:
    payload = _verify_json()
    source = workdir / "verify.json"
    source.write_text(json.dumps(payload), encoding="utf-8")
    result = runner.invoke(app, ["standards", "--from", str(source)])
    assert result.exit_code == 0, result.output
    out = result.output
    assert out.splitlines()[0].startswith("Standards view")
    for asi in ASI:
        assert asi in out, asi
    assert "Agent Goal Hijack" in out
    assert "not covered" in out
    assert "partial" in out  # ASI05 via containment.expression_passthrough
    assert "model-behaviour risk" in out
    assert "ACS-AgBOM" in out


def test_itest_standards_reads_stdin_when_no_file_is_given(workdir: Path) -> None:
    payload = _verify_json()
    result = runner.invoke(app, ["standards", "--json"], input=json.dumps(payload))
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == payload["tools"]["standards"]


def test_itest_standards_without_tools_says_so(tmp_path: Path, monkeypatch) -> None:
    from test_declarations_plan_sync import ALEX
    from test_report import synced, verify_json

    project = synced(tmp_path, monkeypatch, ALEX)
    source = project / "verify.json"
    source.write_text(json.dumps(verify_json(project)), encoding="utf-8")
    result = runner.invoke(app, ["standards", "--from", str(source)])
    assert result.exit_code == 0, result.output
    assert "No agent tools declared" in result.output
    as_json = runner.invoke(app, ["standards", "--from", str(source), "--json"])
    assert json.loads(as_json.output) == []


def test_itest_standards_refuses_a_missing_or_invalid_document(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["standards", "--from", "nope.json"])
    assert result.exit_code == 2
    assert "nope.json" in result.output
    (tmp_path / "bad.json").write_text("{", encoding="utf-8")
    result = runner.invoke(app, ["standards", "--from", "bad.json"])
    assert result.exit_code == 2
