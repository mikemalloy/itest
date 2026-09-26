"""The verdict vocabulary and the Answer layer of the readiness page.

Four words, in precedence order: BLOCKED (a finding), NEEDS REVIEW (a human
decision is pending), PARTIAL (nothing wrong that we know of; we did not look
at everything), VERIFIED (every declared property of every declared tool
passed and nothing is waiting on review). "Unchecked" is never "at risk".

The CI fixture here is the committed example, synced and verified over stdio
with no environment bound — exactly the run the scheduled job makes — with the
committed 2026-09-23 promptfoo results standing in for the harness's output.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from test_reference_mcp_example import copy_example
from typer.testing import CliRunner

from itest.cli import app
from itest.core.evidence.join import join_evidence
from itest.core.evidence.loader import LoadedSource
from itest.core.evidence.promptfoo import read_results
from itest.core.evidence.schema import EvidenceSource
from itest.core.manifest import (
    EvidenceRecord,
    IntegrationPoint,
    Manifest,
    load_manifest,
)
from itest.report import model as report_model
from itest.report import render as report_render

runner = CliRunner()

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOL_LEDGER = REPO_ROOT / "tests" / "fixtures" / "report" / "tool-ledger.json"
EVIDENCE = REPO_ROOT / "tests" / "fixtures" / "evidence"
PROMPTFOO_FIXTURE = EVIDENCE / "promptfoo-reference-mcp-2026-09-23.json"
SYNTHETIC_TARGETED = EVIDENCE / "promptfoo-targeted-synthetic.json"
PROMPTFOO_CONFIG = (
    REPO_ROOT / "examples" / "reference-mcp" / "redteam" / "promptfooconfig.yaml"
)
TEMPLATE = REPO_ROOT / "itest" / "report" / "templates" / "readiness.html"
NOW = datetime(2026, 9, 25, 9, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def ci_fixture(tmp_path_factory: pytest.TempPathFactory) -> tuple[dict, Manifest]:
    """reference-mcp, synced and verified with no environment bound: every
    active check held out, zero failures, the committed red-team results
    joined as evidence."""
    example = copy_example(tmp_path_factory.mktemp("ci") / "reference-mcp")
    patch = pytest.MonkeyPatch()
    patch.chdir(example)
    patch.setenv("ITEST_PROMPTFOO_RESULTS", str(PROMPTFOO_FIXTURE))
    for name in ("REFERENCE_MCP_OPEN_URL", "REFERENCE_MCP_TOKEN", "ITEST_ENVIRONMENT"):
        patch.delenv(name, raising=False)
    try:
        result = runner.invoke(app, ["sync", "--auto-approve", "--allow-unreachable"])
        assert result.exit_code == 0, result.output
        result = runner.invoke(app, ["verify", "--output", "json"])
        assert result.exit_code == 0, result.output
        verify = json.loads(result.output)
        manifest = load_manifest(example / ".itest" / "manifest.yaml")
    finally:
        patch.undo()
    assert verify["on_safe_floor"] is True and verify["environment"] is None
    assert manifest.evidence, "the committed results joined"
    return verify, manifest


def _ledger() -> dict:
    return json.loads(TOOL_LEDGER.read_text(encoding="utf-8"))["tools"]


def _set(ledger: dict, status: str, *, state: str = "current") -> dict:
    """Every check in ``ledger`` at ``status``; the summary agrees."""
    (server,) = ledger["servers"]
    for tool in server["tools"]:
        for check in tool["checks"]:
            check["status"] = status
            check["state"] = state
            check.pop("change", None)
    summary = server["summary"]
    summary["changed"] = 0
    summary["critical"] = 0
    summary["verified"] = summary["declared"] if status == "pass" else 0
    server["exceptions"] = []
    return ledger


def _green() -> dict:
    return _set(_ledger(), "pass")


def _build(ledger: dict | None, points: list[dict] | None = None) -> report_model.Page:
    verify: dict = {"points": points or []}
    if ledger is not None:
        verify["tools"] = ledger
    return report_model.build(verify, Manifest(generated_at=NOW), generated_at=NOW)


def _first_check(ledger: dict) -> dict:
    return ledger["servers"][0]["tools"][0]["checks"][0]


# --- the four words -------------------------------------------------------------------


def test_the_vocabulary_is_the_four_words_in_precedence_order() -> None:
    assert report_model.VERDICT_WORDS == (
        "BLOCKED",
        "NEEDS REVIEW",
        "PARTIAL",
        "VERIFIED",
    )


@pytest.mark.slow
def test_the_ci_fixture_is_partial_not_at_risk(ci_fixture) -> None:
    """Every active check held out and nothing failed: we did not look at
    everything, and nothing is wrong that we know of."""
    verify, manifest = ci_fixture
    page = report_model.build(verify, manifest, generated_at=NOW)
    assert page.verdict.word == "PARTIAL"
    assert page.verdict.reason.startswith("No findings. ")


def test_one_changed_check_and_nothing_failing_is_needs_review() -> None:
    ledger = _green()
    _first_check(ledger)["status"] = "changed"
    page = _build(ledger)
    assert page.verdict.word == "NEEDS REVIEW"
    assert page.verdict.reason == (
        "No findings. 1 tool change is waiting for a reviewer."
    )


def test_a_stale_check_is_needs_review() -> None:
    ledger = _green()
    _first_check(ledger)["state"] = "stale"
    assert _build(ledger).verdict.word == "NEEDS REVIEW"


def test_one_critical_is_blocked_even_if_everything_else_is_held_out() -> None:
    ledger = _set(_ledger(), "held_out")
    _first_check(ledger)["status"] = "critical"
    page = _build(ledger)
    assert page.verdict.word == "BLOCKED"
    assert page.verdict.reason == "1 finding needs attention before release."


def test_every_declared_property_passed_is_verified() -> None:
    page = _build(_green())
    assert page.verdict.word == "VERIFIED"
    assert page.verdict.reason == "No findings. All 6 checks passed."


def test_held_out_alone_is_partial_never_amber() -> None:
    ledger = _set(_ledger(), "held_out")
    page = _build(ledger)
    assert page.verdict.word == "PARTIAL"
    assert page.verdict.reason == (
        "No findings. 0 of 6 checks passed; 6 were not run here."
    )


def test_pending_beats_partial() -> None:
    ledger = _green()
    checks = ledger["servers"][0]["tools"][0]["checks"]
    checks[0]["status"] = "changed"
    checks[1]["status"] = "held_out"
    assert _build(ledger).verdict.word == "NEEDS REVIEW"


def test_blocked_beats_pending() -> None:
    ledger = _green()
    checks = ledger["servers"][0]["tools"][0]["checks"]
    checks[0]["status"] = "critical"
    checks[1]["status"] = "changed"
    page = _build(ledger)
    assert page.verdict.word == "BLOCKED"
    assert page.verdict.reason == "1 finding needs attention before release."


def test_two_findings_pluralize() -> None:
    ledger = _green()
    checks = ledger["servers"][0]["tools"][0]["checks"]
    checks[0]["status"] = "critical"
    checks[1]["status"] = "fail"
    page = _build(ledger)
    assert page.verdict.word == "BLOCKED"
    assert page.verdict.reason == "2 findings need attention before release."


def test_a_stub_point_is_partial_and_a_failing_point_is_blocked() -> None:
    stub = [{"id": "p1", "status": "stub", "source": "a", "target": "b"}]
    assert _build(None, stub).verdict.word == "PARTIAL"
    assert _build(None, stub).verdict.reason == (
        "No findings. 0 of 1 check passed; 1 was not run here."
    )
    failing = [{"id": "p1", "status": "failing", "source": "a", "target": "b"}]
    assert _build(None, failing).verdict.word == "BLOCKED"


def test_the_word_is_never_at_risk_anywhere_in_the_repo() -> None:
    """The split is the point: 'unchecked' must not read as 'danger'. The one
    place the old word may survive is the scope-ledger line explaining it."""
    if not (REPO_ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    tracked = (
        subprocess.run(
            ["git", "ls-files", "-z"], cwd=REPO_ROOT, capture_output=True, check=True
        )
        .stdout.decode("utf-8")
        .split("\0")
    )
    old_word = "AT " + "RISK"  # spelled apart so this file is not a hit
    hits: list[str] = []
    for name in filter(None, tracked):
        if name == "DESIGN.md":
            continue  # the changelog line lives in its scope ledger
        try:
            text = (REPO_ROOT / name).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if old_word in text:
            hits.append(name)
    assert hits == []


# --- the Answer -----------------------------------------------------------------------

#: The jargon contract. The Answer block is the plain-language layer; every
#: method word the engineer relies on stays in the detail layer, spelled as
#: it is today, and none of it appears above the divider. Substrings are
#: matched case-insensitively; the patterns catch a trait slug, a standards
#: id, a run id and an ISO timestamp.
JARGON_WORDS = (
    "held out",
    "not verifiable",
    "environment bound",
    "safe floor",
    "integration point",
    "judgment",
    "lane",
    "harness",
    "targeting",
    "tier",
    "readonly",
    "stub",
    "orphan",
    "manifest",
    "lifecycle",
    "rollup",
    "evidence lane",
    "external evidence",
    "declared",
    # What a pytest failure would bring, and must not.
    "sentinel",
    "traceback",
    "assert",
)
JARGON_PATTERNS = (
    r"\b[a-z]+\.[a-z_]+\b",  # a trait slug (authority.anonymous)
    r"\b[0-9a-f]{12}\b",  # a state or ownership hash
    r"\bASI\d\d\b",  # a standards id
    r"\bLLM\d\d\b",
    r"\beval-\w+",  # a run id
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}",  # an ISO timestamp
)

DELETE_RECORD_POINT = "3f9a1c2b7d10"  # delete_record in the fixture ledger


def _strings(value: object) -> list[str]:
    """Every string anywhere in a data block."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


