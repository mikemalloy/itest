"""The join: ``(source.server, tool name)`` onto the manifest's tool points.

A matched tool lands as an :class:`EvidenceRecord` on the manifest's own
``evidence`` list — top-level, never under ``tests``, because it is not a test.
An unmatched tool lands on the source line. A source whose server ITest does
not inventory produces zero records and says so.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_declarations_plan_sync import EXPECTED_TOOLS
from test_evidence_promptfoo import FIXTURE

from itest.core.declarations.tools import tool_point_id
from itest.core.evidence.join import is_stale, join_evidence, unreadable_source
from itest.core.evidence.loader import LoadedSource
from itest.core.evidence.promptfoo import read_results
from itest.core.evidence.schema import EvidenceSource
from itest.core.manifest import (
    SCHEMA_VERSION,
    EvidenceRecord,
    IntegrationPoint,
    Manifest,
    SourceRecord,
    load_manifest,
    save_manifest,
)

SEEN = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
NOW = datetime(2026, 9, 24, 9, 0, tzinfo=UTC)


def reference_manifest(server: str = "reference-mcp") -> Manifest:
    """reference-mcp's eight tool points, with the ids sync would give them."""
    return Manifest(
        generated_at=SEEN,
        points=[
            IntegrationPoint(
                id=tool_point_id(server, tool),
                type="mcp_tool",
                source=server,
                target=tool,
                attributes={"mutation": "read"},
                hcl_address=f".itest/tools/{server}.yaml",
                origin="declared",
                first_seen=SEEN,
                last_seen=SEEN,
            )
            for tool in sorted(EXPECTED_TOOLS)
        ],
    )


def loaded(
    server: str = "reference-mcp", *, max_age_days: int = 7, results=FIXTURE
) -> LoadedSource:
    return LoadedSource(
        name="promptfoo-lab",
        source=EvidenceSource(
            kind="promptfoo",
            server=server,
            results=str(results),
            agent="sonnet-5 + haiku-4-5 via agent.js",
            standards=["ASI01", "LLM01"],
            max_age_days=max_age_days,
        ),
        results_path=Path(results),
    )


@pytest.fixture
def fixture_run():
    return read_results(FIXTURE, source_name="promptfoo-lab", agent="sonnet-5")


def test_matched_tools_land_on_their_point_ids(fixture_run) -> None:
    manifest = reference_manifest()
    records, source = join_evidence(fixture_run, loaded(), manifest, now=NOW)

    assert source.status == "read"
    assert source.name == "promptfoo-lab"
    assert source.kind == "promptfoo"
    assert source.server == "reference-mcp"
    assert source.run_id == "eval-zyk-2026-09-23T18:26:29"
    assert source.run_at == "2026-09-23T18:26:29.565Z"
    assert source.shape == "agent"
    assert source.rows == 12
    assert source.unmatched_tools == []
    assert source.reason is None

    by_point = {r.point_id: r for r in records}
    assert set(by_point) == {
        tool_point_id("reference-mcp", "get_guide"),
        tool_point_id("reference-mcp", "search_records"),
    }
    guide = by_point[tool_point_id("reference-mcp", "get_guide")]
    assert guide.source_name == "promptfoo-lab"
    assert guide.kind == "promptfoo"
    assert guide.run_id == "eval-zyk-2026-09-23T18:26:29"
    assert guide.run_at == "2026-09-23T18:26:29.565Z"
    assert guide.tool_version == "0.123.0"
    assert guide.agent == "sonnet-5 + haiku-4-5 via agent.js"
    assert guide.calls == 10
    assert guide.refused == 0
    assert guide.succeeded == 10
    assert guide.rows_total == 12
    assert guide.rows_with_call == 10
    assert guide.targeted is None
    assert guide.stale is False
    assert guide.recorded_at == NOW


def test_the_record_carries_the_per_targeted_row_counts() -> None:
    """The three targeted-row counts ride on the record beside the run-wide
    ones, None when the tool was never targeted, so the page can read the
    targeted sentence from the manifest alone."""
    synthetic = FIXTURE.parent / "promptfoo-targeted-synthetic.json"
    run = read_results(synthetic, source_name="promptfoo-lab", agent=None)
    records, _source = join_evidence(
        run, loaded(results=synthetic), reference_manifest(), now=NOW
    )
    by_point = {r.point_id: r for r in records}
    delete = by_point[tool_point_id("reference-mcp", "delete_record")]
    assert (delete.targeted, delete.rows_with_call, delete.refused) == (8, 3, 3)
    assert delete.targeted_rows_with_call == 2
    assert delete.targeted_rows_refused == 2
    assert delete.targeted_rows_succeeded == 0
    plain = read_results(FIXTURE, source_name="promptfoo-lab", agent=None)
    records, _source = join_evidence(plain, loaded(), reference_manifest(), now=NOW)
    for record in records:
        assert record.targeted is None
        assert record.targeted_rows_with_call is None
        assert record.targeted_rows_refused is None
        assert record.targeted_rows_succeeded is None


