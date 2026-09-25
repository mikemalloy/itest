"""Every "Not run" reason reads as a plain sentence, per reason code.

The engine says why a check did not run with a **reason code** from one
closed vocabulary (``itest.core.reasons``): every ``not_verifiable`` result
names one, and verify stamps one on every held-out, skipped, missing,
unregistered or stub outcome. The Answer maps each code to one plain
sentence with a count and nothing else, never prints an engine string, and
fails loudly on a code it does not know rather than printing raw text. Two
reasons on one point are two lines, never a semicolon.

The 2026-09-25 staging page printed two engine strings verbatim (``tier``,
``environment``, ``guard``, two reasons glued with a semicolon); this file is
what keeps that from happening again.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

import pytest
import yaml
from test_answer import (
    JARGON_PATTERNS,
    JARGON_WORDS,
    _answer_block,
    _build,
    _first_check,
    _green,
    _strings,
)
from test_declarations_plan_sync import _sync, make_workdir, runner
from typer.testing import CliRunner

from itest.checks import CheckResult, run_engine_check, run_generated_check
from itest.checks._base import not_verifiable
from itest.cli import app
from itest.core import reasons
from itest.core.manifest import load_manifest, save_manifest
from itest.probes.mcp import McpTarget
from itest.report import model as report_model

REPO_ROOT = Path(__file__).resolve().parents[1]
CHECKS_DIR = REPO_ROOT / "itest" / "checks"
SIMPLE_WEB_APP = REPO_ROOT / "tests" / "fixtures" / "simple-web-app-plan.json"

#: The two engine strings the staging page printed. Neither may reach a line.
ENGINE_STRINGS = (
    "anonymous session admitted; this tool mutates, so its own guard can only "
    "be proven by an active-tier call on a non-production environment",
    "the check exists but has not been written yet",
)

#: Beyond the Answer's jargon list, the three words the staging lines used.
EXTRA_JARGON = ("tier", "environment", "guard")

#: The ledger status each code arrives with.
STATUS_FOR = {
    reasons.HELD_OUT_UNBOUND: "held_out",
    reasons.HELD_OUT_PRODUCTION: "held_out",
    reasons.HELD_OUT_WITHHELD: "held_out",
    reasons.UNWRITTEN: "not_verifiable",
    reasons.STDIO_BOUNDARY: "not_verifiable",
    reasons.DEFERRED: "not_verifiable",
    reasons.UNDECLARED: "not_verifiable",
    reasons.UNREACHABLE: "not_verifiable",
    reasons.NO_CREDENTIAL: "not_verifiable",
    reasons.NOT_LISTED: "not_verifiable",
    reasons.UNCLASSIFIED: "not_verifiable",
    reasons.NO_SAFE_CALL: "not_verifiable",
    reasons.ALREADY_MUTATING: "not_verifiable",
    reasons.NEEDS_FACTS: "not_run",
    reasons.UNREGISTERED: "not_run",
    reasons.SKIPPED: "not_run",
    reasons.MISSING: "not_run",
    reasons.STUB: "stub",  # a point's status, never a cell's
}


def _plain(text: str, *, line: bool = True) -> None:
    """No jargon and no engine string anywhere; no semicolon on a not-run
    line (two reasons are two lines)."""
    lowered = text.lower()
    for word in (*JARGON_WORDS, *EXTRA_JARGON):
        assert word not in lowered, (word, text)
    for pattern in JARGON_PATTERNS:
        assert not re.search(pattern, text), (pattern, text)
    if line:
        assert ";" not in text, text
    for engine in ENGINE_STRINGS:
        assert engine not in text, text


# --- the table --------------------------------------------------------------------


def test_the_vocabulary_is_closed_and_every_code_has_a_sentence() -> None:
    assert set(STATUS_FOR) == set(reasons.REASONS)
    assert set(report_model.REASON_SENTENCES) == set(reasons.REASONS)
    assert len(set(reasons.REASONS)) == len(reasons.REASONS)


@pytest.mark.parametrize("code", reasons.REASONS)
def test_each_code_reads_as_a_plain_sentence_with_a_count(code: str) -> None:
    one = report_model.not_run_sentence(code, 1)
    many = report_model.not_run_sentence(code, 3)
    assert one.startswith("1 check ") and not one.startswith("1 checks")
    assert many.startswith("3 checks ")
    for line in (one, many):
        assert line.endswith(".")
        _plain(line)


@pytest.mark.parametrize(
    "code", [code for code in reasons.REASONS if code != reasons.STUB]
)
def test_the_answer_renders_each_code_and_never_the_engine_detail(code: str) -> None:
    """The line is the code's sentence and the family, and nothing of the
    check's own detail — which here is the very string the staging page
    leaked."""
    ledger = _green()
    check = _first_check(ledger)
    check["status"] = STATUS_FOR[code]
    check["detail"] = ENGINE_STRINGS[0]
    check["reason"] = code
    page = _build(ledger)
    assert page.answer.not_run == [
        f"Not run here: Authority — {report_model.not_run_sentence(code, 1)}"
    ]
    _plain(page.answer.not_run[0])
    for text in _strings(_answer_block(page)):
        _plain(text, line=False)


def test_a_stub_point_and_a_gated_point_read_their_reason() -> None:
    points = [
        {"id": "p1", "status": "stub", "source": "a", "target": "b", "reason": "stub"},
        {
            "id": "p2",
            "status": "gated",
            "source": "a",
            "target": "c",
            "reason": reasons.HELD_OUT_PRODUCTION,
        },
    ]
    page = _build(None, points)
    assert page.answer.not_run == [
        "Not run here: Integrations — "
        + report_model.not_run_sentence(reasons.HELD_OUT_PRODUCTION, 1),
        "Not run here: Integrations — "
        + report_model.not_run_sentence(reasons.STUB, 1),
    ]


def test_two_reasons_on_one_tool_are_two_lines_never_a_semicolon() -> None:
    ledger = _green()
    checks = ledger["servers"][0]["tools"][0]["checks"]
    checks[0].update(status="held_out", reason=reasons.HELD_OUT_UNBOUND)
    checks[1].update(
        status="not_verifiable", detail=ENGINE_STRINGS[0], reason=reasons.DEFERRED
    )
    page = _build(ledger)
    assert len(page.answer.not_run) == 2
    for line in page.answer.not_run:
        assert line.startswith("Not run here: ")
        _plain(line)
    assert page.answer.not_run[0].endswith(
        report_model.not_run_sentence(reasons.HELD_OUT_UNBOUND, 1)
    )
    assert page.answer.not_run[1].endswith(
        report_model.not_run_sentence(reasons.DEFERRED, 1)
    )


def test_counts_add_up_across_tools_and_families() -> None:
    ledger = _green()
    for tool in ledger["servers"][0]["tools"]:
        for check in tool["checks"]:
            check.update(status="held_out", reason=reasons.HELD_OUT_UNBOUND)
    page = _build(ledger)
    (line,) = page.answer.not_run
    assert line == (
        "Not run here: Authority, Blast radius, Change — "
        + report_model.not_run_sentence(reasons.HELD_OUT_UNBOUND, 6)
    )


# --- loud failure -----------------------------------------------------------------


def test_an_unknown_reason_code_fails_loudly_at_render_time() -> None:
    ledger = _green()
    check = _first_check(ledger)
    check.update(status="not_verifiable", detail="something", reason="held_out.new")
    with pytest.raises(report_model.UnknownReason) as excinfo:
        _build(ledger)
    assert "held_out.new" in str(excinfo.value)


def test_a_not_verifiable_check_with_no_code_and_a_strange_detail_fails_loudly() -> (
    None
):
    """A ledger from before reason codes is read by its detail; a detail the
    Answer does not know is an error, never raw text on the page."""
    ledger = _green()
    check = _first_check(ledger)
    check.update(status="not_verifiable", detail="the server said something odd")
    check.pop("reason", None)
    with pytest.raises(report_model.UnknownReason) as excinfo:
        _build(ledger)
    assert "the server said something odd" in str(excinfo.value)


def test_a_ledger_written_before_reason_codes_still_reads() -> None:
    """The details the engine emitted before it carried codes map to codes."""
    ledger = _green()
    ledger["servers"][0]["environment"] = None  # the safe floor
    checks = ledger["servers"][0]["tools"][0]["checks"]
    checks[0].update(status="not_verifiable", detail=ENGINE_STRINGS[0])
    checks[1].update(
        status="not_verifiable", detail="no engine check for containment.egress"
    )
    checks[2].update(status="held_out", detail="withheld: the safe floor does not")
    checks[3].update(
        status="not_run",
        detail="no test is registered for this check; run `itest sync`",
    )
    for check in checks:
        check.pop("reason", None)
    page = _build(ledger)
    sentences = [line.split(" — ", 1)[1] for line in page.answer.not_run]
    assert sentences == [
        report_model.not_run_sentence(reasons.HELD_OUT_UNBOUND, 1),
        report_model.not_run_sentence(reasons.DEFERRED, 1),
        report_model.not_run_sentence(reasons.UNWRITTEN, 1),
        report_model.not_run_sentence(reasons.UNREGISTERED, 1),
    ]


# --- the engine names its reason ---------------------------------------------------


def test_every_not_verifiable_call_in_the_check_library_names_a_reason() -> None:
    """The keyword is required by signature; this makes sure no branch nobody
    exercised is one ``TypeError`` away from a crash."""
    missing = []
    for path in sorted(CHECKS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name != "not_verifiable":
                continue
            if not any(k.arg == "reason" for k in node.keywords):
                missing.append(f"{path.name}:{node.lineno}")
    assert missing == []


def test_not_verifiable_requires_a_reason_from_the_vocabulary() -> None:
    result = not_verifiable("why", reason=reasons.UNREACHABLE)
    assert result == CheckResult(
        status="not_verifiable", detail="why", evidence=None, reason="unreachable"
    )
    with pytest.raises(TypeError):
        not_verifiable("why")  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        not_verifiable("why", reason="made.up")


def test_an_unknown_trait_and_an_unbuilt_generated_check_are_unwritten() -> None:
    target = McpTarget(kind="stdio", command=[sys.executable, "-c", "pass"])
    point = {"id": "x", "type": "mcp_tool", "server": "s", "target": "t"}
    engine = run_engine_check("Q7", point, target, authenticated=False)
    assert (engine.status, engine.reason) == ("not_verifiable", reasons.UNWRITTEN)
    generated = run_generated_check(
        "authority.tenant_isolation", point, target, fixtures={}
    )
    assert (generated.status, generated.reason) == (
        "not_verifiable",
        reasons.UNWRITTEN,
    )


def test_a_server_that_cannot_be_reached_is_unreachable() -> None:
    """The wrapper turns a probe error into ``not_verifiable`` with the
    unreachable code: a stdio command that exits at once."""
    target = McpTarget(
        kind="stdio", command=[sys.executable, "-c", "import sys; sys.exit(3)"]
    )
    point = {
        "id": "x",
        "type": "mcp_tool",
        "server": "s",
        "target": "get_guide",
        "attributes": {"mutation": "read"},
    }
    result = run_engine_check("change.inventory", point, target, authenticated=False)
    assert result.status == "not_verifiable"
    assert result.reason == reasons.UNREACHABLE


# --- verify stamps a reason on everything that did not run ----------------------


def _checks(ledger: dict) -> list[dict]:
    return [c for s in ledger["servers"] for t in s["tools"] for c in t["checks"]]


@pytest.mark.slow
def test_verify_stamps_a_reason_on_every_check_that_did_not_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_workdir(tmp_path, monkeypatch)
    assert _sync().exit_code == 0
    result = runner.invoke(app, ["verify", "--output", "json"])
    assert result.exit_code in (0, 1), result.output
    checks = _checks(json.loads(result.output)["tools"])
    not_run = [
        c for c in checks if c["status"] in ("held_out", "not_verifiable", "not_run")
    ]
    assert not_run, "the safe floor holds the active tier out"
    for check in not_run:
        assert check["reason"] in reasons.REASONS, check
    assert {c["reason"] for c in checks if c["status"] == "held_out"} == {
        reasons.HELD_OUT_UNBOUND
    }
    # A check that ran carries no reason at all.
    assert all("reason" not in c for c in checks if c["status"] == "pass")
    # Bound to staging the active tier runs, and a generated check whose
    # conftest fixture is still the placeholder skips with the marker.
    result = runner.invoke(
        app, ["verify", "--environment", "staging", "--output", "json"]
    )
    assert result.exit_code in (0, 1), result.output
    checks = _checks(json.loads(result.output)["tools"])
    generated = [c for c in checks if c["trait"] == "blast.destructive_gating"]
    assert generated and {c["status"] for c in generated} == {"not_run"}
    assert {c["reason"] for c in generated} == {reasons.NEEDS_FACTS}


@pytest.mark.slow
def test_held_out_says_whether_production_or_a_policy_withheld_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workdir = make_workdir(tmp_path, monkeypatch)
    (workdir / ".itest" / "environments.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "environments": {
                    "staging": {"tiers": ["static", "readonly", "active"]},
                    "prod": {"tiers": ["static", "readonly"], "production": True},
                    "review": {"tiers": ["static", "readonly"]},
                },
            }
        ),
        encoding="utf-8",
    )
    assert _sync().exit_code == 0
    for environment, expected in (
        ("prod", reasons.HELD_OUT_PRODUCTION),
        ("review", reasons.HELD_OUT_WITHHELD),
    ):
        result = runner.invoke(
            app, ["verify", "--environment", environment, "--output", "json"]
        )
        assert result.exit_code in (0, 1), result.output
        held = [
            c
            for c in _checks(json.loads(result.output)["tools"])
            if c["status"] == "held_out"
        ]
        assert held and {c["reason"] for c in held} == {expected}


def test_a_stub_point_and_a_gated_point_carry_their_reason_in_verify_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cli = CliRunner()
    result = cli.invoke(
        app, ["sync", "--auto-approve", "--tf-json", str(SIMPLE_WEB_APP)]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(cli.invoke(app, ["verify", "--output", "json"]).output)
    assert {p["status"] for p in payload["points"]} == {"stub"}
    assert {p["reason"] for p in payload["points"]} == {reasons.STUB}

    manifest_file = tmp_path / ".itest" / "manifest.yaml"
    manifest = load_manifest(manifest_file)
    manifest.tests[0].tier = "active"
    save_manifest(manifest, manifest_file)
    (tmp_path / ".itest" / "environments.yaml").write_text(
        "version: 1\nenvironments:\n  prod: { tiers: [static, readonly] }\n",
        encoding="utf-8",
    )
    result = cli.invoke(app, ["verify", "--environment", "prod", "--output", "json"])
    assert result.exit_code == 0, result.output
    gated = [p for p in json.loads(result.output)["points"] if p["status"] == "gated"]
    assert gated and gated[0]["reason"] == reasons.HELD_OUT_PRODUCTION