def _answer_block(page: report_model.Page) -> dict:
    return report_render.extract_blocks(report_render.render(page))["PAGE"]["answer"]


def _join(manifest: Manifest, results: Path, *, max_age_days: int = 7) -> Manifest:
    """``manifest`` with only ``results`` joined as its evidence, through the
    real reader and the real join."""
    manifest = manifest.model_copy(deep=True)
    source = LoadedSource(
        name="promptfoo-lab",
        source=EvidenceSource(
            kind="promptfoo",
            server="reference-mcp",
            results=str(results),
            max_age_days=max_age_days,
        ),
        results_path=results,
    )
    run = read_results(results, source_name="promptfoo-lab", agent=None)
    records, line = join_evidence(run, source, manifest, now=NOW)
    manifest.evidence = records
    manifest.sources = [line]
    return manifest


def _delete_record_manifest(**record: object) -> Manifest:
    manifest = Manifest(
        generated_at=NOW,
        points=[
            IntegrationPoint(
                id=DELETE_RECORD_POINT,
                type="mcp_tool",
                source="reference-mcp",
                target="delete_record",
                hcl_address=".itest/tools/reference-mcp.yaml",
                origin="declared",
                first_seen=NOW,
                last_seen=NOW,
                attributes={"mutation": "destructive"},
            )
        ],
    )
    if record:
        manifest.evidence = [
            EvidenceRecord(
                point_id=DELETE_RECORD_POINT,
                source_name="promptfoo-lab",
                kind="promptfoo",
                run_id="eval-zyk-2026-09-23T18:26:29",
                run_at="2026-09-23T18:26:29.565Z",
                rows_total=12,
                recorded_at=NOW,
                **record,
            )
        ]
        manifest.sources = [
            report_model.SourceRecord(
                name="promptfoo-lab",
                kind="promptfoo",
                server="reference-mcp",
                run_id="eval-zyk-2026-09-23T18:26:29",
                run_at="2026-09-23T18:26:29.565Z",
                status="read",
                shape="agent",
                rows=12,
                matched_tools=["delete_record"],
                stale=bool(record.get("stale")),
            )
        ]
    return manifest