def test_a_tool_the_manifest_does_not_inventory_is_unmatched(fixture_run) -> None:
    manifest = reference_manifest()
    manifest.points = [p for p in manifest.points if p.target != "search_records"]
    records, source = join_evidence(fixture_run, loaded(), manifest, now=NOW)
    assert [r.point_id for r in records] == [
        tool_point_id("reference-mcp", "get_guide")
    ]
    assert source.unmatched_tools == ["search_records"]
    assert source.status == "read"


def test_a_server_the_manifest_does_not_inventory_joins_nothing(
    fixture_run,
) -> None:
    records, source = join_evidence(
        fixture_run, loaded("other-server"), reference_manifest(), now=NOW
    )
    assert records == []
    assert source.status == "server_not_declared"
    assert source.server == "other-server"
    assert "other-server" in (source.reason or "")
    assert source.run_id == "eval-zyk-2026-09-23T18:26:29"
    assert source.unmatched_tools == []


def test_the_join_never_matches_a_point_of_another_type(fixture_run) -> None:
    manifest = reference_manifest()
    manifest.points.append(
        IntegrationPoint(
            id="sg-1",
            type="sg_edge",
            source="reference-mcp",
            target="get_guide",
            hcl_address="main.tf",
            first_seen=SEEN,
            last_seen=SEEN,
        )
    )
    records, _ = join_evidence(fixture_run, loaded(), manifest, now=NOW)
    assert "sg-1" not in {r.point_id for r in records}


# --- staleness ---------------------------------------------------------------------


def test_staleness_is_the_syncs_clock_against_the_runs_time() -> None:
    run_at = "2026-09-23T18:26:29.565Z"
    assert is_stale(run_at, datetime(2026, 9, 30, 18, 0, tzinfo=UTC), 7) is False
    assert is_stale(run_at, datetime(2026, 9, 30, 19, 0, tzinfo=UTC), 7) is True
    assert is_stale(run_at, datetime(2026, 9, 23, 19, 0, tzinfo=UTC), 0) is True
    # A run time the file did not carry, or one that cannot be read, is stale:
    # an undated run is not fresh evidence.
    assert is_stale(None, NOW, 7) is True
    assert is_stale("yesterday", NOW, 7) is True


def test_stale_is_computed_at_join_and_stored(fixture_run) -> None:
    records, _ = join_evidence(
        fixture_run, loaded(max_age_days=0), reference_manifest(), now=NOW
    )
    assert records and all(r.stale for r in records)
    fresh, _ = join_evidence(
        fixture_run, loaded(max_age_days=7), reference_manifest(), now=NOW
    )
    assert fresh and not any(r.stale for r in fresh)


# --- unreadable --------------------------------------------------------------------


def test_an_unreadable_source_is_a_record_with_its_reason() -> None:
    source = unreadable_source(loaded(), "results file not found: x.json")
    assert source == SourceRecord(
        name="promptfoo-lab",
        kind="promptfoo",
        server="reference-mcp",
        status="unreadable",
        reason="results file not found: x.json",
        agent="sonnet-5 + haiku-4-5 via agent.js",
        standards=["ASI01", "LLM01"],
    )
    assert source.run_id is None
    assert source.shape == "none"
    assert source.rows == 0


# --- the manifest ------------------------------------------------------------------


def test_evidence_and_sources_are_top_level_and_round_trip(
    tmp_path: Path, fixture_run
) -> None:
    manifest = reference_manifest()
    records, source = join_evidence(fixture_run, loaded(), manifest, now=NOW)
    manifest.evidence = records
    manifest.sources = [source]
    path = tmp_path / "manifest.yaml"
    save_manifest(manifest, path)
    text = path.read_text(encoding="utf-8")
    assert "\nevidence:\n" in text
    assert "\nsources:\n" in text
    back = load_manifest(path)
    assert back.evidence == records
    assert back.sources == [source]
    assert back.tests == []  # never under tests


def test_a_manifest_without_evidence_serializes_exactly_as_before(
    tmp_path: Path,
) -> None:
    """No schema bump: the fields default cleanly and are written only when
    set, so a project without a source is byte-identical to one written before
    sources existed."""
    manifest = reference_manifest()
    path = tmp_path / "manifest.yaml"
    save_manifest(manifest, path)
    text = path.read_text(encoding="utf-8")
    assert "evidence" not in text
    assert "sources" not in text
    assert SCHEMA_VERSION == 2
    assert load_manifest(path).evidence == []
    assert load_manifest(path).sources == []


def test_an_evidence_record_is_not_a_test_entry() -> None:
    fields = set(EvidenceRecord.model_fields)
    for forbidden in ("status", "state", "ownership_hash", "tier", "trait"):
        assert forbidden not in fields
