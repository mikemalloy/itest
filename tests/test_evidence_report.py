"""The evidence lane on the readiness page.

A tool row that has evidence gains a second, visually distinct lane beneath
it, labelled "external evidence" with the source's standards ids, carrying the
rate as its parts — ``calls N · refused R · of T rows``, never a percentage
alone. The source lines sit in the tools section. The standards view lists an
id that only a source cites under its own "external evidence" heading with no
covered count, and an id a check also cites keeps the check's coverage with the
evidence listed beside it.

None of it touches the ledger's counts: ``verified()``, ``changed``,
``critical``, ``state_counts()`` and the verdict are the same with the lane and
without it. And nothing is emitted for a page without evidence, so the pinned
snapshots do not move.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_report import GENERATED_AT, TOOL_LEDGER, alex_s7  # noqa: F401 (fixture)

from itest.core.manifest import EvidenceRecord, Manifest, SourceRecord
from itest.report import model as report_model
from itest.report import render as report_render

DELETE_RECORD = "3f9a1c2b7d10"
UPDATE_RECORD = "8c7d6e5f4a39"
NOW = datetime(2026, 9, 24, 9, 0, tzinfo=UTC)


def _ledger() -> dict:
    return json.loads(TOOL_LEDGER.read_text(encoding="utf-8"))["tools"]


def _source(**changes: object) -> SourceRecord:
    defaults = dict(
        name="promptfoo-lab",
        kind="promptfoo",
        server="reference-mcp",
        run_id="eval-zyk-2026-09-23T18:26:29",
        run_at="2026-09-23T18:26:29.565Z",
        status="read",
        shape="agent",
        rows=12,
        matched_tools=["delete_record"],
        unmatched_tools=["get_guide"],
        agent="sonnet-5 + haiku-4-5 via agent.js",
        standards=["ASI01", "ASI06"],
        notes=["2 row(s) named no tool", "targeting not declared by the harness"],
    )
    return SourceRecord(**{**defaults, **changes})


def _record(**changes: object) -> EvidenceRecord:
    defaults = dict(
        point_id=DELETE_RECORD,
        source_name="promptfoo-lab",
        kind="promptfoo",
        run_id="eval-zyk-2026-09-23T18:26:29",
        run_at="2026-09-23T18:26:29.565Z",
        tool_version="0.123.0",
        agent="sonnet-5 + haiku-4-5 via agent.js",
        calls=10,
        refused=0,
        succeeded=10,
        rows_total=12,
        rows_with_call=10,
        recorded_at=NOW,
    )
    return EvidenceRecord(**{**defaults, **changes})


@pytest.fixture
def with_ledger(alex_s7) -> tuple[dict, Manifest]:  # noqa: F811 (fixture)
    verify, manifest = alex_s7
    verify["tools"] = _ledger()
    return verify, manifest


def _pages(
    verify: dict, manifest: Manifest
) -> tuple[report_model.Page, report_model.Page]:
    """The page without the lane, and the same page with it."""
    plain = report_model.build(verify, manifest, generated_at=GENERATED_AT)
    manifest.sources = [_source()]
    manifest.evidence = [_record()]
    lane = report_model.build(verify, manifest, generated_at=GENERATED_AT)
    return plain, lane


# --- the model ---------------------------------------------------------------------


def test_a_tool_with_evidence_carries_a_lane(with_ledger) -> None:
    _, page = _pages(*with_ledger)
    (server,) = page.tools.servers
    delete, update = server.tools
    assert update.evidence == []
    (lane,) = delete.evidence
    assert lane.source == "promptfoo-lab"
    assert lane.kind == "promptfoo"
    assert lane.agent == "sonnet-5 + haiku-4-5 via agent.js"
    assert lane.run_id == "eval-zyk-2026-09-23T18:26:29"
    assert lane.run_at == "2026-09-23T18:26:29.565Z"
    assert lane.stale is False
    assert lane.rate == "calls 10 · refused 0 · of 12 rows"
    assert lane.targeted == "targeting not declared by the harness"
    assert lane.standards == ["ASI01", "ASI06"]


def test_a_declared_target_count_is_shown_as_rows(with_ledger) -> None:
    verify, manifest = with_ledger
    manifest.sources = [_source()]
    manifest.evidence = [_record(targeted=3, calls=0, succeeded=0, rows_with_call=0)]
    page = report_model.build(verify, manifest, generated_at=GENERATED_AT)
    (lane,) = page.tools.servers[0].tools[0].evidence
    assert lane.rate == "calls 0 · refused 0 · of 12 rows"
    assert lane.targeted == "targeted in 3 of 12 rows"


def test_the_source_lines_sit_on_their_server(with_ledger) -> None:
    _, page = _pages(*with_ledger)
    (server,) = page.tools.servers
    (line,) = server.sources
    assert line.name == "promptfoo-lab"
    assert line.status == "read"
    assert line.unmatched_tools == ["get_guide"]
    assert line.matched_tools == ["delete_record"]
    assert line.notes == [
        "2 row(s) named no tool",
        "targeting not declared by the harness",
    ]
    assert page.unattached_sources == []


def test_a_source_for_an_unknown_server_is_still_on_the_page(with_ledger) -> None:
    verify, manifest = with_ledger
    manifest.sources = [
        _source(name="broken", kind=None, server=None, status="unreadable", reason="x")
    ]
    page = report_model.build(verify, manifest, generated_at=GENERATED_AT)
    assert page.tools.servers[0].sources == []
    (line,) = page.unattached_sources
    assert line.name == "broken" and line.status == "unreadable"


def test_the_lane_never_changes_the_ledgers_numbers(with_ledger) -> None:
    plain, lane = _pages(*with_ledger)
    for page in (plain, lane):
        assert page.tools is not None
    assert lane.tools.verified == plain.tools.verified
    assert lane.tools.declared == plain.tools.declared
    assert lane.tools.changed == plain.tools.changed
    assert lane.tools.critical == plain.tools.critical
    assert lane.tools.has_changed_check() == plain.tools.has_changed_check()
    assert lane.tools.has_stale_check() == plain.tools.has_stale_check()
    assert lane.tools.state_counts(lane.tools.servers[0]) == plain.tools.state_counts(
        plain.tools.servers[0]
    )
    assert lane.tools.standards == plain.tools.standards
    assert lane.verdict == plain.verdict
    assert lane.tool_tiles == plain.tool_tiles


def test_a_stale_lane_is_still_not_a_check(with_ledger) -> None:
    verify, manifest = with_ledger
    plain = report_model.build(verify, manifest, generated_at=GENERATED_AT)
    manifest.sources = [_source(stale=True)]
    manifest.evidence = [_record(stale=True)]
    page = report_model.build(verify, manifest, generated_at=GENERATED_AT)
    assert page.tools.servers[0].tools[0].evidence[0].stale is True
    assert page.tools.has_stale_check() == plain.tools.has_stale_check()
    assert page.verdict == plain.verdict


# --- the rendered blocks -----------------------------------------------------------


def _blocks(page: report_model.Page) -> dict:
    return report_render.extract_blocks(report_render.render(page))


def test_without_evidence_no_lane_key_is_emitted(with_ledger) -> None:
    plain, _ = _pages(*with_ledger)
    blocks = _blocks(plain)
    assert all("evidence" not in r for g in blocks["TOOLS"] for r in g["rows"])
    assert "sources" not in blocks["PAGE"]["tools"]
    assert "external" not in blocks["PAGE"]["standards"]
    assert all("evidence" not in r for r in blocks["PAGE"]["standards"]["rows"])


def test_the_lane_is_rendered_beneath_its_row_only(with_ledger) -> None:
    _, page = _pages(*with_ledger)
    blocks = _blocks(page)
    rows = {r["n"]: r for g in blocks["TOOLS"] for r in g["rows"]}
    assert "evidence" not in rows["update_record"]
    (lane,) = rows["delete_record"]["evidence"]
    assert lane["label"] == "external evidence · ASI01, ASI06"
    assert lane["src"] == "promptfoo-lab (promptfoo)"
    assert lane["rate"] == "calls 10 · refused 0 · of 12 rows"
    assert lane["targeted"] == "targeting not declared by the harness"
    assert lane["run"] == "run eval-zyk-2026-09-23T18:26:29 at 2026-09-23T18:26:29.565Z"
    assert lane["agent"] == "sonnet-5 + haiku-4-5 via agent.js"
    assert lane["stale"] is False
    # The row's own cells are the boundary lane's and are untouched.
    assert [c["txt"] for c in rows["delete_record"]["cells"] if c] == ["PASS"] * 4


def test_a_stale_lane_is_marked(with_ledger) -> None:
    verify, manifest = with_ledger
    manifest.sources = [_source(stale=True)]
    manifest.evidence = [_record(stale=True)]
    page = report_model.build(verify, manifest, generated_at=GENERATED_AT)
    rows = {r["n"]: r for g in _blocks(page)["TOOLS"] for r in g["rows"]}
    assert rows["delete_record"]["evidence"][0]["stale"] is True


def test_the_source_lines_are_in_the_tools_section(with_ledger) -> None:
    _, page = _pages(*with_ledger)
    (line,) = _blocks(page)["PAGE"]["tools"]["sources"]
    assert line["name"] == "promptfoo-lab"
    assert line["status"] == "read"
    assert line["text"] == (
        "run eval-zyk-2026-09-23T18:26:29 at 2026-09-23T18:26:29.565Z · 12 rows · "
        "1 tool matched (delete_record) · 1 unmatched (get_guide)"
    )
    assert line["agent"] == "sonnet-5 + haiku-4-5 via agent.js"
    assert line["standards"] == ["ASI01", "ASI06"]
    assert line["notes"] == [
        "2 row(s) named no tool",
        "targeting not declared by the harness",
    ]
    assert line["stale"] is False


def test_an_unreadable_source_line_says_why(with_ledger) -> None:
    verify, manifest = with_ledger
    manifest.sources = [
        _source(status="unreadable", reason="results file not found: x")
    ]
    page = report_model.build(verify, manifest, generated_at=GENERATED_AT)
    (line,) = _blocks(page)["PAGE"]["tools"]["sources"]
    assert line["status"] == "unreadable"
    assert line["text"] == "unreadable: results file not found: x"


def test_the_standards_view_lists_external_evidence_apart(with_ledger) -> None:
    """ASI01 is cited by a check in the fixture ledger: its coverage is the
    check's, and the source is listed beside it. ASI06 is cited by nothing but
    the source: it appears under the external heading with no covered count."""
    plain, page = _pages(*with_ledger)
    before = _blocks(plain)["PAGE"]["standards"]
    after = _blocks(page)["PAGE"]["standards"]
    assert after["eyebrow"] == before["eyebrow"]
    rows = {r["id"]: r for r in after["rows"]}
    assert rows["ASI01"]["evidence"] == ["promptfoo-lab"]
    assert {k: v for k, v in rows["ASI01"].items() if k != "evidence"} == next(
        r for r in before["rows"] if r["id"] == "ASI01"
    )
    assert "evidence" not in rows["ASI06"]
    assert rows["ASI06"]["coverage"] == "not_covered"
    external = after["external"]
    assert [e["id"] for e in external] == ["ASI01", "ASI06"]
    asi01, asi06 = external
    assert asi01["sources"] == ["promptfoo-lab"]
    assert asi01["note"] == "also cited by 2 checks; the coverage above is the check's"
    assert asi06["note"] == "no check cites it; external evidence only"
    assert asi06["title"]  # from the catalogue, never invented
    assert "covered" not in asi06 and "checks" not in asi06


def test_the_lanes_words_are_its_own(with_ledger) -> None:
    _, page = _pages(*with_ledger)
    blocks = _blocks(page)
    (lane,) = next(
        r for g in blocks["TOOLS"] for r in g["rows"] if r["n"] == "delete_record"
    )["evidence"]
    (line,) = blocks["PAGE"]["tools"]["sources"]
    text = json.dumps([lane, line]).lower()
    for word in ("pass", "fail", "verified", "covered"):
        assert word not in text, word


def test_the_page_renders_a_lane_from_a_real_manifest(tmp_path: Path) -> None:
    """The fixture ledger's point ids are the reference server's tools under
    the ids sync would give them only by coincidence of the fixture; here the
    manifest's own point id is what the join wrote and what the page reads."""
    from itest.core.manifest import IntegrationPoint, load_manifest, save_manifest

    manifest = Manifest(
        generated_at=NOW,
        points=[
            IntegrationPoint(
                id=DELETE_RECORD,
                type="mcp_tool",
                source="reference-mcp",
                target="delete_record",
                hcl_address=".itest/tools/reference-mcp.yaml",
                origin="declared",
                first_seen=NOW,
                last_seen=NOW,
            )
        ],
        evidence=[_record()],
        sources=[_source()],
    )
    path = tmp_path / "manifest.yaml"
    save_manifest(manifest, path)
    verify = {"points": [], "tools": _ledger()}
    page = report_model.build(verify, load_manifest(path), generated_at=GENERATED_AT)
    assert page.tools.servers[0].tools[0].evidence[0].rate == (
        "calls 10 · refused 0 · of 12 rows"
    )