@pytest.mark.slow
def test_the_answer_for_the_ci_fixture(ci_fixture) -> None:
    verify, manifest = ci_fixture
    page = report_model.build(verify, manifest, generated_at=NOW)
    answer = page.answer
    assert answer.word == "PARTIAL"
    assert answer.sentence.startswith("No findings.")
    assert answer.findings == []
    block = _answer_block(page)
    assert block["findings"] == []
    assert "Findings" not in _strings(block)  # no empty heading
    held = [line for line in answer.not_run if "non-production copy" in line]
    (line,) = held
    assert line.startswith("Not run here: Authority, Blast radius, Containment — ")
    assert line.endswith("and this run was not pointed at one.")
    for line in answer.not_run:
        assert ";" not in line
    assert answer.ran.startswith("Ran: ")
    (red_team,) = answer.red_team
    assert red_team.text.startswith("Red team (2026-09-23): 12 attempts.")
    assert "No write or destructive tool was called." in red_team.text
    assert red_team.warning is False
    assert answer.footer == "Detail below."


def test_a_critical_names_the_tool_and_the_traits_human_name() -> None:
    ledger = _green()
    check = ledger["servers"][0]["tools"][0]["checks"][1]
    check["trait"] = "blast.destructive_gating"
    check["code"] = "BLAST-2"
    check["status"] = "critical"
    check["detail"] = "an anonymous caller deleted a record"
    page = _build(ledger)
    assert page.answer.word == "BLOCKED"
    assert page.answer.sentence == "1 finding needs attention before release."
    (finding,) = page.answer.findings
    assert finding.severity == "critical"
    assert (finding.source, finding.target) == ("reference-mcp", "delete_record")
    assert finding.check == "destructive gating"
    assert finding.detail == "an anonymous caller deleted a record"
    block = _answer_block(page)
    (line,) = block["findings"]
    assert line["text"].startswith(
        "reference-mcp -> delete_record — destructive gating"
    )
    assert "blast.destructive_gating" not in line["text"]


