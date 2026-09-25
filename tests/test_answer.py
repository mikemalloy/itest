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
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_reference_mcp_example import copy_example
from typer.testing import CliRunner

from itest.cli import app
from itest.core.manifest import Manifest, load_manifest
from itest.report import model as report_model

runner = CliRunner()

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOL_LEDGER = REPO_ROOT / "tests" / "fixtures" / "report" / "tool-ledger.json"
EVIDENCE = REPO_ROOT / "tests" / "fixtures" / "evidence"
PROMPTFOO_FIXTURE = EVIDENCE / "promptfoo-reference-mcp-2026-09-23.json"
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