def test_findings_sort_critical_first() -> None:
    ledger = _green()
    checks = ledger["servers"][0]["tools"][0]["checks"]
    checks[0]["status"] = "fail"
    checks[1]["status"] = "critical"
    assert [f.severity for f in _build(ledger).answer.findings] == [
        "critical",
        "failing",
    ]


def test_a_called_destructive_tool_is_named_and_never_moves_the_verdict() -> None:
    verify = {"points": [], "tools": _green()}
    without = report_model.build(verify, _delete_record_manifest(), generated_at=NOW)
    manifest = _delete_record_manifest(calls=1, succeeded=1, rows_with_call=1)
    page = report_model.build(verify, manifest, generated_at=NOW)
    (line,) = page.answer.red_team
    assert line.warning is True
    assert "1 write/destructive tool was called: delete_record" in line.text
    assert "1 tool exercised." in line.text
    assert page.verdict.word == without.verdict.word
    assert page.verdict == without.verdict
    assert without.answer.red_team == []


def test_stale_evidence_is_marked_with_its_date() -> None:
    verify = {"points": [], "tools": _green()}
    manifest = _delete_record_manifest(calls=1, refused=1, rows_with_call=1, stale=True)
    page = report_model.build(verify, manifest, generated_at=NOW)
    (line,) = page.answer.red_team
    assert line.text.endswith("(stale — from 2026-09-23)")
    assert page.verdict.word == "VERIFIED"


@pytest.mark.slow
def test_declared_targeting_reads_as_targeted_induced_refused(ci_fixture) -> None:
    """Template (a): the harness said what it aimed at, so the line can say
    how often the aim landed and how often the tool refused."""
    verify, manifest = ci_fixture
    plain = report_model.build(
        verify,
        manifest.model_copy(update={"evidence": [], "sources": []}),
        generated_at=NOW,
    )
    page = report_model.build(
        verify, _join(manifest, SYNTHETIC_TARGETED), generated_at=NOW
    )
    (line,) = page.answer.red_team
    # Rows throughout, and only targeted rows: the fixture's benign untargeted
    # call and its refused-twice row change neither number.
    assert line.text == (
        "Red team (2026-09-24): 13 attempts. 8 targeted delete_record; "
        "2 induced a call; 2 of those were refused by the tool."
    )
    assert line.warning is False
    assert page.verdict.word == plain.verdict.word
    assert page.verdict == plain.verdict


@pytest.mark.slow
def test_a_benign_untargeted_call_does_not_count_as_induced(
    ci_fixture, tmp_path: Path
) -> None:
    """A tool called on a row the harness did not aim at it is a benign call,
    not an induced one: rows_with_call goes up, the sentence does not."""
    verify, manifest = ci_fixture
    document = json.loads(SYNTHETIC_TARGETED.read_text(encoding="utf-8"))
    rows = document["results"]["results"]

    def calls_delete(row: dict) -> bool:
        return any(c["name"] == "delete_record" for c in row["metadata"]["toolCalls"])

    benign = [r for r in rows if not r["testCase"].get("metadata") and calls_delete(r)]
    assert len(benign) == 1, "the fixture has one benign untargeted call"
    document["results"]["results"] = [r for r in rows if r not in benign]
    results = tmp_path / "no-benign.json"
    results.write_text(json.dumps(document), encoding="utf-8")

    full = _join(manifest, SYNTHETIC_TARGETED)
    fewer = _join(manifest, results)
    (record,) = [r for r in full.evidence if r.targeted]
    (fewer_record,) = [r for r in fewer.evidence if r.targeted]
    assert record.rows_with_call == fewer_record.rows_with_call + 1
    assert record.targeted_rows_with_call == fewer_record.targeted_rows_with_call

    (line,) = report_model.build(verify, full, generated_at=NOW).answer.red_team
    (fewer_line,) = report_model.build(verify, fewer, generated_at=NOW).answer.red_team
    assert line.text.replace("13 attempts", "12 attempts") == fewer_line.text


@pytest.mark.slow
def test_a_refused_then_admitted_row_shows_as_succeeded_and_warns(
    ci_fixture, tmp_path: Path
) -> None:
    """A targeted row where the tool refused once and then let a retry
    through is refused AND succeeded, and the line says so — styled as a
    warning, and still never a verdict input."""
    verify, manifest = ci_fixture
    document = json.loads(SYNTHETIC_TARGETED.read_text(encoding="utf-8"))
    row = document["results"]["results"][11]
    assert row["testCase"]["metadata"] == {"target_tool": "delete_record"}
    for holder in (row["metadata"], row["response"]["metadata"]):
        holder["toolCalls"][-1]["is_error"] = False
    results = tmp_path / "mixed.json"
    results.write_text(json.dumps(document), encoding="utf-8")
    plain = report_model.build(
        verify,
        manifest.model_copy(update={"evidence": [], "sources": []}),
        generated_at=NOW,
    )
    page = report_model.build(verify, _join(manifest, results), generated_at=NOW)
    (line,) = page.answer.red_team
    assert line.text == (
        "Red team (2026-09-24): 13 attempts. 8 targeted delete_record; "
        "2 induced a call; 2 of those were refused by the tool; 1 succeeded."
    )
    assert line.warning is True
    assert page.verdict == plain.verdict


@pytest.mark.slow
def test_declared_targeting_with_nothing_induced(ci_fixture, tmp_path: Path) -> None:
    verify, manifest = ci_fixture
    document = json.loads(SYNTHETIC_TARGETED.read_text(encoding="utf-8"))
    for row in document["results"]["results"]:
        for holder in (row["metadata"], row["response"]["metadata"]):
            holder["toolCalls"] = [
                c for c in holder["toolCalls"] if c["name"] != "delete_record"
            ]
    results = tmp_path / "none-induced.json"
    results.write_text(json.dumps(document), encoding="utf-8")
    page = report_model.build(verify, _join(manifest, results), generated_at=NOW)
    (line,) = page.answer.red_team
    assert line.text == (
        "Red team (2026-09-24): 13 attempts. 8 targeted delete_record; "
        "0 induced a call."
    )
    assert "refused" not in line.text


def test_the_corpus_declares_its_target_on_the_injection_cases_only() -> None:
    tests = yaml.safe_load(PROMPTFOO_CONFIG.read_text(encoding="utf-8"))["tests"]
    baseline, indirect, direct = tests
    assert baseline["description"].startswith("BASELINE")
    assert "metadata" not in baseline
    for case in (indirect, direct):
        assert case["metadata"] == {"target_tool": "delete_record"}


def test_the_synthetic_fixture_says_it_is_synthetic() -> None:
    document = json.loads(SYNTHETIC_TARGETED.read_text(encoding="utf-8"))
    assert document["_note"].startswith("SYNTHETIC")
    run = read_results(SYNTHETIC_TARGETED, source_name="s", agent=None)
    assert run.rows == 13
    delete = run.per_tool["delete_record"]
    assert delete.targeted == 8
    assert (delete.rows_with_call, delete.refused) == (3, 3)  # run-wide
    assert (delete.targeted_rows_with_call, delete.targeted_rows_refused) == (2, 2)
    assert run.per_tool["get_guide"].targeted == 0


def _template_answer_source() -> str:
    """The template's Answer-rendering script, between its two markers."""
    text = TEMPLATE.read_text(encoding="utf-8")
    start = text.index("/* ---- answer ---- */")
    end = text.index("/* ---- answer:end ---- */")
    return text[start:end]


@pytest.mark.slow
def test_the_answer_block_uses_no_method_vocabulary(ci_fixture) -> None:
    verify, manifest = ci_fixture
    page = report_model.build(verify, manifest, generated_at=NOW)
    for text in _strings(_answer_block(page)):
        lowered = text.lower()
        for word in JARGON_WORDS:
            assert word not in lowered, (word, text)
        for pattern in JARGON_PATTERNS:
            assert not re.search(pattern, text), (pattern, text)
    # The template's script for the block carries no words of its own (every
    # label is data), so the word list is enough there: its member accesses
    # (``a.findings``) would trip the slug pattern for the wrong reason.
    script = _template_answer_source().lower()
    for word in JARGON_WORDS:
        assert word not in script, word


@pytest.mark.slow
def test_the_answer_comes_first_and_the_hero_tiles_moved_down(ci_fixture) -> None:
    verify, manifest = ci_fixture
    page = report_model.build(verify, manifest, generated_at=NOW)
    html = report_render.render(page)
    assert (
        html.index('id="answer"')
        < html.index('id="divider"')
        < html.index('id="verdict"')
    )
    blocks = report_render.extract_blocks(html)
    labels = [n["label"] for n in blocks["PAGE"]["verdict"]["nums"]]
    assert "agent tools verified" not in labels
    assert "integrations verified" not in labels
    assert blocks["TOOLPOSTURE"][0]["lab"].startswith("agent tools verified")
    assert blocks["POSTURE"][0]["lab"].startswith("integrations verified")
